#!/usr/bin/env python3
"""Renders every collector config the templates and Helm values can produce into OUT_DIR (default a temp dir),
checks their pipelines and prints the image they pin. With --validate, also runs `validate` on each config in that
image with Docker (run by .github/workflows/cfn-lint.yaml and collector-bump.yaml).
ecs-fargate.yaml: merge and convert, each with and without OSIS logs. Helm: values.yaml, and values.yaml with
values-convert.yaml on top."""
import copy, re, subprocess, sys, tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from unify import Loader, Tag  # noqa: E402

IMAGE = "ghcr.io/code-hud/hud-otel-collector"
ENVIRONMENT = ('set(attributes["deployment.environment.name"], resource.attributes["deployment.environment.name"])'
               ' where resource.attributes["deployment.environment.name"] != nil')
# Substituted for the template's parameters; any other ${...} left over (except ${env:...}) fails the render.
PARAMS = {"AWS::Region": "us-east-1", "ExportInterval": "60", "OsisLogsUrl": "https://p.us-east-1.osis.amazonaws.com/p/v1/logs"}


def sub(s):
    for k, v in PARAMS.items():
        s = s.replace("${%s}" % k, v)
    return s.replace("${!", "${")


def resolve(v, cond):
    if isinstance(v, str):
        return v
    if v.tag == "!If":
        return resolve(v.value[1] if cond[v.value[0]] else v.value[2], cond)
    if v.tag == "!Sub" and isinstance(v.value, str):
        return sub(v.value)
    raise ValueError("unexpected %s in HUD_OTELCOL_CONFIG" % v.tag)


def ecs_configs():
    t = yaml.load((ROOT / "deploy/aws/ecs-fargate.yaml").read_text(), Loader)
    image = t["Parameters"]["Image"]["Default"]
    env = t["Resources"]["TaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Environment"]
    text, variables = next(e["Value"] for e in env if e["Name"] == "HUD_OTELCOL_CONFIG").value
    for merge in (True, False):
        for logs in (True, False):
            s = text
            for k, v in variables.items():
                s = s.replace("${%s}" % k, resolve(v, {"Merge": merge, "Logs": logs}))
            s = sub(s)
            left = re.findall(r"\$\{(?!env:)[^}]*\}", s)
            assert not left, "unresolved in ecs-fargate.yaml: %s" % left
            yield "ecs-%s-%s" % ("merge" if merge else "convert", "logs" if logs else "nologs"), s, image


def helm_merge(a, b):
    # Helm merges maps and replaces lists.
    for k, v in b.items():
        a[k] = helm_merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return a


def helm_configs():
    base = yaml.safe_load((ROOT / "deploy/helm/values.yaml").read_text())
    convert = helm_merge(copy.deepcopy(base), yaml.safe_load((ROOT / "deploy/helm/values-convert.yaml").read_text()))
    for name, v in (("helm-merge", base), ("helm-convert", convert)):
        yield name, yaml.safe_dump(v["alternateConfig"], sort_keys=False), "%s:%s" % (v["image"]["repository"], v["image"]["tag"])


def check(name, c):
    merge, logs = "-merge" in name, name.endswith("-logs")
    pipes, procs = c["service"]["pipelines"], c["processors"]
    assert not [p for p in pipes if p.split("/")[0] == "traces"], "traces pipeline"
    metrics = {k: v for k, v in pipes.items() if k.split("/")[0] == "metrics"}
    assert set(metrics) == ({"metrics/functions", "metrics/per_pod"} if merge or name == "helm-convert" else {"metrics"}), metrics
    env = procs["transform/environment"]["metric_statements"]
    assert env == [{"context": "datapoint", "statements": [ENVIRONMENT]}], env
    for k, p in metrics.items():
        ps = p["processors"]
        assert p["receivers"] == ["otlp"] and p["exporters"] == ["prometheus_remote_write"], (k, p)
        assert ps[0] == "memory_limiter" and "batch" in ps and "delta_to_cumulative" in ps, (k, ps)
        assert ps.index("batch") < ps.index("delta_to_cumulative"), (k, ps)
        # Every metric carries deployment_environment_name: transform/merge keeps it on functions in merge mode.
        label = "transform/merge" if merge and k == "metrics/functions" else "transform/environment"
        assert label in ps and ps.index(label) < ps.index("delta_to_cumulative"), (k, ps)
    if merge:
        f = metrics["metrics/functions"]["processors"]
        assert f == ["memory_limiter", "filter/functions", "batch", "transform/merge", "transform/restamp",
                     "delta_to_cumulative", "interval"], f
    else:
        used = {x for p in metrics.values() for x in p["processors"]}
        assert not used & {"transform/merge", "transform/restamp", "interval"}, used
    if "metrics/per_pod" in metrics:
        assert metrics["metrics/per_pod"]["processors"][1] == "filter/per_pod", metrics["metrics/per_pod"]
    if logs:
        assert pipes["logs"] == {"receivers": ["otlp"], "processors": ["memory_limiter", "batch"],
                                 "exporters": ["otlp_http/osis"]}, pipes["logs"]
        assert "sigv4auth/osis" in c["service"]["extensions"], c["service"]["extensions"]
        assert c["exporters"]["otlp_http/osis"]["logs_endpoint"] == PARAMS["OsisLogsUrl"]
    else:
        assert "logs" not in pipes and "otlp_http/osis" not in c["exporters"], name
        assert "sigv4auth/osis" not in c.get("extensions", {}), name


def validate(out, image):
    # Dummy credentials: sigv4auth needs some to start, and validate never sends anything.
    env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_REGION": "us-east-1", "MY_POD_IP": "127.0.0.1",
           "AMP_REMOTE_WRITE_URL": "https://aps-workspaces.us-east-1.amazonaws.com/workspaces/ws-x/api/v1/remote_write"}
    failed = []
    for f in sorted(out.glob("*.yaml")):
        cmd = ["docker", "run", "--rm", "-v", "%s:/config.yaml:ro" % f.resolve()]
        cmd += [a for k, v in env.items() for a in ("-e", "%s=%s" % (k, v))]
        if subprocess.run(cmd + [image, "validate", "--config", "/config.yaml"]).returncode:
            failed.append(f.stem)
        else:
            print("valid:", f.stem, file=sys.stderr)
    if failed:
        sys.exit("%s validate failed: %s" % (image, ", ".join(failed)))


if __name__ == "__main__":
    args = sys.argv[1:]
    run_validate = "--validate" in args
    args = [a for a in args if a != "--validate"]
    if len(args) > 1 or [a for a in args if a.startswith("-")]:
        sys.exit("usage: scripts/collector-configs.py [--validate] [OUT_DIR]")
    out = Path(args[0] if args else tempfile.mkdtemp(prefix="collector-configs-"))
    out.mkdir(parents=True, exist_ok=True)
    images = set()
    for name, text, image in [*ecs_configs(), *helm_configs()]:
        try:
            check(name, yaml.safe_load(text))
        except AssertionError as e:
            sys.exit("%s: %s" % (name, e))
        (out / (name + ".yaml")).write_text(text)
        images.add(image)
        print("ok:", name, file=sys.stderr)
    if len(images) != 1 or not next(iter(images)).startswith(IMAGE + ":"):
        sys.exit("the templates and Helm values must pin one %s tag: %s" % (IMAGE, sorted(images)))
    image = images.pop()
    print(image)
    if run_validate:
        validate(out, image)
