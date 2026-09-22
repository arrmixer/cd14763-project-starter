"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the project instructions and rubric for guidance.
Work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

app = BedrockAgentCoreApp()


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"

# Replace the placeholder strings with your actual AWS resource values.
# You collected these in the infrastructure setup section of the project instructions.
#
# GATEWAY_URL format: https://<alias>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
# This starter uses an unsigned MCP connection and therefore assumes the
# project Gateway is configured with the NONE authorizer.
# KB_ID       format: 10-character alphanumeric string from the KB console
# REGION:     your AWS region, e.g. "us-east-1"
# MEMORY_ID   format: shown in the AgentCore Memory console

GATEWAY_URL = "https://customersupportgateway-bct96guevn.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID       = "NOQMQE3OZM"
REGION      = "us-east-1"
MEMORY_ID   = "CustomerSupportMemory-Gcb76342Rz"

model_id = "global.amazon.nova-2-lite-v1:0"
model = BedrockModel(model_id=model_id)
memory_client = MemoryClient(region_name=REGION)

_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# Implement get_namespaces() to return a dict mapping strategy type to
# namespace template string.
#
# Steps:
#   1. Call mem_client.get_memory_strategies(memory_id) to get strategy list
#   2. Read the namespace from strategy["namespaceTemplates"][0].
#      For compatibility with older AgentCore responses, fall back to
#      strategy["namespaces"][0] when namespaceTemplates is absent.
#
# Example output:
#   { "SEMANTIC": "cs_agent/{actorId}/facts",
#     "USER_PREFERENCE": "cs_agent/{actorId}/preferences" }

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)
    namespaces = {}
    for strategy in strategies:
        # namespaceTemplates is current; namespaces is the legacy field. Rubric
        # requires handling both. (We verified the service populates BOTH when you
        # send `namespaces` at create time, so either works on our resource.)
        templates = strategy.get("namespaceTemplates") or strategy.get("namespaces") or []
        if templates:
            # NOTE: [0] discards any additional namespaces, and keying on type means
            # two strategies of the SAME type would collapse — last one wins, silently.
            # Fine here: two distinct types, one namespace each.
            namespaces[strategy["type"]] = templates[0]
    return namespaces

# Implement MemoryHook, a HookProvider subclass that adds long-term memory.
#
# The class needs:
#   __init__(self, actor_id, session_id, memory_client, memory_id)
#     — store all four as instance attributes
#     — call get_namespaces() and store the result as self.namespaces
#
#   retrieve_customer_context(self, event: MessageAddedEvent)
#     — only runs for plain-text user messages (not tool results)
#     — for each strategy namespace, call memory_client.retrieve_memories(
#          memory_id, namespace (formatted with actorId), query, top_k=5)
#     — collect non-empty memory texts tagged with their strategy type
#     — if any memories found, prepend them to the user message as:
#          "Customer Context:\n<memories>\n\n<original_message>"
#
#   save_support_interaction(self, event: AfterInvocationEvent)
#     — walk the message list backwards to find the last plain-text user
#       query and the last assistant response
#     — call memory_client.create_event(memory_id, actor_id, session_id,
#          messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")])
#
#   register_hooks(self, registry: HookRegistry)
#     — register retrieve_customer_context on MessageAddedEvent
#     — register save_support_interaction on AfterInvocationEvent

