# Implementation plan

The work is split into deliverables. Each one runs and can be demoed on its own, and each
leaves the system safe: no PO can be created without approval at any stage.

Tick a deliverable off when its "done when" demo works and its tests pass.

## Done

- [x] **Supplies API (mock)**: `src/supplies_agent/api/`. SQLite seed data, API-key auth,
  paginated catalogue search, stock lookup, idempotent PO creation, fault switch.
- [x] **MCP server**: `src/supplies_agent/mcp_server/`. Four tools (`check_stock`,
  `search_catalog`, `get_catalog_item`, `create_purchase_order`), retries with backoff and
  timeouts, identity from headers, idempotency key derived on the server.

- [x] **Logging**: `src/supplies_agent/logs.py`, Strands-style `field=<value> | message` lines in all three services.

## Agent deliverables

| # | Deliverable | Done when |
|---|---|---|
| D1 | Read-only agent behind `/invocations` | `POST /invocations {message}` with `X-User-Id` and `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` headers → "Do we have PBS?" checks stock and searches the catalogue. Out-of-scope requests are refused. |
| D2 | Session memory (short-term) | A multi-turn conversation survives a service restart. Another user can't open your session. |
| D3 | Approval gate + write tool | The agent proposes a PO and stops. Approve or decline in the next request, even after a restart. Exactly one PO, even with faults on. |
| D4 | Web UI | A chat page with a tool-call log and Approve/Decline buttons runs the full flow. |
| D5 | User facts (long-term memory) | A fact like "deliver to Lab 3.12" is remembered in a new session and used. Other users never see it. |
| D6 | Evals | One command runs all cases: right tool, approval respected, out-of-scope refused, faults on. Results are saved to a file. |
| D7 | `docker compose up` | Mock, MCP server and agent service start from one command. |
| D8 | Observability | `otel-lgtm` in compose. One trace per `/invocations` across all services, custom metrics, provisioned dashboard. |
| D9 | Docs support | README skeleton, AGENTCORE diagram and facts, screenshots, eval output. The narrative is the author's. |

### D1: Read-only agent

- [x] Model factory: provider and model ID from settings, Anthropic by default.
- [x] System prompt: scope, and the "check stock first" flow.
- [x] Strands `Agent` with `MCPClient`, sending `X-User-Id` / `X-Session-Id` headers.
- [x] FastAPI `/invocations` endpoint.
- [x] AgentCore Runtime HTTP contract: `/invocations`, `/ping`, port 8080.
- [x] Identity in headers only: `X-User-Id` and `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` (both required); the body carries only the turn.
- [x] `create_purchase_order` filtered out until D3.
- [x] Verify Strands 1.61 works with mcp 2.1 `streamable_http_client` (main risk; do it first).
- [x] Tests with a scripted fake model (no LLM calls).
- Deps: `uv add --group agent "strands-agents[anthropic]" fastapi "uvicorn[standard]"`
- [x] Live check with a real model (needs `ANTHROPIC_API_KEY`): stock + catalogue question, and an out-of-scope request.
- Done early from D4: `/invocations` already returns `tool_calls`.

### D2: Session memory

- [x] SQLite `SessionRepository` (`memory/sessions.py`) plugged into Strands' `RepositorySessionManager`.
- [x] Each session stores its owner's `user_id`; another user's session is rejected with 403.
- [x] Retention: 30 days without activity (`AGENT_SESSION_RETENTION_DAYS`), purged on startup.
- [x] One turn at a time per session (409 while a turn is running).
- [x] Tests: history across turns and restarts, tool results kept, sessions isolated, purge, concurrency.
- [ ] Live check with a real model: a follow-up question that needs the previous turn, across a service restart.

### D3: Approval gate

- [x] `ApprovalHook` (`agent/approval.py`): `BeforeToolCallEvent` on `create_purchase_order` →
  `event.interrupt("po-approval", reason={lines, deliver_to})`.
- [x] `/invocations` takes `{"approvals": [{interrupt_id, decision}]}` and returns `pending_approvals`.
- [x] Approve runs the tool; decline sets `cancel_tool` with a message telling the model why.
- [x] Pending approvals mirrored in `agent.state`: a message while an order waits, or a decision
  when nothing waits, gets 409 (with the pending list so the UI can recover).
- [x] `create_purchase_order` exposed to the model; prompt updated (no text confirmation first).
- [x] Tests: interrupt before any PO exists; survives a restart; decline leaves no PO;
  faults on + approve gives exactly one PO; each order needs its own approval.
