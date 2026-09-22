"""Verify what the AgentCore Gateway actually exposes to the model.

Dev utility — not part of the graded agent. Uses the same code path main.py will
use in TODO 8 (MCPClient + streamable_http_client), so a pass here means the
Gateway connection and the tool schemas are both good before writing agent code.

Usage:
    uv run verify_gateway.py https://<alias>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp

    # or set it once
    export GATEWAY_URL=https://...
    uv run verify_gateway.py

Why this exists: the console shows the target's *filter* (e.g. "GET /*"), not the
resolved tools. Three things can independently be stale — the REST API, the
Gateway target, and what the model receives. This checks the last one, which is
the only one that matters to the agent.
"""

import json
import os
import sys
import textwrap
from collections import defaultdict

# `--full` prints complete tool descriptions instead of an 80-char preview.
# Worth it when verifying that toolOverrides actually landed.
FULL = "--full" in sys.argv

from mcp.client.streamable_http import streamable_http_client
from strands.tools.mcp.mcp_client import MCPClient

# What the project should expose: 3 order tools + 3 refund tools.
EXPECTED = {
    "get_order",
    "get_customer_orders",
    "get_customer",
    "initiate_refund",
    "check_refund_status",
    "get_return_label",
}


def spec_of(tool):
    """Return (name, description, inputSchema) defensively.

    Strands wraps MCP tools; `tool_spec` is the dict the model sees and
    `tool_name` is the flat name. Both are read via getattr so a wrapper change
    degrades to something printable instead of raising.
    """
    spec = getattr(tool, "tool_spec", None) or {}
    name = spec.get("name") or getattr(tool, "tool_name", None) or str(tool)
    desc = (spec.get("description") or "").strip()
    schema = spec.get("inputSchema") or {}
    # inputSchema is sometimes {"json": {...}} depending on the wrapper.
    if "json" in schema and isinstance(schema["json"], dict):
        schema = schema["json"]
    return name, desc, schema


def main() -> int:
    # First non-flag argument is the URL; fall back to $GATEWAY_URL.
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    url = (positional[0] if positional else os.environ.get("GATEWAY_URL", "")).strip()
    if not url:
        print("Pass the Gateway URL as an argument or set GATEWAY_URL.", file=sys.stderr)
        return 2
    if not url.endswith("/mcp"):
        print(f"warning: URL does not end in /mcp — got {url!r}", file=sys.stderr)

    print(f"Connecting: {url}\n")
    client = MCPClient(lambda: streamable_http_client(url=url))

    # Sync `with` only — MCPClient has no __aenter__. The tools returned reference
    # this open connection, so everything that uses them stays inside the block.
    with client:
        tools = client.list_tools_sync()

        by_target = defaultdict(list)
        flat_names = set()

        for t in tools:
            name, desc, schema = spec_of(t)
            # Gateway prefixes tools as "targetName___toolName".
            target, _, bare = name.rpartition("___")
            by_target[target or "(no target prefix)"].append((bare or name, desc, schema))
            flat_names.add(bare or name)

        for target in sorted(by_target):
            print(f"── {target} " + "─" * max(0, 58 - len(target)))
            for bare, desc, schema in sorted(by_target[target]):
                props = schema.get("properties") or {}
                required = schema.get("required") or []
                params = ", ".join(
                    f"{p}{'*' if p in required else ''}" for p in sorted(props)
                ) or "(none)"
                print(f"  {bare}")
                print(f"      params: {params}       (* = required)")
                if desc:
                    if FULL:
                        print(f"      desc ({len(desc)} chars):")
                        for line in textwrap.wrap(desc, 84):
                            print(f"        {line}")
                    else:
                        # Truncated for readable columns — pass --full to see it all.
                        print(f"      desc:   {desc.splitlines()[0][:80]}")
            print()

        print(f"total tools: {len(tools)}")
        missing = EXPECTED - flat_names
        extra = flat_names - EXPECTED
        if missing:
            print(f"MISSING {len(missing)}: {sorted(missing)}")
        if extra:
            print(f"extra (fine — e.g. gateway search): {sorted(extra)}")
        if not missing:
            print("All 6 expected tools present.")
        return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