class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_id = memory_id
        self.memory_client = memory_client
        # One network round-trip per hook instance (i.e. per request). Cached here
        # so the two callbacks don't each re-fetch it.
        self.namespaces = get_namespaces(memory_client, memory_id)
        # Guards the feedback loop — see retrieve_customer_context.
        self._original_user_text: str | None = None

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        messages = event.agent.messages
        if (
            not messages
            or messages[-1]["role"] != "user"
            or "text" not in messages[-1]["content"][0]
        ):
            return

        user_query = messages[-1]["content"][0]["text"]
        # Remember the customer's ACTUAL words before we mutate the slot, so
        # save_support_interaction persists the question rather than our injection.
        self._original_user_text = user_query

        try:
            found = []
            for strategy_type, template in self.namespaces.items():
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=template.format(actorId=self.actor_id),
                    query=user_query,
                    top_k=5,
                )
                for memory in memories:
                    if not isinstance(memory, dict):
                        continue
                    text = (memory.get("content") or {}).get("text", "").strip()
                    if text:
                        found.append(f"[{strategy_type}] {text}")

            if found:
                block = "\n".join(found)
                messages[-1]["content"][0]["text"] = (
                    f"Customer Context:\n{block}\n\n{user_query}"
                )
                logger.info("Retrieved %d memories for %s", len(found), self.actor_id)

        except Exception as exc:
            # Deliberately non-fatal: a memory outage should degrade personalisation,
            # not break the customer's request.
            logger.error("Memory retrieval failed: %s", exc)

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        try:
            messages = event.agent.messages
            customer_query = self._original_user_text
            agent_response = None

            for msg in reversed(messages):
                if msg["role"] == "assistant" and not agent_response:
                    content = msg["content"]
                    if isinstance(content, list) and content:
                        agent_response = content[0].get("text", "")
                elif (
                    msg["role"] == "user"
                    and not customer_query
                    and "text" in msg["content"][0]
                ):
                    customer_query = msg["content"][0]["text"]
                    break

            if customer_query and agent_response:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")],
                )
                logger.info("Saved interaction for %s", self.actor_id)

        except Exception as exc:
            logger.error("Memory save failed: %s", exc)

# Implement search_knowledge_base(query) using the @tool decorator.
#
# Steps:
#   1. Guard: if KB_ID is empty return "Knowledge base not configured."
#   2. Call _bedrock_runtime.retrieve(
#          knowledgeBaseId=KB_ID,
#          retrievalQuery={"text": query}
#      )
#   3. Extract resp["retrievalResults"]; return a message if empty
#   4. Join the text chunks with "\n---\n" and return the result
#
# The docstring is the tool description — the model uses it to decide when
# to call this tool, so keep it clear and accurate.
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    For calculating a specific customer's discount, use
    calculate_loyalty_discount instead — this tool only describes the rules.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID or KB_ID.startswith("<"):
        return (
            "The product knowledge base is not configured, so I cannot look up "
            "product specifications, return policies, warranty terms, or loyalty "
            "program details right now."
        )

    resp = _bedrock_runtime.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": query},
    )
    results = resp.get("retrievalResults", [])
    if not results:
        return f"No information found for: {query}"

    return "\n---\n".join(r["content"]["text"] for r in results)

# Implement calculate_loyalty_discount() using the @tool decorator.
#
# The tool must:
#   1. Build a self-contained Python code string that:
#        • Defines earn_rates: {"standard": 1, "device": 2, "fresh": 5}
#        • Defines tier_rates: {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
#        • Calculates points_redeemed (floor to nearest 500, cap at 50% of order)
#        • Calculates tier_discount (applied to subtotal after points)
#        • Calculates final_total, total_savings, points_earned, remaining_points
#        • Prints a JSON result dict
#   2. Execute the code with code_session(REGION).invoke("executeCode", {...})
#      using language="python" and clearContext=True
#   3. Return the first result event as a JSON string
#   4. Include a fallback that computes only the tier discount if the
#      Code Interpreter is unavailable

