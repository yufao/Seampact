# Pi host

This adapter embeds the official `@earendil-works/pi-agent-core` `Agent` at
version **1.5.2**. `package-lock.json` pins its complete npm dependency tree. It
uses no coding CLI, TUI, second Agent, provider client, model endpoint or model
credential. Pi owns the transcript, argument validation, model/tool turn loop,
sequential tool execution and native lifecycle events. Python supplies context
preparation, the existing model transport, tool effects, hooks and accounting.

Install once in this directory with Node.js >=22.19.0:

```powershell
npm ci --ignore-scripts --no-audit --no-fund
npm test
```

`node_modules/` is local and ignored. `node bridge.mjs` reads UTF-8 JSONL from
stdin and writes only JSONL protocol messages to stdout. One process handles one
run; no tool operation is retried or replayed by this adapter.

## Protocol

The process first emits `{"type":"ready","identity":{...}}`; identity contains
the actual installed Pi package name and version. Python starts a run with:

```json
{"type":"start","messages":[{"role":"system","content":"..."},{"role":"user","content":"..."}],"tools":[],"model":{"id":"configured-model","contextWindow":128000,"maxTokens":8192}}
```

Messages and tools use the existing OpenAI chat wire shapes. The optional
`systemPrompt` is used only when the supplied history has no leading system
message. Endpoints and credentials stay in Python.

Pi invokes Python through `{"type":"request","id":"1","method":"...","params":{...}}`:

| Method | Parameters | Successful reply `result` |
| --- | --- | --- |
| `context` | `messages`, `tools`, safe `model` metadata | `{messages,tools}`; messages must begin with the current system message |
| `model` | `messages`, `tools`, safe `model` metadata | Complete OpenAI chat completion response, including `choices` and optional `usage` |
| `tool` | `name`, `arguments` object, `call_id` | `{content:string,isError:boolean}`; optional `details`, or Pi text/image content array |

Reply with `{"type":"reply","id":"1","result":...}` or
`{"type":"reply","id":"1","error":{"message":"...","type":"..."}}`.
For model text streaming, send any number of
`{"type":"model_delta","id":"1","text":"..."}` records before the final
reply. The final assistant message remains authoritative. Model failures become
Pi error messages, never a fresh model retry. Tool failures remain Pi error tool
results. A tool error with `fatal:true` aborts the run.

Python may add `_seampact_revision_reason` to a successful final model reply
when its complete delivery checks reject the still-undelivered answer. The
bridge queues that bounded public feedback using the same Pi Agent's native
`followUp` method. Pi retains the draft and prior tool receipts and requests
one further business-model answer; no tool operation is replayed. Python supplies
no tools for that repair turn, rejects any requested tool calls before execution,
and rechecks the revised answer before publishing text or files. A second
revision request is invalid. Delivery exhaustion publishes neither answer.

The Python hook actions are `deny_operation` at `before_tool` and
`revise_delivery` at complete `before_delivery`; both take a public `reason`.
The former returns one native Pi error tool result and lets the Agent continue.
After a denial the same optional check is enforced for subsequent applicable
tools until a rechecked operation is allowed and successfully executed. Failed
effects and unrelated tools leave that obligation pending; after success fresh
operations are optional again. The tool payload exposes `operation_required_hooks` and
`revision_required_hooks` before evaluating applicability, so a check can retain
its tool scope even when later evidence is unavailable. Complete delivery
payloads expose `revision_remaining` (1 then 0), `revision_recheck`, and
`revision_required_hooks`. Rejecting delivery checks are enforced again even if
their original applicability or optional selection changes. Enforced checks must
return an explicit `allow`, `replace`, or rejecting action; an absent assessment
fails closed. Existing `block` remains a terminal stop. Checks own removal of
private evidence from feedback; the host only bounds text and control characters.

Task traces record `scoped_operation_denied`, `scoped_operation_cleared`, `delivery_revision_requested`,
`delivery_revision_accepted` or `delivery_revision_exhausted`, plus each hook's
`enforced_recheck` flag. Pending files, including additions by stateful required
delivery hooks, remain attached to the checked revision.

Normal chat streams expose phase metadata and approved delivery events only.
Persisted continuation wire/context exclude withheld answer candidates and
transient revision system feedback, including while repair is in progress.
Only withheld candidate messages are retained in the task's existing research
detail under `delivery_revision.withheld_messages`; there is no duplicate raw
conversation history. Later chats retain real tool receipts and approved answers.

Each native Pi lifecycle event is emitted as
`{"type":"event","event":{...}}`. Only `message_end` adds
`event.wire_message` in OpenAI form. Pi may reject unknown tools or invalid
arguments before asking Python to execute them; Python must persist these native
error tool messages too, deduplicating by tool call ID. These event records are
execution evidence; user delivery continues through Python's existing hooks.

`{"type":"abort"}` aborts Pi's active request and stops subsequent tools. Python
also cancels its current model request or capability through its existing run
control; Pi's abort alone does not claim a remote side effect has stopped.
Replies arriving after abort are discarded. Closing stdin aborts the process.

The run ends with `{"type":"done","identity":{...},"messages":[...],"context_messages":[...],"aborted":false}`,
optionally including `error`. `messages` is Pi's own retained state;
`context_messages` is the latest prepared context plus completed messages. Python
keeps its continuation-safe wire history and archives independently. On interruption,
Python closes pending calls with unknown outcomes before a later user turn;
the bridge never resumes an incomplete side effect.

Upstream: [Pi agent core](https://github.com/earendil-works/pi/tree/v1.5.2/packages/agent).
