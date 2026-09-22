# Reflection

Building this agent from scratch taught me that the prompt is an important piece but not the only
piece of the puzzle — most of the work was infrastructure. AgentCore's Gateway acts as the agent's
conductor, and I attached tools to it several ways: a Lambda function, an API Gateway REST API, tools
from the Strands framework, my own sandboxed code tool, and the browser tool. An agent is only as reliable as the tools
behind it, because the tools return real data instead of something the model invented.

One design decision worth calling out. The three order tools reach the agent through an API Gateway
target, which derives its schema from the OpenAPI export — and mine had no descriptions, so each tool
described itself as its own name (`desc: get_order`). That matters because `get_customer` and
`get_customer_orders` take the identical parameter, so the model had nothing to distinguish them by. I
fixed it with `toolOverrides` on the Gateway target, one API call, instead of API Gateway documentation
parts, which take five publish steps. The refund tool needed nothing, because its descriptions come
from a static MCP schema I control directly.

The challenge that cost me the most was a silent one. When I filled in my memory hook, the starter's
original `register_hooks` stub was still sitting below my version. Python keeps the last definition, so
both memory callbacks were never registered — no error, no traceback, memory just quietly did nothing.
Coming from Kotlin and Swift this was the sharpest surprise in the project, since both reject a
duplicate method at compile time — so it never occurred to me to look. I found it by parsing the file's
AST to list the class's methods and check for duplicates. Pylance's `reportRedeclaration` would have
flagged it too, and I had type checking enabled, so the lesson is to read the Problems panel, not just
the terminal.

For production, the browser tool concerns me most. After my browser test,
`list-browser-sessions` showed session `01M337MNK6…` still `READY` minutes after the request finished,
because `invoke()` constructs `AgentCoreBrowser` per request and never closes it. Every browsing
request strands a session that bills until it times out, and at real volume they would accumulate until
the concurrent-session quota is reached — which breaks *new* requests rather than the one that leaked.
I would own that lifecycle explicitly and close it in a `finally` block.