@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    code = f"""
import json

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

loyalty_points = {loyalty_points}
tier           = {tier!r}
order_total    = {order_total}
category       = {product_category!r}

POINTS_PER_DOLLAR = 100     # 100 points = $1 discount
MIN_REDEMPTION    = 500     # catalog minimum redemption
REDEEM_CAP_PCT    = 0.50    # points may cover at most 50% of the order

# Cap by value FIRST, then floor to the 500-point granularity, so rounding
# can never push the redemption above the 50% cap.
max_points_by_cap = int(order_total * REDEEM_CAP_PCT * POINTS_PER_DOLLAR)
redeemable        = min(loyalty_points, max_points_by_cap)
points_redeemed   = (redeemable // MIN_REDEMPTION) * MIN_REDEMPTION
if points_redeemed < MIN_REDEMPTION:
    points_redeemed = 0

points_value = points_redeemed / POINTS_PER_DOLLAR

# Tier discount applies to the subtotal AFTER points are deducted.
subtotal_after_points = round(order_total - points_value, 2)
tier_rate             = tier_rates.get(tier, 0.0)
tier_discount         = round(subtotal_after_points * tier_rate, 2)

final_total   = round(subtotal_after_points - tier_discount, 2)
total_savings = round(points_value + tier_discount, 2)

# Points are earned on what the customer actually pays.
points_earned = int(final_total * earn_rates.get(category, 1))

# Post-redemption balance. Newly earned points are reported separately rather
# than folded in, so both numbers stay legible to the customer.
remaining_points = loyalty_points - points_redeemed

print(json.dumps({{
    "tier": tier,
    "product_category": category,
    "order_total": round(order_total, 2),
    "points_redeemed": points_redeemed,
    "points_value_usd": round(points_value, 2),
    "subtotal_after_points": subtotal_after_points,
    "tier_discount_pct": round(tier_rate * 100, 2),
    "tier_discount_usd": tier_discount,
    "final_total": final_total,
    "total_savings": total_savings,
    "points_earned": points_earned,
    "remaining_points": remaining_points,
}}, indent=2))
"""

    try:
        with code_session(REGION) as code_client:
            response = code_client.invoke(
                "executeCode",
                {
                    "code": code,
                    "language": "python",
                    # Fresh sandbox per call: no variable from a previous
                    # invocation survives, so one customer's state cannot leak
                    # into another's.
                    "clearContext": True,
                },
            )

            for event in response["stream"]:
                return json.dumps(event["result"])

        return json.dumps({"error": "code interpreter returned no events"})

    except Exception as exc:
        # Rubric #22 — degraded but CORRECT: tier discount only, no points
        # redeemed, and say so, so the model can tell the customer.
        logger.error("Code Interpreter unavailable, using fallback: %s", exc)
        tier_rate = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}.get(tier, 0.0)
        tier_discount = round(order_total * tier_rate, 2)
        return json.dumps(
            {
                "points_redeemed": 0,
                "tier_discount_pct": round(tier_rate * 100, 2),
                "tier_discount_usd": tier_discount,
                "final_total": round(order_total - tier_discount, 2),
                "remaining_points": loyalty_points,
                "degraded": True,
                "note": (
                    "Calculated with the tier discount only — the secure calculator "
                    "was unavailable, so loyalty points were not redeemed."
                ),
            },
            indent=2,
        )
