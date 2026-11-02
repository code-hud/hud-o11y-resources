#!/usr/bin/env python3
"""Composes the quick-create templates in deploy/aws/ from the Observability Stack's data part
(parts/observability-data.yaml) and the templates that use it, so they can't drift from the separate ones.
Every parameter that a data output says to "Pass to the ... as <X>" is replaced by that output's value, and
conditions on those parameters are resolved. --check fails if a committed output is stale (run by
.github/workflows/cfn-lint.yaml)."""
import copy, re, sys
from pathlib import Path

import yaml

class Tag:
    def __init__(self, tag, value):
        self.tag, self.value = tag, value

    def __eq__(self, other):
        return isinstance(other, Tag) and (self.tag, self.value) == (other.tag, other.value)


class Loader(yaml.SafeLoader):
    pass


def construct(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return Tag("!" + suffix, value)


Loader.add_multi_constructor("!", construct)


class Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def represent_tag(dumper, t):
    if isinstance(t.value, list):
        return dumper.represent_sequence(t.tag, t.value)
    if isinstance(t.value, dict):
        return dumper.represent_mapping(t.tag, t.value)
    return dumper.represent_scalar(t.tag, t.value, style="|" if "\n" in t.value else None)


def represent_str(dumper, s):
    return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None)


Dumper.add_representer(Tag, represent_tag)
Dumper.add_representer(str, represent_str)


def load(name):
    return yaml.load((AWS / name).read_text(), Loader)


def walk(x, f):
    x = f(x)
    if isinstance(x, Tag):
        return Tag(x.tag, walk(x.value, f))
    if isinstance(x, list):
        return [walk(v, f) for v in x]
    if isinstance(x, dict):
        return {k: walk(v, f) for k, v in x.items()}
    return x


def refs(x):
    found = set()

    def f(n):
        if isinstance(n, Tag) and n.tag == "!Ref":
            found.add(n.value)
        if isinstance(n, Tag) and n.tag == "!GetAtt":
            found.add((n.value if isinstance(n.value, str) else n.value[0]).split(".")[0])
        if isinstance(n, Tag) and n.tag == "!Sub":
            s, local = (n.value, {}) if isinstance(n.value, str) else n.value
            found.update(set(re.findall(r"\$\{([\w:.]+)\}", s)) - set(local))
        return n

    walk(x, f)
    return found


AWS = Path(__file__).resolve().parent.parent / "aws"
PROVIDER = "parts/observability-data.yaml"
# Tag each consumer goes by in the data outputs' "Pass to the <tag> stack" descriptions.
TAGS = {"ecs-fargate.yaml": "collector", "eks-iam.yaml": "collector", "hud-access.yaml": "access",
        "parts/observability-ui.yaml": "UI", "parts/observability-attach.yaml": "attach",
        "parts/collector-platform.yaml": "platform"}
# Parts that are never deployed on their own, so outputs only for them are always internal.
INTERNAL = {"UI", "attach", "platform"}
# What both one-stack templates share: Hud access, and a new OpenSearch UI app unless you name yours (ExistingUiAppId).
COMMON = ["hud-access.yaml", "parts/observability-ui.yaml", "parts/observability-attach.yaml"]
# Stack-unique names (the first block of the stack id), so they can't clash with a stack you already run.
UNIQUE_NAME = Tag("!Join", ["-", [Tag("!Ref", "NamePrefix"), Tag("!Select", [0, Tag("!Split", ["-", Tag("!Select", [
    2, Tag("!Split", ["/", Tag("!Ref", "AWS::StackId")])])])])]])
