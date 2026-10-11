"""
test_gateway.py - talk to the AgentCore Gateway directly over MCP.
 
No agent, no model, no memory in the path. If a call works here, the Gateway
and its Lambdas are fine; if the agent later fails, the fault is above this layer.
 
    uv run python test_gateway.py
"""

import asyncio
import json
 
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
 
with open("phase2_outputs.json") as f:
    GATEWAY_URL = json.load(f)["gateway_url"]

def short(result, limit=400):
    text = " ".join(getattr(c, "text", str(c)) for c in result.content)
    is_err = getattr(result, "is_error", getattr(result, "isError", False))
    flag = " [isError]" if is_err else ""
    return (text[:limit] + ("..." if len(text) > limit else "")) + flag
 
 
async def main():
    print(f"Gateway: {GATEWAY_URL}\n")
    async with streamable_http_client(GATEWAY_URL) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
 
            tools = (await session.list_tools()).tools
            print(f"== {len(tools)} tools advertised")
            full = {}
            for t in tools:
                bare = t.name.split("___", 1)[-1]
                full[bare] = t.name
                schema = getattr(t, "input_schema", getattr(t, "inputSchema", None)) or {}
                props = list(schema.get("properties", {}))
                print(f"  {t.name}   args={props}")
            if not tools:
                print("\n!! No tools listed. A target is probably FAILED "
                      "(runbook E6). Check target status.")
                return
 
            async def call(bare, args, label):
                if bare not in full:
                    print(f"\n-- {label}: SKIPPED (tool '{bare}' not found)")
                    return None
                print(f"\n-- {label}\n   {bare}({args})")
                try:
                    res = await session.call_tool(full[bare], args)
                    out = short(res)
                except Exception as e:
                    out = f"EXCEPTION: {e}"
                print(f"   -> {out}")
                return out
 
            print("\n== Calls")
            await call("get_order", {"order_id": "ORD-001"}, "valid order")
            await call("get_order", {"order_id": "ORD-999"},
                       "unknown order (expect an error, see runbook E11)")
            await call("get_customer", {"customer_id": "CUST-123"}, "customer profile")
            await call("list_customer_orders", {"customer_id": "CUST-123"},
                       "customer's orders")
            out = await call("initiate_refund",
                             {"order_id": "ORD-002", "reason": "Gateway test",
                              "amount": 139.99}, "refund")
            await call("check_refund_status", {"refund_id": "REF-TEST0001"},
                       "refund status")
            await call("get_return_label", {"order_id": "ORD-002"}, "return label")
 
    print("\nDone.")
 
 
asyncio.run(main())