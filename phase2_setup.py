"""
phase2_setup.py - build the AWS infrastructure for the support agent.

Run ONE STAGE AT A TIME. (Each stage is safe to re run.):

uv run python phase2_setup.py lambdas  # IAM role + 2 Lambda functions
uv run python phase2_setup.py api      # REST API(3 GET routes) for order tracking
uv run python phase2_setup.py gateway  # Gateway role, Gateway, 2 targets 
uv run python phase2_setup.py memory   # AgentCore Memory + 2 strategies
uv run python phase2_setup.py all      # everything in order 

Every id created is saved to phase2_outpts.json
Run from the project folder (needs lambda/order_tracking.py, lambda/refund_processor.py, lamda/lambda_schema.py)
Knowledge is not created here, expensive to do so. created last in the console.
"""

import io
import json
import os
import sys
import time
import zipfile 

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
OUT_FILE = "phase2_outputs.json"

LAMBDA_ROLE = "csai-lambda-role"
GATEWAY_ROLE = "csai-gateway-role"
ORDER_FN = "order-tracker"
REFUND_FN = "refund-processor"
API_NAME = "csai-orders-api"
STAGE = "prod"
GATEWAY_NAME = "CustomerSupportGateway"
MEMORY_NAME = "CustomerSupportMemory"   # memory names allow underscores, not hyphens
# Gateway and target names allow letters, digits and HYPHENS only (no underscores).
ORDER_TARGET = "order-tracker"
REFUND_TARGET = "refund-processor"

sts = boto3.client("sts", region_name=REGION)
iam = boto3.client("iam", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
apigw = boto3.client("apigateway", region_name=REGION)
ctrl = boto3.client("bedrock-agentcore-control", region_name=REGION)
ACCOUNT = sts.get_caller_identity()["Account"]

#--- Helpers -----

def load_out():
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE) as f:
            return json.load(f)
    return {}

