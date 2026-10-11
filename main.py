"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the step-by-step solution files and INSTRUCTIONS.md for guidance.
Do NOT copy the solution directly — work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
from chardet import registry
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
from contextlib import ExitStack

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── TODO 1 — App Initialisation ───────────────────────────────────────────────
# Create a BedrockAgentCoreApp instance.
# This registers the ASGI server for AgentCore deployment.
# There must be exactly one instance per deployment.
#
# Hint: app = BedrockAgentCoreApp()

# TODO: Create the BedrockAgentCoreApp instance
app = BedrockAgentCoreApp()  # Replace this line


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── TODO 2 — Configuration ────────────────────────────────────────────────────
# Replace the placeholder strings with your actual AWS resource values.
# You collected these in Part 1 of the INSTRUCTIONS.
#
# GATEWAY_URL format: https://<alias>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
# KB_ID       format: 10-character alphanumeric string from the KB console
# REGION:     your AWS region, e.g. "us-east-1"
# MEMORY_ID   format: shown in the AgentCore Memory console

GATEWAY_URL = "https://customersupportgateway-gmaadbzgyz.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"   # TODO: Replace with your Gateway URL
KB_ID       = "OU4UWSD0LV"          # TODO: Replace with your Knowledge Base ID
REGION      = "us-east-1"        # TODO: Replace with your AWS region
MEMORY_ID   = "CustomerSupportMemory-oMbWw82sxh"        # TODO: Replace with your Memory ID


# ── TODO 3 — Model and Clients ────────────────────────────────────────────────
# Create:
#   1. A BedrockModel using model_id "global.amazon.nova-2-lite-v1:0"
#   2. A MemoryClient with region_name=REGION
#   3. A boto3 client for the "bedrock-agent-runtime" service in REGION
#
# Hint: model = BedrockModel(model_id=model_id)

model_id = "global.amazon.nova-2-lite-v1:0"

# TODO: Create the BedrockModel instance
model = BedrockModel(model_id=model_id)  # Replace this line

# TODO: Create the MemoryClient instance
memory_client = MemoryClient(region_name=REGION)  # Replace this line

# TODO: Create the boto3 bedrock-agent-runtime client
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)  # Replace this line


# ── TODO 4 — Namespace Helper ─────────────────────────────────────────────────
# Implement get_namespaces() to return a dict mapping strategy type to
# namespace template string.
#
# Steps:
#   1. Call mem_client.get_memory_strategies(memory_id) to get strategy list
#   2. Return a dict: { strategy["type"]: strategy["namespaces"][0] for each strategy }
#
# Example output:
#   { "SEMANTIC": "cs_agent/{actorId}/facts",
#     "USER_PREFERENCE": "cs_agent/{actorId}/preferences" }

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    # TODO: Implement this function
    strategies = mem_client.get_memory_strategies(memory_id)
    return {s["type"]: s["namespaces"][0] for s in strategies}


