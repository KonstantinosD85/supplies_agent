# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

Take-home assignment: build a **Lab Supplies Agent** (Option 3). The full brief is in `docs/SCOPE.md`; read it before making design decisions. The implementation plan and progress are in `docs/PLAN.md`: check it at the start of a task and tick items off as they're finished.

A scientist asks for reagents or consumables. The agent:

1. checks what is in stock,
2. finds catalogue items from approved suppliers,
3. proposes a purchase order and waits for the user to approve it,
4. creates the purchase order only after approval.

The only write is **create purchase order**. Everything else is a read.

## Stack

- **Language:** Python 3.12+
- **Agent framework:** [Strands Agents](https://strandsagents.com) (`strands-agents`, plus extras such as `[bedrock]`, `[anthropic]`, `[openai]`, `[otel]`)
- **Tools:** our own MCP server (the `mcp` Python SDK). The agent connects to it through `strands.tools.mcp.MCPClient`.
- **Model:** Anthropic by default (swappable via config)
- **Evals:** Strands Evals SDK (`strands-agents-evals`)
- **Observability:** OpenTelemetry → Grafana `otel-lgtm`
- **Mock backend:** a small inventory and ordering service on SQLite with a few seeded records
- **Package and env management:** `uv`

Before writing Strands, MCP or other library code, check the current docs with Context7. Don't rely on memory: these APIs change quickly.

## Required components (from the brief)

| # | Component | Notes |
|---|-----------|-------|
| 1 | Agent service | HTTP endpoint that takes `user_id`, `session_id` and `message`. Model provider is swappable via config. |
| 2 | MCP server | Wraps the mock. Needs a paginated search tool, a read tool and the write tool (create PO). Authenticates to the mock with a key or token. The agent reaches the mock **only** through this server. |
| 3 | Approval gate | The agent proposes the PO and stops. The user approves or declines in their next message. A pending approval **must survive a service restart**, so persist it rather than keeping it in memory. |
| 4 | Memory | Session conversation history, plus at least one fact per user that persists across sessions (e.g. their lab or team). Strict per-user isolation. |
| 5 | Failure handling | A switch on the mock injects rate limits, 5xx errors and slow responses. The agent retries with backoff, gives up after a timeout, tells the user when it can't finish, and **never creates the same PO twice** (use idempotency keys). |
| 6 | Evals | One command runs all cases. Cover at least: correct tool choice, the approval step respected, an out-of-scope request refused, and a run with failures on. |
| 7 | Traces | Per request: model calls, tool calls with args and results, latency, tokens and errors (OTel or structured logs). |
| 8 | Web UI | Thin page that shows tool calls and has approve and decline buttons. Visual design isn't scored. |

Optional (at most one): a handoff to a second agent, streaming, or request cancellation. **Chosen: streaming** (see "Optional extra").

Deliverables also include `README.md` and `AGENTCORE.md` (how this would run on Amazon Bedrock AgentCore).

## Working rules

- **Never run `uv` commands yourself** (`uv add`, `uv sync`, `uv run`, `uv pip`, `uv init`, etc.). If a dependency, environment change or command is needed, stop and brainstorm it with the user first: what's needed, why, and the alternatives. Let the user run it.
- **Never `git commit` or `git push`.** Leave changes in the working tree; the user reviews and commits.
- **Simple and well-engineered beats clever.** The brief says so explicitly. Avoid speculative abstractions and extra features.
- **Keep the mock small:** SQLite, a handful of seeded suppliers, catalogue items and stock rows.
- **Tool design matters:** write clear names, tight JSON schemas, descriptions the model can act on, and error messages that tell the model what to do next.
- **Writes are idempotent:** every create-PO call carries an idempotency key, and the mock deduplicates on it.
- **Containerised:** it should run with `docker compose up`.
- **Tests and evals live in the repo** and run with one command each.
- **README.md and AGENTCORE.md must reflect the author's own thinking.** Don't write their narrative or opinion sections unprompted. Help with structure, facts, diagrams and drafts only when asked, and mark drafts clearly.

## Architecture decisions

### Services (one `docker compose`)

```
Browser (thin UI: chat, tool-call log, Approve/Decline)
   │  POST /invocations {message} | {approvals: [{interrupt_id, decision}]}   (AgentCore HTTP contract)
   │  JSON, or SSE with Accept: text/event-stream
   │  headers: X-User-Id, X-Amzn-Bedrock-AgentCore-Runtime-Session-Id
   ▼
agent-service   FastAPI + Strands Agent + approval hook + memory (SQLite) + static UI
   │  MCP (streamable HTTP)
   ▼
mcp-server      search_catalog (paginated), read tools, create_purchase_order
   │            retries with backoff, overall timeout
   │  HTTP + API key
   ▼
mock-api        FastAPI + SQLite (seeded): suppliers, catalogue, stock, POs
                dedupes POs by Idempotency-Key; fault switch (429 / 5xx / slow)

otel-lgtm       Grafana + Tempo + Prometheus + Loki; receives OTLP from all three services
```

The mock is a separate HTTP service so that the MCP server's authentication to it is real.

### Approval: Strands interrupts (decided)

- A `BeforeToolCallEvent` hook on `create_purchase_order` calls `event.interrupt("po-approval", reason={...PO summary...})`.
- The turn ends with `stop_reason == "interrupt"`. The session manager persists the interrupt state, so the pending approval survives a restart.
- On the next turn the UI sends `{"approvals": [{"interrupt_id", "decision": "approve" | "decline"}]}`; the service turns it into Strands `interruptResponse` blocks. The agent is rebuilt from the session store and the hook resumes: approve lets the tool run, decline sets `event.cancel_tool`.
- Pending approvals are mirrored in `agent.state["pending_approvals"]` (public, session-persisted) so a request is checked before the agent runs: a message while an order waits, or a decision when nothing waits or for the wrong ids, gets 409 with the pending list. The response carries `pending_approvals`.
- **Every PO is approved on its own.** Don't use the docs' "trust" pattern of remembering approval in `agent.state`.
- **Approval is structured, never inferred by the model.** UI buttons send the decision. A typed "yes" is refused with 409 (not mapped, for now); we never ask the LLM whether the user approved.
- **Idempotency key = hash(`user_id`, `session_id`, normalized order)**, derived by the MCP server, never by the model. `user_id` and `session_id` reach the MCP server as connection headers set by the agent service. Why not `session_id` + `toolUseId`: if a write commits but the client sees a 503 and gives up, the user's next attempt is a new tool call with a new `toolUseId`, so a new key and a duplicate PO. Trade-off: an identical order placed twice in one session returns the existing PO. (Verified: a Strands `BeforeToolCallEvent` hook *can* modify `tool_use["input"]`, so hook-injected keys remain an option.)
  - **TODO, explore later:** other key strategies, e.g. a key tied to the approval/interrupt id, reconciling by looking up the PO after a give-up, or letting the user explicitly re-order the same items in one session.

### Memory: one `memory` component on SQLite (decided)

The two layers copy AgentCore Memory's short-term and long-term split, so the AgentCore design can swap in `AgentCoreMemorySessionManager`.

| Layer | Holds | Mechanism | Scope / lifetime |
|---|---|---|---|
| Short-term | Conversation history, agent state, pending interrupts | Our own `SessionRepository` (SQLite), plugged into Strands' `RepositorySessionManager` | Per `(user_id, session_id)`; purged after 30 days without activity (`AGENT_SESSION_RETENTION_DAYS`) |
| Long-term | User facts (lab, team, delivery location) | Strands memory SDK: our `SqliteFactStore` (`memory/facts.py`, `user_facts` table) in a `MemoryManager`; the profile is injected into the model input on each new message, written only by the `remember_fact` tool through `memory_manager.add` | Per `user_id`, until overwritten |

- **Isolation is structural.** Each session row stores its owner's `user_id`, and the service rejects any request for a session owned by someone else. The fact store is built per request bound to the caller's `user_id`; no memory tool takes a user id from the model.
- **Facts are a closed set of keys** (`lab`, `team`, `delivery_location`), one value per key: `remember_fact(key, value)` calls `memory_manager.add(value, {"metadata": {"key": key}})` and the store upserts. The SDK's own `search_memory` and free-text `add_memory` tools and extraction stay off. Injection is ephemeral (not saved in session history) and runs on every model call (`trigger: "everyTurn"`): the SDK default injects only on the first call of a turn, but the model picks `deliver_to` after its tool lookups.
- **Every order needs a delivery location.** `deliver_to` is required by the mock, the MCP tool and the approval card. The agent uses the saved `delivery_location`, otherwise asks once and saves the answer; a location for one order doesn't replace the saved one. The approval hook sends an order without `deliver_to` back to the model instead of proposing it.

### Observability: OpenTelemetry → Grafana `otel-lgtm` (decided)

**Agent service and MCP server** (the mock exports nothing yet).

- `agent/telemetry.py` `setup()`: `StrandsTelemetry` with `setup_baggage_processor`, `setup_otlp_exporter()` and `setup_meter(enable_otlp_exporter=True)`, plus a `LoggerProvider` with an OTLP log exporter and the `LoggingHandler` from `opentelemetry-instrumentation-logging` on the root logger (the SDK's own `LoggingHandler` is deprecated) (deps: `strands-agents[anthropic,otel]`, `opentelemetry-instrumentation-logging`). On only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (compose sets it), so tests, evals and plain local runs export nothing.
- **Traces:** one trace per `/invocations`. FastAPI's native telemetry makes the root span (`POST /invocations`, HTTP status) and HTTP metrics; configured with `FASTAPI_TELEMETRY` (no `/ping` spans, no per-operation spans). Strands adds agent, cycle, model (`chat`) and tool spans with tokens, args and results.
- **Caller in baggage:** `caller_context()` sets `user.id` and `session.id` on the root span and in baggage; a `BaggageSpanProcessor` (those two keys only) stamps them on every span. Baggage also travels to the MCP server in `_meta` (its `tools/call` span already joins the trace in-process tests). Ids only in baggage, never secrets or content.
- **Metrics:** built-in Strands (tokens, cycles, model latency, tool calls/errors/duration) and FastAPI (`http.server.request.duration`). Prometheus names, e.g. `strands_tool_call_count_Count_total`, `strands_event_loop_input_tokens_token_sum`, `http_server_request_duration_seconds_bucket`. Quirks: Strands labels tool metrics with `tool_use_id` (a series per call; aggregate by `tool_name`), counts a tool call interrupted for approval as a tool error, and its seconds histograms use default buckets (0, 5, 10… s), so use model latency (ms) for latency panels.
- **Dashboard:** `deploy/grafana/supplies-agent.json` (Grafana home in `otel-lgtm`, datasource uids `prometheus`, `tempo`, `loki`), provisioned by `deploy/grafana/dashboards.yaml`. Stat panels use `sum(last_over_time(...[$__range]))` (counter totals per instance, robust to restarts and new series), graphs use `rate()`. A table lists recent traces via TraceQL and a logs panel (Loki); both filter by the `user_id` and `session_id` variables (prefix match), the logs also by `level`. Metric panels can't be filtered (no user/session labels: high cardinality), so their titles say "all users"; per-user numbers would come from TraceQL metrics, if otel-lgtm's Tempo enables them (unchecked).
- **Custom counter:** `supplies.approvals` (`agent/approval.py`, `outcome` = proposed / approved / declined), created from the global meter, so it is a no-op until `setup()` runs.
- **Logs → Loki:** exported records keep their `extra` fields as attributes, which Loki stores as structured metadata, so they are searchable without parsing: `{service_name="supplies-agent"} | user_id="alice" | stop_reason="interrupt"`. Each record carries `trace_id`/`span_id`: otel-lgtm's Loki datasource links a line to its trace and Tempo's "logs for this span" finds the lines. Only what reaches the root logger is exported (our INFO+, Strands WARNING+; uvicorn's own loggers don't propagate).
- **Console correlation:** `configure_logging(trace=...)` gives the console formatter a callable for the text after the logger name; the agent passes `telemetry.log_trace` (` trace_id=<32 hex>` inside a request, empty otherwise). `logs.py` stays free of OpenTelemetry (the mock image doesn't have it).
- **MCP server:** `mcp_server/telemetry.py` `setup()` (traces only; dep `opentelemetry-exporter-otlp-proto-http` in the `mcp` group): a `TracerProvider` with an OTLP exporter and `CallerSpanProcessor` (copies `user.id`/`session.id` from baggage; our own, so the `mcp` image needs no `opentelemetry-processor-baggage`). The MCP SDK's built-in `OpenTelemetryMiddleware` makes the `tools/call <tool>` server span from the `_meta` trace context; `SuppliesApi.request` adds one `supplies API <METHOD>` client span per attempt (`supplies.attempt`, `http.response.status_code`, `supplies.fault_injected`, error status on failed attempts). Compose sets `OTEL_SERVICE_NAME=supplies-mcp`. Logs stay on the console with ` trace_id=<...>` (OTLP logs would need `opentelemetry-instrumentation-logging` in the `mcp` group).
- **Later:** the mock (traces, logs), MCP logs to Loki, and counters for faults injected, MCP retries with outcome, duplicate POs blocked.

### Model provider (decided)

- **Default: Anthropic**, through Strands' `AnthropicModel` (`strands-agents[anthropic]`) with `ANTHROPIC_API_KEY`.
- Swappable through config (provider and model ID from env/settings) to Bedrock, OpenAI or Ollama. Keep the provider choice inside one factory function.
- A model is built per request and each turn runs in its own event loop (`asyncio.run(agent.invoke_async(...))`). `close_model()` must close the provider's async HTTP client in that same loop; otherwise garbage collection closes it on another loop and logs "Event loop is closed". A new async provider needs a branch there.

### Evals: Strands Evals SDK (decided)

- Package `strands-agents-evals` (dependency group `evals`), imported as `strands_evals`. One `Experiment` per group of cases; `python -m evals` runs them all.
- **Evals test the agent in isolation, as the SDK docs do.** The task (`evals/task.py`) builds the agent with `build_agent` (real model, prompt, approval hook, fact store on a temporary SQLite) and calls `agent.invoke_async` directly: no FastAPI, no session store. A case's `decision` resumes the approval interrupt with `interruptResponse` blocks, as the UI buttons do. **Fake supplies tools** (`evals/fake_supplies.py`) use the MCP server's real tool definitions (read in-process), result models and error wording, with data from `api/seed.py`; a contract test (`tests/evals/test_fake_supplies.py`) compares them with the real MCP tools. Integration guarantees (409s, sessions, user isolation, duplicate POs under faults) are covered by pytest, not by the evals.
- **Cases** (`evals/cases.py`, `groups(judge)` → `{group: (cases, evaluators)}`): a message plus metadata (`facts`, `stock`, `unavailable`, `then_say`, `decision`), the expected end state (`expected_environment_state`), and optionally `expected_trajectory` and `expected_output` (the reply the judge should see). The task reports four states: `tools_called`, `proposed_orders`, `orders_placed`, `saved_facts`. Groups: tool_choice, approval, out_of_scope, failures, user_facts.
- **Built-in evaluators only.** Deterministic first: `StateEquals(name)` compares a state with the case's expected value (every case in a group must give one for each `StateEquals` there), `ToolCalled`. LLM judges where exact checks can't decide: `TrajectoryEvaluator` (tool order, via its scorer tools; `trajectory_description` is ignored in 1.6.0) and `OutputEvaluator` with a rubric (refusal and outage wording). `RefusalEvaluator` needs a traced `Session`, so it isn't used.
- **Judge model:** a cheaper Anthropic model (`EVAL_JUDGE_MODEL_ID`, default `claude-haiku-5-5`), passed as an `AnthropicModel` instance so evals don't need AWS (string ids are Bedrock ids).
- Results: `report.to_file("evals/results/latest.json")`, committed so the README can cite the latest run. Exit code 1 if any case fails.

### AgentCore Runtime readiness (decided)

The agent service follows the AgentCore Runtime HTTP contract (Option B, custom FastAPI agent), so the same container runs locally and on AgentCore:

- `POST /invocations` (JSON body: `message`, or `approvals` to decide on pending purchase orders), `GET /ping` → `{"status": "Healthy"}`, port 8080, host `0.0.0.0` in containers, `linux/arm64` images.
- Identity travels in headers only, never in the body; both are required and validated against the same safe-id pattern:
  - Session: `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`. AgentCore sets it from `runtimeSessionId`; locally the client (UI, evals, curl) sets it.
  - User: `X-User-Id`, the same name the MCP server reads. It can't pass through AgentCore Runtime as is: Runtime forwards only allowlisted headers, and only `Authorization` or names starting with `X-Amzn-Bedrock-AgentCore-Runtime-Custom-` can be allowlisted. On AgentCore the user comes from the JWT (see AGENTCORE.md §5).
- The local version trusts `X-User-Id`. On AgentCore the user must come from a validated JWT (`customJWTAuthorizer`), not a plain header. Note this in README/AGENTCORE.md.
- Not `BedrockAgentCoreApp` (Option A): we already have FastAPI and want no extra dependency.
- AgentCore microVMs are per session with temporary disk, so local SQLite state is a local-only choice; on AgentCore, sessions move to AgentCore Memory (see Memory).

### Agent service structure (decided)

One concern per module in `agent/`; FastAPI dependencies wire everything before the route runs.

- `app.py`: `create_app(settings)` only: shared state (`app.state.settings`, `app.state.locks`), storage setup, exception handlers, router. Plus `main()`.
- `routes.py`: thin endpoints (`/`, `/ping`, `/invocations`): run the prepared turn as JSON, or stream it.
- `dependencies.py`: `Settings`, `Locks`, `Caller` (async yield: identity headers → span + baggage, so later dependencies and the route inherit it), `WantsStream` (`Accept`), `get_model`, `get_tool_source`, `get_fact_store`, and `PreparedInvocation` (sync yield, the only one reading the body). Tests override `get_model` / `get_tool_source`.
- `invocation.py`: one agent invocation (one `/invocations` request; it runs as many Strands event-loop cycles as the model needs), no FastAPI. `prepare()` (lock → session (403) → MCP connection (503) → agent → approval check (409), in an `ExitStack`), `run()` (takes the resources over with `pop_all()` and releases them itself; the dependency's teardown releases whatever was never taken over), the reply text.
- `tool_calls.py`: the `ToolCalls` hook: emits `tool_start`/`tool_end` as tools run, keeps finished results (Strands parks them while an approval waits) and `report()`s the invocation's tool calls for the response; one truncation (`result_text`) for both.
- `errors.py`: one exception handler per expected error (`SessionAccessDenied` 403, `ApprovalConflict` 409, `SessionBusy` 409, `MCPClientInitializationError` 503, `ModelThrottledException` 503), body `{"detail": ...}`; `answer()` also builds the stream's `error` event.
- `streaming.py`: the worker-thread bridge and the SSE response.
- Header models can't share a dependant with other header params, so `Accept` lives in its own dependency. Only one dependency may take the `ChatRequest` body (more would make FastAPI embed it by name).

### Logging (decided)

- Standard library `logging`, configured once by `supplies_agent.logs.configure_logging()` at the start of each service's `main()`. `LOG_LEVEL` (default INFO) for our code, `STRANDS_LOG_LEVEL` (default WARNING) for the SDK.
- Every module uses `logging.getLogger(__name__)`. **Structured:** the message names the event, fields go in `extra` (`logger.info("invocation finished", extra={"user_id": ..., "stop_reason": ...})`), never formatted into the message. `logs.ConsoleFormatter` prints them in the Strands style, `... | field=<value>, other=<value> | message`; the OTLP handler exports them as attributes.
- Log events and ids, not content: no user message text or tool results in logs (traces carry those).
- The agent's lines carry ` trace_id=<...>` after the logger name and are exported to Loki (see Observability).
- Log output isn't unit-tested (decided: not worth the tests); check it in the smoke run.

### Optional extra: streaming (decided)

- **Opt-in by header:** `POST /invocations` with `Accept: text/event-stream` streams server-sent events; without it the response is the `ChatResponse` JSON, unchanged (tests, evals, curl). AgentCore Runtime passes SSE responses from `/invocations` through.
- **Events** (`schemas.STREAM_EVENTS`, SSE `event:` name + JSON `data:`): `text` (delta), `tool_start` (`id`, `name`, `input`), `tool_end` (`id`, `status`, `result`), then `done` (the same `ChatResponse` as the JSON path, the source of truth) or `error` (`status`, `detail`).
- **Refusals stay HTTP statuses:** `invocation.prepare()` (run by the `prepared_invocation` dependency) runs everything that can refuse an invocation (403, 409, MCP 503) before the first byte. A failure mid-invocation becomes a final `error` event (the 200 is already sent). Strands retries model throttling itself; only what's left reaches the user.
- **One invocation path:** `invocation.run()` serves both: `agent.stream_async` (text deltas) plus the `tool_calls.ToolCalls` hook (`BeforeToolCallEvent` / `AfterToolCallEvent`; tool results aren't stream events). JSON calls it in the request thread with a no-op emit; streaming calls it in a worker thread (`streaming.start`, its own event loop, the request's context copied so spans and logs join the trace) that feeds a queue.
- **The worker owns the turn:** it releases the session lock and MCP connection and records pending approvals itself, so a client that disconnects mid-stream can't leave the session locked or half-recorded. The turn finishes; its outcome is in the session.
- **UI:** `fetch` + stream reader (not `EventSource`, GET-only); tool rows and text render live, `done` settles the turn. Text before a tool call is treated as narration and replaced, as the JSON reply shows only the final message.
- No cancellation or handoff (at most one extra).

### Still open

- Other idempotency-key strategies (see the Approval section).

## Dependencies

One `pyproject.toml` and one `uv.lock`. Shared deps go in `[project].dependencies`; each layer's runtime deps go in its own dependency group (`api`, `mcp`, `agent`), plus `dev`. `default-groups = "all"` installs everything locally; each service image installs only its group (`uv sync --locked --no-default-groups --group <layer>`). Groups don't stop cross-layer imports, so `tests/test_layers.py` enforces the layering:

- `api`, `mcp_server` and `memory` import no other layer; `agent` may import `memory`; every layer may import `logs`.
- Layers talk over their real interfaces: agent → MCP server over MCP, MCP server → mock over HTTP.
- `agent` uses only `memory`'s public API (`__all__` in each memory module: `sessions.setup`, `session_manager`, `SessionAccessDenied`; `facts.setup`, `store`, `FACT_KEYS`, `STORE_NAME`). `memory` takes plain ids, never the agent's `RequestContext`.
- A new top-level package needs a rule in `tests/test_layers.py` (a test fails until it has one).

## Layout (proposed — update as it settles)

```
src/supplies_agent/
  agent/        # Strands agent, prompts, approval hook, FastAPI service (see "Agent service structure"; static/index.html web UI; telemetry.py: OTel traces, metrics, logs)
  memory/       # SQLite SessionRepository (sessions.py) + MemoryStore for user facts (facts.py)
  mcp_server/   # MCP server exposing the mock as tools  [done]
  api/          # inventory/ordering mock (SQLite) + fault injection  [done]
  logs.py       # shared logging setup (configure_logging, ConsoleFormatter); imports no layer
evals/          # eval cases (groups), task, fake supplies tools, runner (python -m evals)
scripts/        # eval_report.py: HTML report from evals/results/latest.json
tests/
deploy/grafana/ # provisioned dashboard JSON
Dockerfile, docker-compose.yml
docs/SCOPE.md   # assignment brief
```

## Commands

The user runs all `uv` commands (see Working rules), including the `make` targets that wrap them. `make` (or `make help`) lists the targets; a new target needs a `## description` on its line to show up there.

- Install: `uv sync`
- Run the mock: `make api` / `uv run mock-api` (env prefix `API_`, e.g. `API_KEY`, `API_FAULTS__SERVER_ERROR_RATE`; serves on `127.0.0.1:8001`)
- Run the MCP server: `make mcp` / `uv run mcp-server` (env prefix `MCP_`, e.g. `MCP_API_URL`, `MCP_API_KEY`; streamable HTTP at `127.0.0.1:8002/mcp`, stateless, JSON responses). Identity comes from `X-User-Id` / `X-Session-Id` headers. Host headers are checked on any bind address (`MCP_ALLOWED_HOSTS`, JSON list; the SDK alone checks only on localhost).
- Run the agent service: `make agent` / `uv run agent-service` (env prefix `AGENT_`, plus `ANTHROPIC_API_KEY`; reads `.env`, see `.env.example`; serves `POST /invocations`, `GET /ping` and the web UI at `GET /` on `127.0.0.1:8080`; sessions in `AGENT_DB_PATH`, default `data/agent.db`)
- Run everything in containers: `make docker-up` / `docker compose up --build` (stop: `make docker-down`, keeps the volumes) (one `Dockerfile`, `GROUP` build arg per service; UI on `localhost:8080`, Grafana on `localhost:3000` (admin/admin), mock on `127.0.0.1:8001` for `PUT /admin/faults` with `X-API-Key`; MCP server internal only; SQLite bind-mounted from `./data`, the same `agent.db` and `mock.db` as a local run, so don't run both at once).
- Test: `make test` or `uv run pytest` (agent tests use `tests/agent/scripted_model.py`, a fake model: no LLM calls; the `app_for` fixture in `tests/agent/conftest.py` wires it, and stand-in tools, through `app.dependency_overrides`; `client_for` builds on it). Test files need unique basenames.
- Lint: `make lint` (`ruff check` + `ruff format --check`, changes nothing). Fix and format: `make format` (`ruff check --fix` + `ruff format`).
- Eval: `make eval` or `uv run python -m evals` (all cases; `--case <name>` to filter; needs `ANTHROPIC_API_KEY`). Writes `evals/results/latest.json`.
- Eval report: `make eval-report` runs the evals, then `scripts/eval_report.py` (standard library only; can also run on its own) turns `latest.json` into `evals/results/latest.html`: one entry per case, grouped by the name prefix (`tool-`, `approval-`, …), with each evaluator's verdict, trajectories, output and end state.