NAME_PREFIX = {
    "Type": "String",
    "Default": "hud",
    # 16 + "-" + 8-character suffix + "-access" fits the 32-character OpenSearch Serverless policy name limit.
    "AllowedPattern": "^[a-z][a-z0-9-]{2,15}$",
    "Description": "Start of the collection, pipeline, AMP workspace and policy names. The stack adds a unique "
                   "suffix, e.g. hud-1a2b3c4d (3-16 lowercase characters).",
}
NEW_UI = ("NewUi", Tag("!Equals", [Tag("!Ref", "ExistingUiAppId"), ""]))
OUTPUTS = {
    "observability.yaml": {
        "consumers": ["parts/observability-ui.yaml"],
        "description": "Observability Stack on OpenSearch Serverless (the same resources as the official serverless "
                       "installer, github.com/opensearch-project/observability-stack): OpenSearch collection, OpenSearch "
                       "Ingestion pipeline, Amazon Managed Service for Prometheus, and an OpenSearch UI application with "
                       "an Observability workspace.",
    },
    "hud-ecs.yaml": {
        "consumers": ["ecs-fargate.yaml"] + COMMON,
        "gates": {"parts/observability-ui.yaml": NEW_UI},
        "description": "Hud on ECS Fargate, in one stack: the Hud OpenTelemetry collector in your ECS cluster, the "
                       "Observability data it writes to (OpenSearch Serverless, OpenSearch Ingestion, Amazon Managed "
                       "Service for Prometheus; uniquely named), a read-only role Hud can assume to query it, and a new "
                       "OpenSearch UI app or your existing one (ExistingUiAppId).",
        "unique_name": True,
    },
    "hud.yaml": {
        "consumers": ["parts/collector-platform.yaml", "eks-iam.yaml"] + COMMON,
        "gates": {"parts/observability-ui.yaml": NEW_UI,
                  "eks-iam.yaml": ("Eks", Tag("!Equals", [Tag("!Ref", "CollectorPlatform"), "EKS"]))},
        "description": "Hud on EKS or with a collector you run, in one stack: the Observability data your collector "
                       "writes to (OpenSearch Serverless, OpenSearch Ingestion, Amazon Managed Service for Prometheus; "
                       "uniquely named), a read-only role Hud can assume to query it, a new OpenSearch UI app or your "
                       "existing one (ExistingUiAppId), and on EKS the collector's IAM role for the Helm chart.",
        "unique_name": True,
    },
}


def merge(*templates):
    out = {"Groups": [], "Parameters": {}, "Rules": {}, "Conditions": {}, "Resources": {}, "Outputs": {}}
    for t in templates:
        for section in ("Parameters", "Rules", "Conditions", "Resources", "Outputs"):
            for k, v in t.get(section, {}).items():
                sec = out.setdefault(section, {})
                if section == "Parameters":
                    sec.setdefault(k, v)
                    continue
                assert k not in sec or sec[k] == v, (section, k)
                sec[k] = v
        out["Groups"] += t.get("Metadata", {}).get("AWS::CloudFormation::Interface", {}).get("ParameterGroups", [])
    return out


def rewriter(bound, fixed):
    def sub_var(expr):
        if isinstance(expr, Tag) and expr.tag in ("!Ref", "!GetAtt"):
            return expr.value

    def rewrite(n):
        if not isinstance(n, Tag):
            return n
        if n.tag == "!If" and n.value[0] in fixed:
            return rewrite(n.value[1] if fixed[n.value[0]] else n.value[2])
        if n.tag == "!Ref" and n.value in bound:
            return copy.deepcopy(bound[n.value])
        if n.tag == "!Sub":
            s, vars = (n.value, {}) if isinstance(n.value, str) else (n.value[0], dict(n.value[1]))
            for p in set(re.findall(r"\$\{(\w+)\}", s)) & set(bound) - set(vars):
                name = sub_var(bound[p])
                if name:
                    s = s.replace("${%s}" % p, "${%s}" % name)
                else:
                    vars[p] = copy.deepcopy(bound[p])
            return Tag("!Sub", [s, vars] if vars else s)
        return n

    return rewrite