# ── TODO 5 — Memory Hook ──────────────────────────────────────────────────────
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

    def __init__(self, actor_id, session_id, memory_client, memory_id):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(memory_client, memory_id)
    
        # TODO: Store actor_id, session_id, memory_id, memory_client as attributes
        # TODO: Call get_namespaces() and store the result as self.namespaces
        

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        try:
            messages = event.agent.messages
            if not messages:
                return
            last = messages[-1]
            content = last.get("content", [])

            # Only plain-text user messages. Strands also sends tool results
            # with role "user", so reject anything containing a toolResult.
            if last.get("role") != "user" or not content:
                return
            if any("toolResult" in block for block in content):
                return
            if "text" not in content[0]:
                return

            user_query = content[0]["text"]

            # Query every strategy namespace for this customer.
            memory_lines = []
            for strategy_type, namespace in self.namespaces.items():
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace.format(actorId=self.actor_id),
                    query=user_query,
                    top_k=5,
                )
                for memory in memories:
                    text = memory.get("content", {}).get("text", "")
                    if text:
                        memory_lines.append(f"[{strategy_type}] {text}")

            # Prepend what we found to the customer's message.
            if memory_lines:
                context_text = "\n".join(memory_lines)
                content[0]["text"] = (
                    f"Customer Context:\n{context_text}\n\n{user_query}"
                )

        except Exception as e:
            # A memory failure should degrade the answer, never block it.
            logger.warning(f"Memory retrieval failed: {e}")
            

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        try:
            messages = event.agent.messages
            customer_query = None
            agent_response = None

            # Walk backwards to find the last plain-text exchange.
            for msg in reversed(messages):
                content = msg.get("content", [])
                if not content:
                    continue

                # Skip tool results (they also arrive with role "user").
                if any("toolResult" in block for block in content):
                    continue

                text = " ".join(b["text"] for b in content if "text" in b).strip()
                if not text:
                    continue

                if msg.get("role") == "assistant" and agent_response is None:
                    agent_response = text
                elif msg.get("role") == "user" and customer_query is None:
                    customer_query = text

                if customer_query and agent_response:
                    break

            if not (customer_query and agent_response):
                return

            # Remove the "Customer Context" block we prepended on the way in,
            # so recalled memories are not saved back as if the customer said them.
            if customer_query.startswith("Customer Context:"):
                customer_query = customer_query.split("\n\n", 1)[-1]

            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[
                    (customer_query, "USER"),
                    (agent_response, "ASSISTANT"),
                ],
            )

        except Exception as e:
            # Saving failed - log it, but never break the response.
            logger.warning(f"Memory save failed: {e}")

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        # TODO: Register retrieve_customer_context on MessageAddedEvent
        # TODO: Register save_support_interaction on AfterInvocationEvent
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)



# ── TODO 6 — Knowledge Base Tool ─────────────────────────────────────────────
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

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID:
        return "Knowledge base not configured."

    try:
        resp = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )
        results = resp.get("retrievalResults", [])
        if not results:
            return "No relevant information found in the knowledge base."

        chunks = [r["content"]["text"] for r in results if r.get("content", {}).get("text")]
        return "\n---\n".join(chunks)

    except Exception as e:
        return f"Knowledge base search failed: {e}"


# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
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
    tier = tier.strip().title()
    product_category = product_category.strip().lower()

    # Inject the arguments at the top; the logic below is a plain string.
    header = (
        f"loyalty_points = {int(loyalty_points)}\n"
        f"tier = {tier!r}\n"
        f"order_total = {float(order_total)}\n"
        f"category = {product_category!r}\n"
    )
    logic = '''
import json
earn_rates = {"standard": 1, "device": 2, "fresh": 5}
tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}

max_points = int(round(order_total * 0.5 * 100))      # 50% of the order, in points
usable = min(loyalty_points, max_points)
points_redeemed = (usable // 500) * 500               # floor to a multiple of 500
points_discount = points_redeemed / 100               # 100 points = $1
subtotal = round(order_total - points_discount, 2)
tier_discount = round(subtotal * tier_rates.get(tier, 0.0), 2)
final_total = round(subtotal - tier_discount, 2)
total_savings = round(points_discount + tier_discount, 2)
points_earned = int(final_total * earn_rates.get(category, 1))
remaining_points = loyalty_points - points_redeemed

print(json.dumps({
    "source": "code_interpreter",
    "tier": tier,
    "points_redeemed": points_redeemed,
    "points_discount": points_discount,
    "subtotal_after_points": subtotal,
    "tier_discount_pct": round(tier_rates.get(tier, 0.0) * 100, 2),
    "tier_discount": tier_discount,
    "final_total": final_total,
    "total_savings": total_savings,
    "points_earned": points_earned,
    "remaining_points": remaining_points,
}))
'''
    code = header + logic

    try:
        with code_session(REGION) as code_client:
            response = code_client.invoke(
                "executeCode",
                {"code": code, "language": "python", "clearContext": True},
            )
            for event in response["stream"]:
                return json.dumps(event["result"])
        return json.dumps({"error": "Code Interpreter returned no result"})

    except Exception as e:
        # Fallback: tier discount only (no points redemption).
        rate = {"Silver": 0.0, "Gold": 0.10, "Platinum": 0.15}.get(tier, 0.0)
        discount = round(order_total * rate, 2)
        return json.dumps({
            "source": "fallback",
            "note": f"Code Interpreter unavailable ({e}); tier discount only.",
            "tier": tier,
            "points_redeemed": 0,
            "tier_discount_pct": round(rate * 100, 2),
            "tier_discount": discount,
            "final_total": round(order_total - discount, 2),
            "remaining_points": loyalty_points,
        })

