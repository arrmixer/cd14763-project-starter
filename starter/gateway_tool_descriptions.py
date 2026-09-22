"""Build the order-tracker Gateway target configuration, with real tool descriptions.

Why this exists
---------------
An API Gateway target derives its tool schema from the REST API's OpenAPI export.
Our methods have no summary/description, so the export gives each tool a
description equal to its own name ("desc: get_order") — useless to a model, which
has only the description to decide *when* to call a tool.

Two ways to fix it:
  1. API Gateway documentation parts — 5 publish steps, two of which silently
     no-op if skipped (create-documentation-version bound to the stage, then
     synchronize-gateway-targets).
  2. toolOverrides on the Gateway target — one call. What this file builds.

`toolOverrides` entries use path+method to identify the operation, and
name+description to set what the model sees.

Usage
-----
    uv run gateway_tool_descriptions.py <rest-api-id> > /tmp/order-target.json
"""

import json
import sys

STAGE = "prod"

# Descriptions are written to DISAMBIGUATE, not merely to exist. get_customer and
# get_customer_orders take the identical parameter (customer_id), so each says
# explicitly what it does and does not return. ID formats live in the prose
# because per-parameter docs do not reach the model — only the description does.
TOOL_OVERRIDES = [
    {
        "name": "get_order",
        "path": "/orders/{order_id}",
        "method": "GET",
        "description": (
            "Look up ONE specific order by its order ID (format ORD-XXX, e.g. ORD-001). "
            "Returns order status, items, total, tracking number, carrier, and estimated "
            "or actual delivery date. Use this when the customer names or asks about a "
            "particular order. Do not use it to list a customer's orders."
        ),
    },
    {
        "name": "get_customer_orders",
        "path": "/customers/{customer_id}/orders",
        "method": "GET",
        "description": (
            "List ALL orders belonging to a customer, by customer ID (format CUST-XXX, "
            "e.g. CUST-123). Returns an array of that customer's orders. Use this when "
            "the customer asks about their order history or 'my orders' without naming a "
            "specific order ID."
        ),
    },
    {
        "name": "get_customer",
        "path": "/customers/{customer_id}",
        "method": "GET",
        "description": (
            "Look up a customer's PROFILE by customer ID (format CUST-XXX, e.g. "
            "CUST-123). Returns their name, loyalty points balance, and tier (Silver, "
            "Gold, or Platinum). Use this when you need the customer's tier or points - "
            "for example before calculating a loyalty discount. This does NOT return any "
            "orders."
        ),
    },
]


def build(rest_api_id: str) -> dict:
    return {
        "mcp": {
            "apiGateway": {
                "restApiId": rest_api_id,
                "stage": STAGE,
                "apiGatewayToolConfiguration": {
                    # Expose every GET route...
                    "toolFilters": [{"filterPath": "/*", "methods": ["GET"]}],
                    # ...then override the three with usable descriptions.
                    "toolOverrides": TOOL_OVERRIDES,
                },
            }
        }
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        sys.exit(2)
    print(json.dumps(build(sys.argv[1]), indent=2))
