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

Optional (at most one): a handoff to a second agent, streaming, or request cancellation. Don't add one unless asked.

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
   │  POST /invocations {message | interruptResponse}   (AgentCore HTTP contract)
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
- On the next turn the UI sends `interruptResponse {interruptId, response: "approve" | "decline"}`. The agent is rebuilt from the session store and the hook resumes: approve lets the tool run, decline sets `event.cancel_tool`.
- **Every PO is approved on its own.** Don't use the docs' "trust" pattern of remembering approval in `agent.state`.
- **Approval is structured, never inferred by the model.** UI buttons send the `interruptResponse`. A typed "yes" may map to it in code, but we never ask the LLM whether the user approved.
- **Idempotency key = hash(`user_id`, `session_id`, normalized order)**, derived by the MCP server, never by the model. `user_id` and `session_id` reach the MCP server as connection headers set by the agent service. Why not `session_id` + `toolUseId`: if a write commits but the client sees a 503 and gives up, the user's next attempt is a new tool call with a new `toolUseId`, so a new key and a duplicate PO. Trade-off: an identical order placed twice in one session returns the existing PO. (Verified: a Strands `BeforeToolCallEvent` hook *can* modify `tool_use["input"]`, so hook-injected keys remain an option.)
  - **TODO, explore later:** other key strategies, e.g. a key tied to the approval/interrupt id, reconciling by looking up the PO after a give-up, or letting the user explicitly re-order the same items in one session.

### Memory: one `memory` component on SQLite (decided)

The two layers copy AgentCore Memory's short-term and long-term split, so the AgentCore design can swap in `AgentCoreMemorySessionManager`.

| Layer | Holds | Mechanism | Scope / lifetime |
|---|---|---|---|
| Short-term | Conversation history, agent state, pending interrupts | Our own `SessionRepository` (SQLite), plugged into Strands' `RepositorySessionManager` | Per `(user_id, session_id)`; purged after 30 days without activity (`AGENT_SESSION_RETENTION_DAYS`) |
| Long-term | User facts (lab, team, delivery location) | `user_facts` table; loaded into the system prompt and updated by a local `remember_fact` tool | Per `user_id`, until overwritten |

- **Isolation is structural.** Each session row stores its owner's `user_id`, and the service rejects any request for a session owned by someone else. Memory tools take `user_id` from the request context, never from model arguments.

### Observability: OpenTelemetry → Grafana `otel-lgtm` (decided)

- Set up with `StrandsTelemetry`: `setup_otlp_exporter()` for traces and `setup_meter(enable_otlp_exporter=True)` for metrics.
- **Built-in Strands signals:** tokens in and out, agent cycles, model latency, and tool calls by name, status and duration.
- **Custom counters:**
  - approvals proposed, approved and declined
  - faults injected
  - MCP retries, with outcome
  - duplicate POs blocked
- **Traces:** one trace per `/invocations` request across the agent service, the MCP server and the mock. Propagate trace context over the MCP and HTTP hops.
- **Dashboard:** a pre-built Grafana dashboard JSON is provisioned, so `docker compose up` opens to it.

### Model provider (decided)

- **Default: Anthropic**, through Strands' `AnthropicModel` (`strands-agents[anthropic]`) with `ANTHROPIC_API_KEY`.
- Swappable through config (provider and model ID from env/settings) to Bedrock, OpenAI or Ollama. Keep the provider choice inside one factory function.

### Evals: Strands Evals SDK (decided)

- Package `strands-agents-evals`, imported as `strands_evals`. Build with `Case`, `Experiment` and evaluators; one command runs every experiment.
- **Deterministic checks first.** Write custom `Evaluator` subclasses that return `EvaluationOutput` for anything that can be checked exactly. Cases cover:
  - the right tool chosen (trajectory from `tools_use_extractor`)
  - an interrupt raised before `create_purchase_order`, with no PO in the mock before approval
  - exactly one PO after approval with faults on
- **LLM judges only for fuzzy cases.** Use `OutputEvaluator` or `TrajectoryEvaluator` with a rubric for things like how well an out-of-scope request is refused, or whether the failure message is clear.
- **Judge model:** string model IDs and the default judge are **Bedrock** IDs. Pass a Strands `AnthropicModel` instance as `model=` so evals don't need AWS.
- **Multi-turn cases** (propose, then approve or decline) are scripted in the task function, which sends the `interruptResponse` itself.
- Save experiment results (`experiment.to_file`) so the README can cite the latest run.

### AgentCore Runtime readiness (decided)

