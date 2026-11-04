#!/usr/bin/env python3
"""Checks the EKS onboarding end to end in the sandbox: the collector pod is ready, the sample app's metrics are in
AMP, the hud-hourly rules evaluate, investigations are in OpenSearch, and every PromQL panel of the imported Hud
dashboards (dashboard-loader.py output) runs. Panels that run but return nothing are listed, not failed: some need
history (week over week) or alert rules. Needs boto3 and kubectl.
Usage: deploy/dev/eks-sandbox/check.py <dashboards ndjson>   (env: STACK, default hud-eks-sandbox)"""
import hashlib, json, os, subprocess, sys, urllib.error, urllib.parse, urllib.request
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

S = boto3.Session(profile_name="sandbox", region_name="us-east-1")
out = {o["OutputKey"]: o["OutputValue"] for o in
       S.client("cloudformation").describe_stacks(StackName=os.environ.get("STACK", "hud-eks-sandbox"))["Stacks"][0]["Outputs"]}
VARS = {"env": "eks-sandbox", "service": "shop-api", "flow": r"http\|/api/checkout\|post",
        "endpoint": r"http\|/api/checkout\|post", "function": "chargeCard", "sort": "0", "__range_s": "3600", "__range": "1h"}
PPL_VARS = {"env": "eks-sandbox", "endpoint": "http|/api/checkout|post", "function": "chargeCard", "investigation": "none"}
failed, empty = [], []


def get(service, url, body=None, ctype="application/json"):
    # OpenSearch Serverless rejects a signed request without the payload hash header.
    headers = {"x-amz-content-sha256": hashlib.sha256(body or b"").hexdigest(), **({"content-type": ctype} if body else {})}
    req = AWSRequest(method="POST" if body else "GET", url=url, data=body, headers=headers)
    SigV4Auth(S.get_credentials(), service, "us-east-1").add_auth(req)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, method=req.method, headers=dict(req.headers))) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": "%s %s" % (e.code, e.read().decode()[:300])}


def promql(q):
    base = out["AmpQueryUrl"].rsplit("/query", 1)[0]
    return get("aps", base + "/query", urllib.parse.urlencode({"query": q}).encode(), "application/x-www-form-urlencoded")


def check(name, ok, detail=""):
    print("%s %s %s" % ("ok  " if ok else "FAIL", name, detail))
    if not ok:
        failed.append(name)


ready = subprocess.run(["kubectl", "--context", "hud-eks-sandbox", "-n", "hud", "get", "deploy", "hud-collector",
                        "-o", "jsonpath={.status.readyReplicas}"], capture_output=True, text=True).stdout
check("collector ready", ready not in ("", "0"), ready)
r = promql('count(code_function_calls_total{job="shop-api"})').get("data", {}).get("result", [])
check("function metrics in AMP", bool(r), r and r[0]["value"][1])
rules = get("aps", out["AmpQueryUrl"].rsplit("/query", 1)[0] + "/rules").get("data", {}).get("groups", [])
health = {g["name"]: {x["health"] for x in g["rules"]} for g in rules}
check("hud-hourly rules evaluate", bool(health) and all(h == {"ok"} for h in health.values()), health)
n = get("aoss", out["CollectionEndpoint"] + "/hud-investigations-v1/_count").get("count", 0)
check("investigations in OpenSearch", n > 0, n)

for line in open(sys.argv[1]):
    o = json.loads(line)
    if o.get("type") != "explore":
        continue
    q = json.loads(o["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"]).get("query", {})
    text = q.get("query", "")
    if q.get("language") == "PPL":
        # What the UI does: serviceName is the logs schema's alias, and a multi-value variable becomes a list.
        text = text.replace("serviceName", "`resource.attributes.service.name`").replace("in $service", "in ('shop-api')")
        for k, v in PPL_VARS.items():
            text = text.replace("$" + k, v)
        r = get("aoss", out["CollectionEndpoint"] + "/_plugins/_ppl", json.dumps({"query": text}).encode())
        check("panel " + o["id"], "error" not in r, r.get("error", ""))
        if "error" not in r and not r.get("datarows"):
            empty.append(o["id"])
        continue
    if q.get("language") != "PROMQL":
        continue
    for k in sorted(VARS, key=len, reverse=True):
        text = text.replace("$" + k, VARS[k])
    # A panel can hold several queries, separated by ";".
    rs = [promql(t) for t in text.split(";") if t.strip()]
    errors = [r["error"] for r in rs if "error" in r]
    check("panel " + o["id"], not errors, "; ".join(errors))
    if not errors and not any(r.get("data", {}).get("result") for r in rs):
        empty.append(o["id"])

print("panels with no data:", ", ".join(empty) or "none")
sys.exit("%d failed" % len(failed) if failed else 0)
