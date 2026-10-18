# Agent API

The agent service (`src/supplies_agent/agent/`) is what clients call: the web UI, evals, curl. It follows the
[AgentCore Runtime HTTP contract](https://strandsagents.com/docs/user-guide/sdk/deploy/deploy_to_bedrock_agentcore/python/#option-b-custom-agent),
so the same container runs locally and on AgentCore. It reaches the supplies backend only through the
[MCP server](MCP.md).

- Base URL: `http://127.0.0.1:8080` (`http://localhost:8080` in compose)
- Interactive docs: `GET /docs` (OpenAPI, from FastAPI)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/invocations` | One agent invocation: a message, or decisions on pending purchase orders |
| `GET` | `/ping` | Health check (`{"status": "Healthy"}`) |
| `GET` | `/` | The web UI (local only; AgentCore routes only `/invocations` and `/ping`) |

Error examples are real responses. Success examples show the shape with a typical reply; the model's wording
varies run to run.

## Identity headers

Who is calling and in which conversation travel in headers, never in the body. Both are required.

| Header | Meaning |
|---|---|
| `X-User-Id` | The user. Owns sessions and saved facts |
| `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` | The conversation. AgentCore sets it from `runtimeSessionId`; locally the client sets it |

Both must match `^[A-Za-z0-9_.-]{1,100}$`, because they end up in keys and logs. Locally `X-User-Id` is taken on
trust; on AgentCore the user would come from a validated JWT.

A session belongs to the user who created it. Another user using the same session id gets a 403.

## `POST /invocations`

The body is **either** a message **or** approval decisions, never both (unknown fields are rejected):

| Field | Type | Notes |
|---|---|---|
| `message` | string, 1–4000 | A new user message |
| `approvals` | 1–10 × `{interrupt_id, decision}` | `decision`: `approve` \| `decline`. One per pending order |

### Response (JSON)

| Field | Type | Notes |
|---|---|---|
| `reply` | string | The assistant's final message |
| `stop_reason` | string | `end_turn` normally; `interrupt` when an order waits for approval |
| `tool_calls` | list of `{name, input, status, result}` | This invocation's tool calls, in order. `status` is `success` / `error`, or `null` while a call waits for approval. `result` is the tool's text, truncated to 2000 characters |
| `pending_approvals` | list of `{interrupt_id, lines, deliver_to}` | Orders waiting for a decision |
| `usage` | `{input_tokens, output_tokens}` | Tokens used by this invocation |

### Example: a message that proposes an order

```bash
curl -s http://127.0.0.1:8080/invocations \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: alice' -H 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: s1' \
  -d '{"message": "Order 2 boxes of nitrile gloves, size M."}'
```

```json
{
  "reply": "Please approve or decline this purchase order.",
  "stop_reason": "interrupt",
  "tool_calls": [
    {"name": "check_stock", "input": {"query": "nitrile gloves"}, "status": "success", "result": "{\"matches\": [{\"sku\": \"LC-GLOVE-M\", ... \"quantity\": 0 ...}], \"hint\": null}"},
    {"name": "search_catalog", "input": {"query": "nitrile gloves"}, "status": "success", "result": "{\"items\": [{\"sku\": \"LC-GLOVE-M\", ...}], ...}"},
    {"name": "create_purchase_order", "input": {"lines": [{"sku": "LC-GLOVE-M", "quantity": 2}], "deliver_to": "Lab 3.12"}, "status": null, "result": null}
  ],
  "pending_approvals": [
    {"interrupt_id": "<opaque id>", "lines": [{"sku": "LC-GLOVE-M", "quantity": 2}], "deliver_to": "Lab 3.12"}
  ],
  "usage": {"input_tokens": 9150, "output_tokens": 210}
}
```

The model's own text replaces the default reply when it writes one. The order has **not** been placed: the
`create_purchase_order` call is paused until the user decides. The pending approval is saved with the session,
so it survives a restart of the service.

### Example: approving

```bash
curl -s http://127.0.0.1:8080/invocations \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: alice' -H 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: s1' \
  -d '{"approvals": [{"interrupt_id": "<opaque id>", "decision": "approve"}]}'
```

```json
{
  "reply": "Done: purchase order PO-000001 for 2 packs of nitrile gloves (size M) from LabCore Direct, 24.00 EUR, delivered to Lab 3.12.",
  "stop_reason": "end_turn",
  "tool_calls": [
    {"name": "create_purchase_order", "input": {"lines": [{"sku": "LC-GLOVE-M", "quantity": 2}], "deliver_to": "Lab 3.12"}, "status": "success", "result": "{\"po_number\": \"PO-000001\", ... \"note\": \"Order placed.\"}"}
  ],
  "pending_approvals": [],
  "usage": {"input_tokens": 4800, "output_tokens": 60}
}
```

With `"decision": "decline"` the tool is cancelled, nothing is created, and the model is told: *"The user
declined this purchase order, so it was not placed. Tell the user it was not placed, and do not place it again
unless they ask."*

### Approval rules

- While an order waits, the session accepts **only** a decision on it. A message (even "yes") gets a 409; the
  model never decides whether the user approved.
- Send exactly one decision for each pending order (each `interrupt_id` from `pending_approvals`).
- Every order is approved on its own; approvals are never remembered.
- An order without `deliver_to` is never shown for approval: the model is sent back to ask for a location.

## Streaming

Send `Accept: text/event-stream` to get the same invocation as server-sent events. Each event has a name and
JSON data:

| Event | Data | When |
|---|---|---|
| `text` | `{"text": "..."}` | A piece of the model's text as it is written |
| `tool_start` | `{"id", "name", "input"}` | A tool call starts |
| `tool_end` | `{"id", "status", "result"}` | A tool call finishes (`result` truncated to 2000 characters) |
| `done` | the full JSON response above | Last event on success; the source of truth |
| `error` | `{"status", "detail"}` | Last event if the invocation fails after streaming started |

```bash
curl -s -N http://127.0.0.1:8080/invocations \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -H 'X-User-Id: alice' -H 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: s2' \
  -d '{"message": "Do we have PBS in stock?"}'
```

```text
event: tool_start
data: {"id":"tooluse_...","name":"check_stock","input":{"query":"PBS"}}

event: tool_end
data: {"id":"tooluse_...","status":"success","result":"{\"matches\": [...], \"hint\": null}"}

event: text
data: {"text":"Yes. We have 6 packs"}

event: text
data: {"text":" of PBS 1X, pH 7.4, sterile ..."}

event: done
data: {"reply":"Yes. We have 6 packs of PBS ...","stop_reason":"end_turn","tool_calls":[...],"pending_approvals":[],"usage":{...}}
```

Text written before a tool call is narration ("Let me check…"); the UI replaces it, as the JSON `reply`
contains only the final message.

Requests that will be refused (403, 409, 503 below) are refused **before** the stream starts, with a normal
HTTP status and JSON body. Once streaming has started, the status is already 200, so a later failure arrives as
the final `error` event:

```text
event: error
data: {"status":500,"detail":"Something went wrong on our side. Please try again."}
```

If the client disconnects mid-stream, the invocation still finishes and its outcome (including any pending
approval) is saved in the session.

## Errors

All error bodies are `{"detail": ...}`.

| Status | When | `detail` |
|---|---|---|
| 422 | Missing or invalid headers, or invalid body (FastAPI validation) | List of validation errors, e.g. `"String should match pattern '^[A-Za-z0-9_.-]{1,100}$'"` or `"send either 'message' or 'approvals', not both"` |
| 403 | The session belongs to another user | `"This session belongs to another user. Start a new session."` |
| 409 | A message while an order waits | `{"message": "A purchase order is waiting for your decision. Approve or decline it first.", "pending_approvals": [...]}` |
| 409 | Decisions when nothing waits | `{"message": "No purchase order is waiting for approval.", "pending_approvals": []}` |
| 409 | Decisions that don't match the pending orders | `{"message": "Send exactly one decision for each purchase order that is waiting.", "pending_approvals": [...]}` |
| 409 | Another invocation for this session is still running | `"A previous message in this session is still being processed. Wait for it to finish, then retry."` |
| 503 | The MCP server is unreachable | `"The supplies tools are unavailable right now. Please try again shortly."` |
| 503 | The model provider is still throttling after Strands' own retries | `"The assistant is busy right now. Please try again in a moment."` |

The approval 409s include `pending_approvals`, so a client can show the waiting orders again.

Anything unexpected in the JSON path is a plain `500 Internal Server Error` (text, not JSON); in a stream it is
the `error` event shown above.

Failures **inside** a run (the supplies API down, a rejected order) are not HTTP errors. The tool returns an error
message to the model ([MCP errors](MCP.md#errors)), and the model explains it in the `reply`.

## Related

- Configuration (`AGENT_*` variables): [README appendix](../README.md#configuration)
- How approval and memory work: [README §3](../README.md#3-agent-design)