The agent service follows the AgentCore Runtime HTTP contract (Option B, custom FastAPI agent), so the same container runs locally and on AgentCore:

- `POST /invocations` (JSON body: `message`, later `interruptResponse`), `GET /ping` → `{"status": "Healthy"}`, port 8080, host `0.0.0.0` in containers, `linux/arm64` images.
- Identity travels in headers only, never in the body; both are required and validated against the same safe-id pattern:
  - Session: `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`. AgentCore sets it from `runtimeSessionId`; locally the client (UI, evals, curl) sets it.
  - User: `X-User-Id`, the same name the MCP server reads. On AgentCore it must be added to the runtime's request-header allowlist.
- The local version trusts `X-User-Id`. On AgentCore the user must come from a validated JWT (`customJWTAuthorizer`), not a plain header. Note this in README/AGENTCORE.md.
- Not `BedrockAgentCoreApp` (Option A): we already have FastAPI and want no extra dependency.
- AgentCore microVMs are per session with temporary disk, so local SQLite state is a local-only choice; on AgentCore, sessions move to AgentCore Memory (see Memory).

### Logging (decided)

- Standard library `logging`, configured once by `supplies_agent.logs.configure_logging()` at the start of each service's `main()`. `LOG_LEVEL` (default INFO) for our code, `STRANDS_LOG_LEVEL` (default WARNING) for the SDK.
- Every module uses `logging.getLogger(__name__)`. Messages follow the Strands style: `field=<value>, other=<value> | message`.
- Log events and ids, not content: no user message text or tool results in logs (traces carry those).
- Trace/log correlation (`trace_id` on log lines) comes with D8.

### Optional extra (decided)

- **None for now.** No handoff, streaming or cancellation. Revisit only if the user asks.

### Still open

- Other idempotency-key strategies (see the Approval section).

## Dependencies

One `pyproject.toml` and one `uv.lock`. Shared deps go in `[project].dependencies`; each layer's runtime deps go in its own dependency group (`api`, `mcp`, `agent`), plus `dev`. `default-groups = "all"` installs everything locally; each service image installs only its group (`uv sync --locked --no-default-groups --group <layer>`). Groups don't stop cross-layer imports, so `tests/test_layers.py` enforces the layering:

- `api`, `mcp_server` and `memory` import no other layer; `agent` may import `memory`; every layer may import `logs`.
- Layers talk over their real interfaces: agent → MCP server over MCP, MCP server → mock over HTTP.
- `agent` uses only `memory`'s public API (`__all__` in each memory module: `setup`, `session_manager`, `SessionAccessDenied`). `memory` takes plain ids, never the agent's `RequestContext`.
- A new top-level package needs a rule in `tests/test_layers.py` (a test fails until it has one).

## Layout (proposed — update as it settles)

```
src/supplies_agent/
  agent/        # Strands agent, prompts, approval hook, FastAPI app (app.py: /invocations, /ping) + web UI
  memory/       # SQLite SessionRepository (sessions.py) + user facts
  mcp_server/   # MCP server exposing the mock as tools  [done]
  api/          # inventory/ordering mock (SQLite) + fault injection  [done]
  telemetry/    # OTel setup + custom metrics
evals/          # eval cases + runner
tests/
deploy/grafana/ # provisioned dashboard JSON
docs/SCOPE.md   # assignment brief
```

## Commands

The user runs all `uv` commands (see Working rules).

- Install: `uv sync`
- Run the mock: `uv run mock-api` (env prefix `API_`, e.g. `API_KEY`, `API_FAULTS__SERVER_ERROR_RATE`; serves on `127.0.0.1:8001`)
- Run the MCP server: `uv run mcp-server` (env prefix `MCP_`, e.g. `MCP_API_URL`, `MCP_API_KEY`; streamable HTTP at `127.0.0.1:8002/mcp`, stateless, JSON responses). Identity comes from `X-User-Id` / `X-Session-Id` headers.
- Run the agent service: `uv run agent-service` (env prefix `AGENT_`, plus `ANTHROPIC_API_KEY`; reads `.env`, see `.env.example`; serves `POST /invocations` and `GET /ping` on `127.0.0.1:8080`; sessions in `AGENT_DB_PATH`, default `data/agent.db`)
- Test: `uv run pytest` (agent tests use `tests/agent/scripted_model.py`, a fake model: no LLM calls). Test files need unique basenames.
- Lint/format: `uv run ruff check . && uv run ruff format .`
- Eval: _TBD_
