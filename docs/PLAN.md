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
- [ ] Live check with a real model (needs `ANTHROPIC_API_KEY`): stock + catalogue question, and an out-of-scope request.
- Done early from D4: `/invocations` already returns `tool_calls`.

### D2: Session memory

- [ ] SQLite `SessionRepository` plugged into Strands' `RepositorySessionManager`.
- [ ] Each session stores its owner's `user_id`; requests for another user's session are rejected.
- [ ] Decide the retention period.

### D3: Approval gate

- [ ] `BeforeToolCallEvent` hook → `event.interrupt("po-approval", reason={PO summary})`.
- [ ] `/invocations` accepts `interruptResponse` and returns the pending approval.
- [ ] Approve runs the tool; decline sets `cancel_tool`.
- [ ] Tests: interrupt before any PO exists; survives a restart; decline leaves no PO;
  faults on + approve gives exactly one PO.
- Needs D2: the interrupt state is stored in the session.

### D4: Web UI

- [ ] Static page served by the agent service.
- [ ] `/invocations` response adds `pending_approval` (`tool_calls` already done in D1).

### D5: User facts

- [ ] `user_facts` table.
- [ ] Local `remember_fact` tool; `user_id` comes from the request context, never from model arguments.
- [ ] Facts injected into the system prompt.
- [ ] Isolation tests.

### D6: Evals

- [ ] Strands Evals cases and a runner; deterministic evaluators first, LLM judges only for fuzzy checks.
- [ ] Save results with `experiment.to_file`.
- Deps: `uv add --group dev strands-agents-evals` (or a separate `evals` group).

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

- Order of D4 (UI) and D5 (user facts).
- Short-term memory retention period (D2).
- Other idempotency-key strategies (see CLAUDE.md, Approval section).
- User identity on AgentCore: from a validated JWT, not the `X-User-Id` header (local version trusts the header).
- Auth between the agent service and the MCP server (none today; only the agent can reach it
  inside compose).
