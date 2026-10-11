"""
run_tests.py - run the six README test scenarios against the deployed runtime
and save the evidence.
 
    uv run python run_tests.py          # all six
    uv run python run_tests.py 1 2 5    # just these scenarios
    uv run python run_tests.py 4        # memory test (waits between sessions)
 
Every call passes a customer_id (so memory is keyed to a real customer) and a
fresh session ID. The memory test deliberately uses TWO sessions for the same
customer. Output is printed AND written to test-transcript.md.
"""

import json
import sys
import time
import uuid
import os
from datetime import datetime

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
RUNTIME_ARN = os.environ.get(
    "RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-east-1:383866091400:runtime/customer_support_agent-umNZ6y5A7I",
)

CUSTOMER = "CUST-123"
MEMORY_WAIT_SECONDS = 90   # long-term memory is extracted asynchronously
 
client = boto3.client("bedrock-agentcore", region_name=REGION,
                      config=Config(read_timeout=600, retries={"max_attempts": 1}))
 
transcript = [f"# Test transcript\n\nRun at {datetime.now():%Y-%m-%d %H:%M:%S}\n"
              f"Runtime: `{RUNTIME_ARN}`\n"]

def emit(text = ""):
    print(text)
    transcript.append(text)

def ask(prompt, session_id, customer_id=CUSTOMER):
    """Invoke the deployed runtime once and return the reply text."""
    payload = {"prompt": prompt, "customer_id": customer_id, "session_id": session_id}
    emit(f"**Session:** `{session_id}`  |  **Customer:** `{customer_id}`")
    emit(f"**Prompt:** {prompt}\n")
    started = time.time()
    try:
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId=session_id,          # must be 33+ chars (a UUID is 36)
            payload=json.dumps(payload).encode("utf-8"),
        )
        raw = resp["response"].read().decode("utf-8")
        try:
            reply = json.loads(raw)
            if not isinstance(reply, str):
                reply = json.dumps(reply, indent=2)
        except json.JSONDecodeError:
            reply = raw
    except Exception as e:
        reply = f"!! INVOCATION FAILED: {e}"
    emit(f"**Reply** ({time.time() - started:.1f}s):\n\n{reply}\n")
    return reply

def new_session():
    return str(uuid.uuid4())

def header(n, title):
    emit(f"\n{'=' * 70}\n## Test {n}: {title}\n{'=' * 70}")

def test1():
    header(1, "Order tracking")
    ask("Can you track order ORD-001?", new_session())
    emit("_Expected: shipping status, tracking TRK987654321, carrier UPS, delivery date._")

def test2():
    header(2, "Refund processing")
    ask("I want to return my Kindle Paperwhite (ORD-002). Please initiate a refund. "
        "The reason is that I no longer need it.", new_session())
    emit("_Expected: refund ID (REF-...), APPROVED, 3-5 business days._")

def test3():
    header(3, "Knowledge base (RAG)")
    ask("What are the benefits of the Platinum loyalty tier?", new_session())
    emit("_Expected: free same-day shipping, 15% discount, priority support._")

def test4():
    header(4, "Long-term memory (two sessions, same customer)")
    emit("### Session A - introduce yourself")
    ask("Hi, I am Jane. I prefer concise responses.", new_session())
    emit(f"_Waiting {MEMORY_WAIT_SECONDS}s for memory extraction..._\n")
    time.sleep(MEMORY_WAIT_SECONDS)
    emit("### Session B - NEW session, verify recall")
    ask("Do you remember my name and communication preference?", new_session())
    emit("_Expected: recalls 'Jane' and 'concise responses'. If it does not, wait a "
         "little longer and re-run only this test._")

def test5():
    header(5, "Loyalty discount (Code Interpreter)")
    ask("I am a Gold member with 4250 points. Calculate my discount on a $150 "
        "standard order.", new_session())
    emit("_Expected: 4000 points redeemed ($40), Gold 10% ($11), final total $99.00, "
         "250 points remaining. Confirm in the logs that the Code Interpreter ran._")

def test6():
    header(6, "Browser tool")
    ask("Go to https://www.amazon.com and tell me the page title.", new_session())
    emit("_Expected: a page title retrieved from live Amazon.com._")

TESTS = {1: test1, 2: test2, 3: test3, 4: test4, 5: test5, 6: test6}

if __name__ == "__main__":
    wanted = [int(a) for a in sys.argv[1:]] or sorted(TESTS)
    for n in wanted:
        if n not in TESTS:
            sys.exit(f"Unknown test {n}; choose from {sorted(TESTS)}")
    for n in wanted:
        TESTS[n]()
    suffix = "all" if len(wanted) == 6 else "-".join(map(str, wanted))
    out = f"test-transcript-{suffix}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(transcript))
    print(f"\nSaved to {out}")