"""Build the refund-processor Gateway target configuration.

Companion to gateway_tool_descriptions.py, which builds the *other* target.

Why this exists
---------------
A Lambda target cannot derive its schema from anything — there is no OpenAPI
export to read — so the tool schema must be supplied by hand as
`toolSchema.inlinePayload`. That payload already lives in `lambda/lambda_schema`,
so this file only wraps it in the target-configuration envelope rather than
restating it, which keeps one source of truth for the three refund tools.

The trade-off versus the API Gateway target is recorded in
`docs/gateway-runbook.md`: hand-written schema means exact control and no export
artifacts (the stray `basePath` parameter), at the cost of writing it yourself.

Usage
-----
    uv run gateway_refund_target.py <lambda-arn> > /tmp/refund-target.json
"""

import json
import pathlib
import sys

SCHEMA_FILE = pathlib.Path(__file__).parent / "lambda" / "lambda_schema"


def build(lambda_arn: str) -> dict:
    tools = json.loads(SCHEMA_FILE.read_text())
    return {
        "mcp": {
            "lambda": {
                "lambdaArn": lambda_arn,
                "toolSchema": {"inlinePayload": tools},
            }
        }
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: uv run gateway_refund_target.py <lambda-arn>", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(build(sys.argv[1]), indent=2))