SYSTEM_PROMPT = """You are a customer support agent for an Amazon store.

  Your job is to help customers with orders and deliveries, refunds and returns, product
  questions, and loyalty rewards. Answer using your tools. If a request falls outside those
  areas, say so politely and offer what you can help with instead.

  GROUNDING — this is your most important rule:
  - Every fact you state must come from a tool result or from the customer's own message.
  - Never invent order numbers, tracking numbers, dates, prices, point balances, tiers,
    policies or product details. If a tool did not return it, you do not know it.
  - If you cannot obtain something, say so plainly and say which detail you need. Asking
    the customer for their order ID is always better than guessing one.
  - Do not perform arithmetic yourself. Use the calculator tool for any figure a customer
    might act on.

  CHOOSING TOOLS:
  - To look up an order, a customer's order history, or a customer's profile (their name,
    points balance and tier), use the order and customer lookup tools.
  - To start a refund, check a refund's status, or produce a return label, use the refund
    tools.
  - search_knowledge_base answers questions about products, return and refund policies,
    warranty terms, the loyalty program RULES, and order status definitions.
  - calculate_loyalty_discount COMPUTES a specific customer's discount. Use it whenever a
    customer asks what they would pay. Never quote a tier percentage from the knowledge
    base as if it were a calculated total.
  - Use the browser tool only when the answer is on a public web page and is not available
    from any other tool.

  CUSTOMER CONTEXT:
  - When a "Customer Context:" block appears at the start of the customer's message, it
    holds facts and preferences retrieved from earlier conversations. Use it to personalise
    your reply, and do not ask the customer to repeat anything it already tells you.
  - If NO "Customer Context:" block is present, this is your first conversation with this
    customer. Greet them as new. Do not say "welcome back", do not imply prior contact, and
    do not invent remembered details.

  STYLE:
  - Be concise and concrete. Quote exact figures — order totals, tracking numbers, discount
    amounts — rather than paraphrasing them.
  - If a tool reports that it is degraded or unavailable, tell the customer plainly what you
    could not do instead of guessing at the answer.
  """


# Implement the invoke() function decorated with @app.entrypoint.
#
# Steps:
#   1. Extract user_input, actor_id, and session_id from the payload
#      (generate a UUID if session_id is missing)
#   2. Instantiate MemoryHook for this actor/session
#   3. Instantiate AgentCoreBrowser(region=REGION)
#   4. Build the tools list: [search_knowledge_base, calculate_loyalty_discount,
#                              agent_core_browser.browser]
#   5. Connect to the Gateway via MCPClient, load gateway_tools, extend tools list
#   6. Create and invoke the Agent with all tools, hooks, and system_prompt
#   7. Return the text from the first content block of the response
#   8. Handle exceptions gracefully

@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    try:
        user_input = payload.get("prompt", "")
        if not user_input:
            return "I did not receive a message. What can I help you with?"

        # Identity: NO plausible fallback. A real-looking default would serve one
        # customer's memories to another — the sentinel is obviously not a customer id.
        actor_id = payload.get("customer_id")
        if not actor_id:
            actor_id = "anonymous-no-customer-id"
            logger.warning("No customer_id in payload; memory scoped to %s", actor_id)

        session_id = payload.get("session_id") or uuid.uuid4().hex

        memory_hook = MemoryHook(
            actor_id=actor_id,
            session_id=session_id,
            memory_client=memory_client,
            memory_id=MEMORY_ID,
        )

        # region defaults to None, so pass it explicitly or browser calls fail.
        agent_core_browser = AgentCoreBrowser(region=REGION)

        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]

        gateway_client = MCPClient(lambda: streamable_http_client(url=GATEWAY_URL))

        # The `with` block MUST enclose the agent call: tools returned by
        # list_tools_sync() reference this open connection. Closing it first
        # yields tools that fail at invocation time.
        with gateway_client:
            gateway_tools = gateway_client.list_tools_sync()
            tools.extend(gateway_tools)
            logger.info(
                "actor=%s session=%s tools=%d (%d from gateway)",
                actor_id, session_id, len(tools), len(gateway_tools),
            )

            agent = Agent(
                model=model,
                system_prompt=SYSTEM_PROMPT,
                tools=tools,
                hooks=[memory_hook],
            )
            response = agent(user_input)

        # "text" is optional on a ContentBlock — the first block can be toolUse,
        # reasoningContent, etc. Scan for the first text block instead of assuming
        # index 0, so a non-text block can't raise KeyError.
        blocks = response.message.get("content") or []
        text = next(
            (b["text"] for b in blocks if isinstance(b, dict) and "text" in b),
            None,
        )
        return text or "I could not produce a response for that request."

    except Exception as exc:
        logger.exception("Invocation failed")
        return (
            "I ran into a problem handling that request and could not complete it. "
            f"Please try again in a moment. ({type(exc).__name__})"
        )


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()
