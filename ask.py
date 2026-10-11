import asyncio, logging, os, sys
import main

logging.getLogger("strands_tools").setLevel(logging.DEBUG)

async def go():
    r = await main.invoke({
        "prompt": sys.argv[1],
        "customer_id": "CUST-123",
        "session_id": sys.argv[2],
    })
    print("RETURNED>>>", repr(r)[:600])
    sys.stdout.flush()
    os._exit(0)

asyncio.run(go())