- [x] Live check with a real model: propose → approve, propose → decline, approve with faults on.
- [x] Fix: close the Anthropic client in the turn's own event loop ("Event loop is closed" on GC).
- Needs D2: the interrupt state is stored in the session.

### D4: Web UI

- [x] Static page (`agent/static/index.html`, plain JS, no build) served at `GET /` by the agent service.
- [x] `/invocations` already returns `tool_calls` (D1) and `pending_approvals` (D3).
- [x] User id input, session id from `crypto.randomUUID()`, "New session" button.
- [x] Per-turn tool-call log (input, status, result), tokens and latency.
- [x] Approval cards with Approve/Decline; one request decides every pending order; composer locked meanwhile.
- [x] Transcript kept in `localStorage` (no history endpoint); a 409 re-syncs the pending list from the server.
- [x] Live check in a browser: propose → approve, propose → decline, approve after a restart, another user → 403.
- On AgentCore the runtime only routes `/invocations` and `/ping`, so the page would be hosted separately (AGENTCORE.md).

### D5: User facts

- [x] Strands memory SDK: `SqliteFactStore` (`memory/facts.py`, `user_facts` table) implements `MemoryStore`;
  one value per key (`lab`, `team`, `delivery_location`), bound to the caller's `user_id`.
- [x] `MemoryManager` per request: profile injected on each new message (not saved in history);
  `search_memory`, `add_memory` and extraction off.
- [x] `remember_fact(key, value)` tool (`agent/profile.py`) writes through `memory_manager.add`; no user id in its schema.
- [x] Delivery location required: mock API (422), MCP tool schema, approval card; the hook sends an
  order without `deliver_to` back to the model. Prompt: use the saved location, else ask once and save it;
  a one-off location doesn't replace the saved one.
- [x] Tests: upsert, key/value validation, isolation, injection in a new session, nothing injected for
  another user, injection not stored in history, missing delivery location at each layer.
- [x] Fix: a tool batched with an order (e.g. `remember_fact`) showed as waiting for approval. Strands parks
  finished results until the interrupted batch resumes; a `FinishedTools` hook (`AfterToolCallEvent`)
  records them so the response shows them done, once.
- [x] Live check with a real model: "order gloves" → asked where → "Lab 3.12" → card shows it; new
  session → no question; another user → asked.
- On AgentCore the store is replaced by AgentCore Memory; `remember_fact` and the manager stay.

### D6: Evals

- [x] Fake supplies tools with the MCP server's real definitions, result models and error wording;
  contract test against the real MCP tools.
- [x] Harness: plays a case (messages, approve/decline, other user/session) through `/invocations` with
  the real model, a fresh SQLite and fake tools.
- [x] 14 cases: right tool, approval gate (propose/approve/decline), out of scope, read and write outage,
  user facts (collect, recall in a new session, one-off location, isolation, team).
- [x] `ScenarioChecks` (exact checks from case metadata) and `RubricJudge` (cheaper judge, rubric cases only).
- [x] Runner `python -m evals` (`--case`), summary, `evals/results/latest.json`, exit code.
- [x] Pipeline tests with scripted models (no LLM calls).
- [ ] First real run; commit `evals/results/latest.json`.
- Deps: `uv add --group evals strands-agents-evals` (done).

### D7: Docker compose

- [ ] One image per service, each installing only its dependency group; `linux/arm64`; agent on `0.0.0.0:8080`.
- [ ] Decide `transport_security` for the MCP server when it binds to `0.0.0.0`.

### D8: Observability

- [ ] `StrandsTelemetry` OTLP traces and metrics.
- [ ] Trace propagation over the MCP and HTTP hops.
- [ ] Custom counters: approvals, faults injected, MCP retries, duplicate POs blocked.
- [ ] Correlate logs with traces: add `trace_id`/`span_id` to log lines (a `logging.Filter` reading the current span); optionally ship logs to Loki.
- [ ] `otel-lgtm` in compose with a provisioned dashboard.
- Deps: the `[otel]` extra on `strands-agents`, plus OTel instrumentation for FastAPI and httpx.

### D9: Docs support

- [ ] README skeleton and setup instructions.
- [ ] AGENTCORE diagram and platform facts.
- [ ] Screenshots or recording of one full run, plus the eval output.

## Open decisions

- Short-term memory retention period (D2).
- Other idempotency-key strategies (see CLAUDE.md, Approval section).
- User identity on AgentCore: from a validated JWT, not the `X-User-Id` header (local version trusts the header).
- Auth between the agent service and the MCP server (none today; only the agent can reach it
  inside compose).