def save_out(**kv):
    data = load_out()
    data.update(kv)
    with open(OUT_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return data

def need(key, stage):
    data = load_out()
    if key not in data:
        sys.exit(f"Missing '{key}' in {OUT_FILE}. Run the '{stage}' stage first.")
    return data[key]

def ensure_role(name, service, managed_policies=()):
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow",
                       "Principal": {"Service": service},
                       "Action": "sts:AssumeRole"}],
    }
    try:
        arn = iam.create_role(RoleName=name,
                              AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        print(f"  created role {name}")
        created = True
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        print(f"  role {name} already exists")
        created = False
        try:   # make sure the trust policy is right even for a pre-existing role
            iam.update_assume_role_policy(RoleName=name,
                                          PolicyDocument=json.dumps(trust))
        except ClientError as err:
            print(f"  !! could not update trust policy ({err.response['Error']['Code']}) - check later")
    for p in managed_policies:
        iam.attach_role_policy(RoleName=name, PolicyArn=p)
    return arn, created

def zip_py(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(path, arcname=os.path.basename(path))
    return buf.getvalue()
 
 
def ensure_lambda(fn_name, file_path, handler, role_arn):
    code = zip_py(file_path)
    try:
        arn = lam.create_function(
            FunctionName=fn_name, Runtime="python3.12", Role=role_arn,
            Handler=handler, Code={"ZipFile": code}, Timeout=15,
        )["FunctionArn"]
        print(f"  created Lambda {fn_name}")
    except ClientError as e:
        code_name = e.response["Error"]["Code"]
        if code_name == "ResourceConflictException":
            lam.update_function_code(FunctionName=fn_name, ZipFile=code)
            arn = lam.get_function(FunctionName=fn_name)["Configuration"]["FunctionArn"]
            print(f"  Lambda {fn_name} exists - code updated")
        elif code_name == "InvalidParameterValueException" and "role" in str(e).lower():
            return None
        else:
            raise
    lam.get_waiter("function_active_v2").wait(FunctionName=fn_name)
    return arn

# -- stage: Lambdas-- 

def stage_lambdas():
    print("== Stage: lambdas")
    role_arn, created = ensure_role(
        LAMBDA_ROLE, "lambda.amazonaws.com",
        ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"])
    if created:
        print("  waiting 15s for IAM role to propagate...")
        time.sleep(15)
 
    arn_order = arn_refund = None
    for attempt in range(4):   # retry: new roles are sometimes not usable yet
        arn_order = ensure_lambda(ORDER_FN, "lambda/order_tracker.py",
                                  "order_tracker.lambda_handler", role_arn)
        arn_refund = ensure_lambda(REFUND_FN, "lambda/refund_processor.py",
                                   "refund_processor.lambda_handler", role_arn)
        if arn_order and arn_refund:
            break
        print("  role not ready yet, retrying in 10s...")
        time.sleep(10)
    if not (arn_order and arn_refund):
        sys.exit("Could not create Lambdas - role not usable. Re-run the stage.")
 
    save_out(account=ACCOUNT, region=REGION, lambda_role_arn=role_arn,
             order_lambda_arn=arn_order, refund_lambda_arn=arn_refund)
    print(f"  {arn_order}\n  {arn_refund}")

# -- stage: API --

ROUTES = [
    # (path, path-template segments built below)
    "/orders/{order_id}",
    "/customers/{customer_id}/orders",
    "/customers/{customer_id}",
]
 
 
def _get_or_create_resource(api_id, root_id, path):
    """Create every segment of `path` under the root; return the leaf resource id."""
    existing = {r["path"]: r["id"] for r in
                apigw.get_resources(restApiId=api_id, limit=500)["items"]}
    parent_id, current = root_id, ""
    for seg in [s for s in path.split("/") if s]:
        current += "/" + seg
        if current in existing:
            parent_id = existing[current]
        else:
            parent_id = apigw.create_resource(
                restApiId=api_id, parentId=parent_id, pathPart=seg)["id"]
            existing[current] = parent_id
    return parent_id

def stage_api():
    print("== Stage: api")
    order_arn = need("order_lambda_arn", "lambdas")
 
    apis = [a for a in apigw.get_rest_apis(limit=500)["items"] if a["name"] == API_NAME]
    if apis:
        api_id = apis[0]["id"]
        print(f"  REST API exists: {api_id}")
    else:
        api_id = apigw.create_rest_api(
            name=API_NAME, endpointConfiguration={"types": ["REGIONAL"]})["id"]
        print(f"  created REST API: {api_id}")
 
    root_id = [r for r in apigw.get_resources(restApiId=api_id)["items"]
               if r["path"] == "/"][0]["id"]
    uri = (f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/"
           f"{order_arn}/invocations")

    for path in ROUTES:
        rid = _get_or_create_resource(api_id, root_id, path)
        try:
            apigw.put_method(restApiId=api_id, resourceId=rid, httpMethod="GET",
                             authorizationType="NONE")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConflictException":
                raise
        apigw.put_integration(restApiId=api_id, resourceId=rid, httpMethod="GET",
                              type="AWS_PROXY", integrationHttpMethod="POST", uri=uri)
        # IMPORTANT (runbook E7): method responses must exist, otherwise the exported
        # OpenAPI spec has no `responses` block and the Gateway target goes FAILED.
        for code in ("200", "404", "500"):
            try:
                apigw.put_method_response(
                    restApiId=api_id, resourceId=rid, httpMethod="GET",
                    statusCode=code,
                    responseModels={"application/json": "Empty"})
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConflictException":
                    raise
        print(f"  route GET {path} ready")

     # let API Gateway invoke the Lambda
    try:
        lam.add_permission(
            FunctionName=ORDER_FN, StatementId="apigw-invoke",
            Action="lambda:InvokeFunction", Principal="apigateway.amazonaws.com",
            SourceArn=f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{api_id}/*/GET/*")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceConflictException":
            raise 

    apigw.create_deployment(restApiId=api_id, stageName=STAGE)
    url = f"https://{api_id}.execute-api.{REGION}.amazonaws.com/{STAGE}"
    save_out(rest_api_id=api_id, stage=STAGE, api_url=url)
    print(f"  deployed. Test it:\n    curl -s {url}/orders/ORD-001")

# -- stage: Gateway --

def wait_status(getter, ok, bad, label, timeout=300):
    start = time.time()
    while time.time() - start < timeout:
        r = getter()
        status = r["status"] if "status" in r else r["memory"]["status"]
        if status in ok:
            return r
        if status in bad:
            reasons = r.get("statusReasons") or r.get("memory", {}).get("failureReason")
            sys.exit(f"{label} -> {status}: {reasons}")
        time.sleep(5)
    sys.exit(f"Timed out waiting for {label}")

def stage_gateway():
    print("== Stage: gateway")
    api_id = need("rest_api_id", "api")
    refund_arn = need("refund_lambda_arn", "lambdas")
    order_arn = need("order_lambda_arn", "lambdas")
 
    role_arn, created = ensure_role(GATEWAY_ROLE, "bedrock-agentcore.amazonaws.com")
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "lambda:InvokeFunction",
             "Resource": [refund_arn, order_arn]},
            {"Effect": "Allow", "Action": "execute-api:Invoke",
             "Resource": f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{api_id}/*"},
        ],
    }
    iam.put_role_policy(RoleName=GATEWAY_ROLE, PolicyName="csai-gateway-access",
                        PolicyDocument=json.dumps(policy))
    if created:
        print("  waiting 15s for IAM role to propagate...")
        time.sleep(15)
 
    # find or create the gateway
    gw = next((g for g in ctrl.list_gateways().get("items", [])
               if g["name"] == GATEWAY_NAME), None)
    if gw:
        gw_id = gw["gatewayId"]
        print(f"  Gateway exists: {gw_id}")
    else:
        gw_id = ctrl.create_gateway(
            name=GATEWAY_NAME, roleArn=role_arn, protocolType="MCP",
            authorizerType="NONE",
            description="Customer support tools (orders, refunds)")["gatewayId"]
        print(f"  created Gateway: {gw_id}")
    gw = wait_status(lambda: ctrl.get_gateway(gatewayIdentifier=gw_id),
                     {"READY"}, {"FAILED"}, "Gateway")
    gw_url = gw["gatewayUrl"]
    save_out(gateway_id=gw_id, gateway_role_arn=role_arn, gateway_url=gw_url)
 
    existing = {t["name"]: t["targetId"] for t in
                ctrl.list_gateway_targets(gatewayIdentifier=gw_id).get("items", [])}
    iam_cred = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]

    #target 1: Rest API (Order Tracker) 

    if ORDER_TARGET not in existing:
        ctrl.create_gateway_target(
            gatewayIdentifier=gw_id, name=ORDER_TARGET,
            description="Order and customer lookup",
            targetConfiguration={"mcp": {"apiGateway": {
                "restApiId": api_id, "stage": STAGE,
                "apiGatewayToolConfiguration": {
                    "toolFilters": [
                        {"filterPath": "/orders/{order_id}", "methods": ["GET"]},
                        {"filterPath": "/customers/{customer_id}/orders", "methods": ["GET"]},
                        {"filterPath": "/customers/{customer_id}", "methods": ["GET"]},
                    ],
                    "toolOverrides": [
                        {"name": "get_order", "path": "/orders/{order_id}", "method": "GET",
                         "description": "Look up a single order by order ID (e.g. ORD-001). Returns status, items, total, tracking number, carrier and delivery dates."},
                        {"name": "list_customer_orders", "path": "/customers/{customer_id}/orders", "method": "GET",
                         "description": "List all orders for a customer ID (e.g. CUST-123)."},
                        {"name": "get_customer", "path": "/customers/{customer_id}", "method": "GET",
                         "description": "Get a customer's profile: name, loyalty points balance and loyalty tier."},
                    ]}}}},
            credentialProviderConfigurations=iam_cred)
        print(f"  created target {ORDER_TARGET}")
    else:
        print(f"  target {ORDER_TARGET} exists")

    #target 2: Direct Lambda(refund processor) -- tool schema from lambda/lambda_schema.py

    if REFUND_TARGET not in existing:
        with open("lambda/lambda_schema") as f:
            schema = json.load(f)
        ctrl.create_gateway_target(
            gatewayIdentifier=gw_id, name=REFUND_TARGET,
            description="Refund processing",
            targetConfiguration={"mcp": {"lambda": {
                "lambdaArn": refund_arn,
                "toolSchema": {"inlinePayload": schema}}}},
            credentialProviderConfigurations=iam_cred)
        print(f"  created target {REFUND_TARGET}")
    else:
        print(f"  target {REFUND_TARGET} exists")

    #Report target status 

    time.sleep(5)
    for t in ctrl.list_gateway_targets(gatewayIdentifier=gw_id).get("items", []):
        for _ in range(30):
            r = ctrl.get_gateway_target(gatewayIdentifier=gw_id, targetId=t["targetId"])
            if r["status"] not in ("CREATING", "UPDATING", "SYNCHRONIZING"):
                break
            time.sleep(5)
        print(f"  target {r['name']}: {r['status']}")
        if r["status"] != "READY":
            print(f"    statusReasons: {r.get('statusReasons')}")
    print(f"\n  GATEWAY_URL = {gw_url}")

#--- stage: Memory ---

def stage_memory():
    print("== Stage: memory")
    found = next((m for m in ctrl.list_memories().get("memories", [])
                  if m["id"].startswith(MEMORY_NAME)), None)
    if found:
        mem_id = found["id"]
        print(f"  Memory exists: {mem_id}")
    else:
        mem = ctrl.create_memory(
            name=MEMORY_NAME, eventExpiryDuration=30,
            description="Long-term memory for the customer support agent",
            memoryStrategies=[
                {"semanticMemoryStrategy": {
                    "name": "customer_facts",
                    "namespaces": ["cs_agent/{actorId}/facts"]}},
                {"userPreferenceMemoryStrategy": {
                    "name": "customer_preferences",
                    "namespaces": ["cs_agent/{actorId}/preferences"]}},
            ])["memory"]
        mem_id = mem["id"]
        print(f"  created Memory: {mem_id}")
    wait_status(lambda: ctrl.get_memory(memoryId=mem_id),
                {"ACTIVE"}, {"FAILED"}, "Memory", timeout=600)
    save_out(memory_id=mem_id)
    print(f"  MEMORY_ID = {mem_id}")
 
 
STAGES = {"lambdas": stage_lambdas, "api": stage_api,
          "gateway": stage_gateway, "memory": stage_memory}
 
if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "all":
        for fn in STAGES.values():
            fn()
    elif arg in STAGES:
        STAGES[arg]()
    else:
        sys.exit(__doc__)
    print(f"\nSaved to {OUT_FILE}:\n{json.dumps(load_out(), indent=2)}")


    