# ── TODO 8 — Agent Entrypoint ─────────────────────────────────────────────────
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
        # 1. Read the request
        user_input = payload.get("prompt", "")
        session_id = payload.get("session_id") or str(uuid.uuid4())
        # No shared default actor: a missing customer_id gets its own isolated
        # identity, so unrelated requests never share long-term memory.
        actor_id = payload.get("customer_id") or f"anonymous-{session_id}"

        if not user_input:
            return "Please tell me how I can help you today."

        # 2. Memory hook for this customer and session
        memory_hook = MemoryHook(actor_id, session_id, memory_client, MEMORY_ID)

        # 3. Browser tool
        agent_core_browser = AgentCoreBrowser(region=REGION)

        # 4. Local tools
        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]

        # 5. Gateway tools (orders, refunds) - the client must stay open while
        #    the agent runs, so create AND invoke the agent inside this block.
        gateway_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))

        base_prompt = f"""You are a friendly, concise customer support agent for an Amazon store.

The current customer's ID is {actor_id}. Use it for customer and order lookups.

Tool guidance:
- Order status, tracking, order history, customer profile (including loyalty
  points and tier): use the order tools. Do not ask the customer for their
  points or tier; look them up.
- Refunds and return labels: use the refund tools. A refund needs an order ID
  and a reason; ask for any that are missing. Confirm the refund ID and status.
- Products, return policy, warranty, loyalty program rules, order status
  definitions: use search_knowledge_base.
- Loyalty discount calculations: use calculate_loyalty_discount. Never do the
  arithmetic yourself.
- Live website information: use the browser tool.
- If the browser tool times out or fails, say that you could not load the
  page. Do not guess or recall a page title or page content.

Customer Context at the start of a message is recalled from past
conversations and may be outdated. Use it for names and preferences only.
For order status, tracking, delivery dates and refunds, always use the
order or refund tools instead of relying on it.

If an order lookup returns an internal error, say you could not find that
order and ask the customer to double-check the order ID. Never invent order,
tracking or refund details. Only state facts that come from a tool result."""

        gateway_note = ""

        # ExitStack keeps the Gateway connection open while the agent runs,
        # but lets us catch a failure to connect or to load the tools.
        with ExitStack() as stack:
            try:
                stack.enter_context(gateway_client)
                gateway_tools = gateway_client.list_tools_sync()
                if not gateway_tools:
                    raise RuntimeError("Gateway returned no tools")
                logger.warning(f"Gateway tools loaded: {len(gateway_tools)}")
                tools.extend(gateway_tools)
            except Exception as gw_err:
                logger.error(f"Gateway unavailable: {gw_err}")
                gateway_note = """

IMPORTANT: The order, customer-profile and refund tools are temporarily
unavailable. If the customer asks about an order, a refund, a return label
or their loyalty balance, apologize briefly, say our order and refund system
is temporarily unreachable, and ask them to try again in a few minutes. Do
not guess any order, tracking or refund details. You can still answer
product and policy questions.You can still answer product and policy questions.
Do not mention any order details, tracking numbers or delivery dates, even
if they appear in Customer Context."""

            agent = Agent(
                model=model,
                tools=tools,
                hooks=[memory_hook],
                system_prompt=base_prompt + gateway_note,
            )
            response = await agent.invoke_async(user_input)

        # 7. Return the text of the first content block
        return response.message["content"][0]["text"]

    except Exception as e:
        # 8. Fail gracefully
        logger.exception("Agent invocation failed")
        return "Sorry, I ran into a problem handling your request. Please try again."


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