def build(consumers, description, unique_name=False, gates={}):
    data = load(PROVIDER)
    loaded = {n: load(n) for n in consumers}
    for t in loaded.values():
        t.get("Resources", {}).pop("Placeholder", None)
    used_by = merge(*loaded.values())
    included = {TAGS[n] for n in consumers}
    bound, internal = {}, set()
    for k, o in data["Outputs"].items():
        m = re.search(r"Pass to the (.+?) stacks? (?:\(.*?\) )?as (\w+)\.", o.get("Description", ""))
        if m:
            if m.group(2) in used_by["Parameters"]:
                bound[m.group(2)] = o["Value"]
            if set(re.split(r", | and ", m.group(1))) <= included | INTERNAL and "Give this to Hud" not in o["Description"]:
                internal.add(k)

    # A bound parameter is never empty, so: Equals[Ref p, ""] is false.
    def cond(e):
        if e.tag == "!Not":
            return not cond(e.value[0])
        a, b = e.value
        assert e.tag == "!Equals" and isinstance(a, Tag) and a.tag == "!Ref" and a.value in bound and b == "", e
        return False

    fixed = {k: cond(v) for k, v in used_by["Conditions"].items() if refs(v) & set(bound)}
    rewrite = rewriter(bound, fixed)

    resources = {k: {a: b for a, b in v.items() if not (a == "Condition" and b in fixed)}
                 for k, v in used_by["Resources"].items() if fixed.get(v.get("Condition"), True)}
    resources = walk(resources, rewrite)
    assert not set(data["Resources"]) & set(resources), set(data["Resources"]) & set(resources)

    outputs = walk(used_by["Outputs"], rewrite)
    for o in outputs.values():
        m = re.search(r"Pass to the (.+?) stacks? (?:\(.*?\) )?as (\w+)\.", o.get("Description", ""))
        if m and set(re.split(r", | and ", m.group(1))) <= included:
            del o["Description"]
    rules = {k: v for k, v in used_by.get("Rules", {}).items() if not refs(v) & set(bound)}
    conditions = {**data.get("Conditions", {}), **{k: v for k, v in used_by["Conditions"].items() if k not in fixed}}

    # A gated consumer's resources, rules and the outputs that use its resources apply only when its condition holds.
    def gate_on(gate, c):
        if c in (None, gate):
            return gate
        conditions[gate + c] = Tag("!And", [Tag("!Condition", gate), Tag("!Condition", c)])
        return gate + c

    for n, (gate, expr) in gates.items():
        conditions[gate] = expr
        gated = set(loaded[n].get("Resources", {})) & set(resources)
        for k in sorted(gated):
            resources[k] = {"Condition": gate_on(gate, resources[k].pop("Condition", None)), **resources[k]}
        for k, o in outputs.items():
            if {r.split(".")[0] for r in refs(o)} & gated:
                outputs[k] = {"Condition": gate_on(gate, o.pop("Condition", None)), **o}
        for k in sorted(set(loaded[n].get("Rules", {})) & set(rules)):
            rc = rules[k].get("RuleCondition")
            rules[k] = {"RuleCondition": Tag("!And", [conditions[gate], rc]) if rc else conditions[gate], **rules[k]}

    for k, o in data["Outputs"].items():
        if k not in internal:
            outputs["Observability" + k if k in outputs else k] = o

    params = dict(data["Parameters"])
    params.update({k: v for k, v in used_by["Parameters"].items() if k not in bound and k not in params})
    groups = [{"Label": {"default": "Observability Stack"}, "Parameters": list(data["Parameters"])}]
    for g in used_by["Groups"]:
        ps = [p for p in g["Parameters"] if p not in bound and p not in data["Parameters"]]
        if ps:
            groups.append({**g, "Parameters": ps})

    t = {
        "AWSTemplateFormatVersion": data["AWSTemplateFormatVersion"],
        "Description": description,
        "Metadata": {"AWS::CloudFormation::Interface": {"ParameterGroups": groups}} if used_by["Groups"] else {},
        "Parameters": params,
        "Rules": rules,
        "Conditions": conditions,
        "Resources": {**data["Resources"], **resources},
        "Outputs": outputs,
    }
    if unique_name:
        t = walk(t, rewriter({"Name": UNIQUE_NAME}, {}))
        t["Parameters"] = {("NamePrefix" if k == "Name" else k): (NAME_PREFIX if k == "Name" else v)
                           for k, v in t["Parameters"].items()}
        for g in t["Metadata"]["AWS::CloudFormation::Interface"]["ParameterGroups"]:
            g["Parameters"] = ["NamePrefix" if p == "Name" else p for p in g["Parameters"]]
    t = {k: v for k, v in t.items() if v}

    used = {r.get("Condition") for r in t["Resources"].values()}

    def ifs(n):
        if isinstance(n, Tag) and n.tag == "!If":
            used.add(n.value[0])
        return n

    walk(t, ifs)
    leftover = refs(t) & (set(bound) | ({"Name"} if unique_name else set())) | used & set(fixed)
    assert not leftover, leftover
    sources = [PROVIDER] + consumers
    header = "# Generated by deploy/scripts/unify.py from %s. Do not edit.\n" % ", ".join(sources)
    return header + yaml.dump(t, Dumper=Dumper, sort_keys=False, width=1000, allow_unicode=True)


if __name__ == "__main__":
    stale = []
    for name, spec in OUTPUTS.items():
        text = build(**spec)
        if sys.argv[1:] == ["--check"]:
            if (AWS / name).read_text() != text:
                stale.append(name)
            else:
                print("ok:", name)
        else:
            (AWS / name).write_text(text)
    if stale:
        sys.exit("stale, run deploy/scripts/unify.py: " + ", ".join(stale))
