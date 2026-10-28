#!/usr/bin/env python3
"""Adapt OpenSearch Dashboards / OpenSearch UI saved-object exports (NDJSON) to another environment.

Reads every *.ndjson under the input paths (default: dashboards/), rewrites the environment-specific
values, checks the result and writes all objects to one file, <out>/hud.ndjson. Sources are never modified.

What is rewritten:
  - the workspace in dashboard link URLs (/w/<id>/app/...), or the prefix removed when no workspace is used;
  - the OpenSearch data source id (data-source references and dataset.dataSource), or both removed
    for a self-managed OSD that queries its local cluster;
  - the Prometheus connection name (dataset id/title of PromQL panels and dashboard variables);
  - the logs index pattern (index-pattern title, dataset title, PPL "source = ..." clauses);
  - dashboard links that use '$__all', when the target is older than OSD 3.10 (see --links).

Python 3.9+, standard library only.
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"\bHUD_[A-Z0-9_]+\b")
WORKSPACE_RE = re.compile(r"/w/([^/\s\"'?#]+)(?=/app/)")
LINK_RE = re.compile(r"/app/dashboards#/view/([^?\s\"']+)\?([^\s\"]*)")
LINK_VARS_RE = re.compile(r"_a=\(variables:!\(((?:\(id:[^,()]+,current:!\([^()]*\)\),?)*)\)\)")
LINK_VAR_RE = re.compile(r"\(id:([^,()]+),current:!\(([^()]*)\)\)")
ALL_VALUE = "'$__all'"
MIN_VERSION = (3, 9)
ALL_MARKER_VERSION = (3, 10)
OUTPUT_NAME = "hud.ndjson"


class Report:
    def __init__(self):
        self.errors, self.warnings, self.notes = [], [], []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def note(self, msg):
        self.notes.append(msg)


# ---------------------------------------------------------------------------- JSON helpers
# Saved objects embed JSON documents as strings (searchSourceJSON, visualization, variablesJSON, ...).
# They are decoded so they can be edited structurally, and only re-encoded when they changed, in the
# same separator style, so unchanged objects stay byte-identical to the export.

class Embedded:
    __slots__ = ("value", "raw")

    def __init__(self, value, raw):
        self.value, self.raw = value, raw


def _style(raw):
    first = re.search(r'"(?:[^"\\]|\\.)*"\s*:( ?)', raw)
    compact = not (first and first.group(1))
    return {
        "separators": (",", ":") if compact else (", ", ": "),
        "ensure_ascii": bool(re.search(r"\\u[0-9a-fA-F]{4}", raw)),
    }


def decode(x):
    if isinstance(x, dict):
        return {k: decode(v) for k, v in x.items()}
    if isinstance(x, list):
        return [decode(v) for v in x]
    if isinstance(x, str) and x[:1] in "{[":
        try:
            parsed = json.loads(x)
        except ValueError:
            return x
        if isinstance(parsed, (dict, list)):
            return Embedded(decode(parsed), x)
    return x


def encode(x):
    if isinstance(x, Embedded):
        value = encode(x.value)
        if value == json.loads(x.raw):
            return x.raw
        return json.dumps(value, **_style(x.raw))
    if isinstance(x, dict):
        return {k: encode(v) for k, v in x.items()}
    if isinstance(x, list):
        return [encode(v) for v in x]
    return x


def walk(x, fn, parent=None, key=None):
    """Call fn(container, key, value) for every value, depth first, looking inside embedded JSON."""
    if isinstance(x, Embedded):
        walk(x.value, fn)
        return
    if isinstance(x, dict):
        for k in list(x):
            walk(x[k], fn, x, k)
    elif isinstance(x, list):
        for i in range(len(x)):
            walk(x[i], fn, x, i)
    if parent is not None:
        fn(parent, key, x)


def datasets(obj):
    found = []

    def fn(parent, key, value):
        if key == "dataset" and isinstance(value, dict) and "type" in value:
            found.append(value)

    walk(obj, fn)
    return found


def map_strings(obj, fn):
    def visit(parent, key, value):
        if isinstance(value, str):
            new = fn(value)
            if new != value:
                parent[key] = new

    walk(obj, visit)


def unwrap(x):
    return x.value if isinstance(x, Embedded) else x


# ---------------------------------------------------------------------------- loading

class Line:
    def __init__(self, path, lineno, raw, obj):
        self.path, self.lineno, self.raw, self.obj = path, lineno, raw, obj

    @property
    def where(self):
        return f"{self.path.name}:{self.lineno} {self.obj.get('type')} {self.obj.get('id')}"


def load(paths, report):
    files = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files += sorted(p.glob("*.ndjson"))
        elif p.is_file():
            files.append(p)
        else:
            report.error(f"input not found: {p}")
    if not files and not report.errors:
        report.error(f"no *.ndjson files under {', '.join(map(str, paths))}")
    loaded = {}
    for f in files:
        lines = []
        for n, raw in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            try:
                doc = json.loads(raw)
            except ValueError as e:
                report.error(f"{f.name}:{n}: not JSON ({e})")
                continue
            if "type" not in doc and "exportedCount" in doc:
                if doc.get("missingRefCount"):
                    report.warn(f"{f.name}: the export reports {doc['missingRefCount']} missing references: "
                                f"{doc.get('missingReferences')}")
                continue
            lines.append(Line(f, n, raw, decode(doc)))
        loaded[f] = lines
    return loaded


# ---------------------------------------------------------------------------- version

def parse_version(s):
    m = re.match(r"^\s*(\d+)\.(\d+)", s or "")
    if not m:
        raise ValueError(f"not a version: {s!r}")
    return int(m.group(1)), int(m.group(2))


def fetch_osd_version(url, user, password, insecure):
    import ssl

    req = urllib.request.Request(url.rstrip("/") + "/api/status", headers={"osd-xsrf": "true"})
    if user:
        token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    ctx = ssl._create_unverified_context() if insecure else None
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        status = json.load(r)
    return status["version"]["number"]


# ---------------------------------------------------------------------------- rewrites

def parse_mapping(values, what, found, report):
    """--x NEW or --x OLD=NEW (repeatable) -> {old: new} over the values found in the export."""
    mapping = {}
    for v in values or []:
        if "=" in v:
            old, new = v.split("=", 1)
            if old not in found:
                report.error(f"{what} {old!r} does not occur in the export (found: {sorted(found) or 'none'})")
            mapping[old] = new
        elif len(found) > 1:
            report.error(f"the export uses several {what}s ({', '.join(sorted(found))}); "
                         f"pass OLD=NEW for each")
        else:
            mapping.update({old: v for old in found})
    return {o: n for o, n in mapping.items() if o != n}


def rewrite_prometheus(objs, mapping):
    for o in objs:
        for ds in datasets(o):
            if str(ds.get("type")).upper() == "PROMETHEUS":
                for k in ("id", "title"):
                    if ds.get(k) in mapping:
                        ds[k] = mapping[ds[k]]


def rewrite_index_pattern(objs, mapping):
    def ppl(s):
        for old, new in mapping.items():
            s = re.sub(r"(\bsource\s*=\s*)" + re.escape(old) + r"(?=[\s|,]|$)", lambda m: m.group(1) + new, s)
        return s

    for o in objs:
        attrs = o.get("attributes", {})
        if o.get("type") == "index-pattern" and attrs.get("title") in mapping:
            attrs["title"] = mapping[attrs["title"]]
        for ds in datasets(o):
            if str(ds.get("type")).upper() == "INDEX_PATTERN" and ds.get("title") in mapping:
                ds["title"] = mapping[ds["title"]]

        def visit(parent, key, value):
            if key == "query" and isinstance(value, str) and isinstance(parent, dict) \
                    and str(parent.get("language", "")).upper() == "PPL":
                parent[key] = ppl(value)

        walk(o, visit)


def opensearch_data_sources(objs):
    found = set()
    for o in objs:
        found |= {r["id"] for r in o.get("references", []) if r.get("type") == "data-source"}
        for ds in datasets(o):
            src = ds.get("dataSource")
            if isinstance(src, dict) and src.get("id") and str(src.get("type", "")).lower() == "opensearch":
                found.add(src["id"])
    return found


def rewrite_data_source(objs, new_id, new_title, remove):
    for o in objs:
        if remove:
            o["references"] = [r for r in o.get("references", []) if r.get("type") != "data-source"]
        else:
            for r in o.get("references", []):
                if r.get("type") == "data-source":
                    r["id"] = new_id
        for ds in datasets(o):
            src = ds.get("dataSource")
            if isinstance(src, dict) and src.get("id") and str(src.get("type", "")).lower() == "opensearch":
                if remove:
                    del ds["dataSource"]
                else:
                    src["id"] = new_id
                    if new_title:
                        src["title"] = new_title


def rewrite_workspace(objs, workspace_id):
    prefix = f"/w/{workspace_id}" if workspace_id else ""

    def fn(s):
        if "/app/dashboards#" not in s:
            return s
        return re.sub(r"(/w/[^/\s\"'?#]+)?(/app/dashboards#)", lambda m: prefix + m.group(2), s)

    for o in objs:
        map_strings(o, fn)


def version_neutral_links(objs, report):
    """Rewrite links that set variables to '$__all' (understood only by OSD >= 3.10).

    The '$__all' entries are dropped from the link. On the target dashboard, the variables the link
    still sets move to the front, and every dropped variable becomes a label-values lookup chained to
    them: same label, the linked variable's metric, and a matcher per linked variable (and per earlier
    dropped variable), so its default matches the clicked entity instead of the first option.
    """
    linked, dropped = {}, {}

    def fn(s):
        def link(m):
            target, rest = m.group(1), m.group(2)

            def vars_(vm):
                entries = LINK_VAR_RE.findall(vm.group(1))
                keep = [(i, c) for i, c in entries if c != ALL_VALUE]
                gone = [i for i, c in entries if c == ALL_VALUE]
                if not gone:
                    return vm.group(0)
                linked.setdefault(target, [])
                linked[target] += [i for i, _ in keep if i not in linked[target]]
                dropped.setdefault(target, [])
                dropped[target] += [i for i in gone if i not in dropped[target]]
                return "_a=(variables:!(" + ",".join(f"(id:{i},current:!({c}))" for i, c in keep) + "))"

            return f"/app/dashboards#/view/{target}?" + LINK_VARS_RE.sub(vars_, rest)

        return LINK_RE.sub(link, s) if "/app/dashboards#" in s else s

    rewritten = 0
    for o in objs:
        before = json.dumps(encode(o))
        map_strings(o, fn)
        rewritten += before != json.dumps(encode(o))
    if rewritten:
        report.note(f"links: removed '$__all' from links in {rewritten} objects")

    dashboards = {o["id"]: o for o in objs if o.get("type") == "dashboard"}
    for target, gone in dropped.items():
        dash = dashboards.get(target)
        if dash is None:
            continue
        holder = dash["attributes"].get("variablesJSON")
        doc = unwrap(holder)
        if not isinstance(doc, dict):
            report.warn(f"dashboard {target}: links dropped '$__all' for {gone} but it has no variablesJSON")
            continue
        variables = doc.get("variables", [])
        by_name = {v.get("name"): v for v in variables}
        keep = [by_name[n] for n in linked[target] if n in by_name]
        chain = [by_name[n] for n in gone if n in by_name and n not in linked[target]]
        anchors = [v for v in keep if (v.get("promQLResourceQuery") or {}).get("kind") == "labelValues"]
        if not anchors:
            report.warn(f"dashboard {target}: no label-values variable among {linked[target]} to chain "
                        f"{gone} to; links land on their defaults")
            continue
        metric = anchors[-1]["promQLResourceQuery"]["metric"]
        done = []
        for v in chain:
            q = v.get("promQLResourceQuery")
            if not q or q.get("kind") != "labelValues":
                report.warn(f"dashboard {target}: variable {v.get('name')} is not a label-values lookup; left as is")
                continue
            matchers = [m for m in q.get("matchers", []) if m.get("label") not in
                        {a["promQLResourceQuery"]["label"] for a in anchors + done}]
            matchers += [{"label": a["promQLResourceQuery"]["label"], "operator": "=", "value": "$" + a["name"]}
                         for a in anchors]
            matchers += [{"label": d["promQLResourceQuery"]["label"], "operator": "=~", "value": "$" + d["name"]}
                         for d in done]
            q["metric"], q["matchers"] = metric, matchers
            done.append(v)
        moved = keep + [v for v in variables if v not in keep]
        if [v.get("name") for v in moved] != [v.get("name") for v in variables]:
            doc["variables"] = moved
        report.note(f"dashboard {target}: variables {[v.get('name') for v in moved]}; "
                    f"{[v.get('name') for v in done]} chained to {[a['name'] for a in anchors]} on {metric}")


# ---------------------------------------------------------------------------- checks

def check(objs, where, args, report, old_values):
    ids = {}
    for o in objs:
        key = (o.get("type"), o.get("id"))
        if key in ids:
            report.error(f"duplicate object {key[0]} {key[1]} in {where[id(o)]} and {ids[key]}")
        ids[key] = where[id(o)]
    by_id = {i for _, i in ids}
    by_type = {}
    for t, i in ids:
        by_type.setdefault(t, set()).add(i)
    dashboards = by_type.get("dashboard", set())
    index_patterns = by_type.get("index-pattern", set())

    for o in objs:
        w = where[id(o)]
        text = json.dumps(encode(o), ensure_ascii=False)
        for p in sorted(set(PLACEHOLDER_RE.findall(text))):
            report.error(f"{w}: placeholder {p} is still present")
        dangling = set()
        for r in o.get("references", []):
            if r.get("type") == "data-source":
                if r["id"] != args.opensearch_data_source_id:
                    report.error(f"{w}: data-source reference {r['id']} is not the target data source")
            elif r.get("id") not in by_type.get(r.get("type"), set()):
                dangling.add(r.get("id"))
                report.error(f"{w}: reference {r.get('name')} -> {r.get('type')} {r.get('id')} "
                             f"is not in the output")
        for ds in datasets(o):
            t = str(ds.get("type")).upper()
            if t == "INDEX_PATTERN" and ds.get("id") not in index_patterns | dangling:
                report.error(f"{w}: query dataset uses index pattern {ds.get('id')} that is not in the output")
            if t == "PROMETHEUS" and args.prometheus_names and ds.get("id") not in args.prometheus_names:
                report.error(f"{w}: Prometheus connection {ds.get('id')} is not one of {sorted(args.prometheus_names)}")
        for target, _ in LINK_RE.findall(text):
            if target not in dashboards:
                report.error(f"{w}: link to dashboard {target}, which is not in the output")
        for m in re.finditer(r"([^\s\"]*)/app/dashboards#/view/", text):
            ws = re.search(r"/w/([^/]+)$", m.group(1))
            if args.workspace_id and (not ws or ws.group(1) != args.workspace_id):
                report.error(f"{w}: a dashboard link does not carry /w/{args.workspace_id}")
            if not args.workspace_id and ws:
                report.error(f"{w}: a dashboard link still carries /w/{ws.group(1)}")
        if args.links == "version-neutral" and ALL_VALUE in text and "/app/dashboards#" in text:
            report.error(f"{w}: a dashboard link still uses '$__all', which this OSD version does not understand")
        for label, values in old_values.items():
            for v in values:
                if re.search(r"(?<![\w-])" + re.escape(v) + r"(?![\w-])", text):
                    report.warn(f"{w}: old {label} {v!r} still occurs (outside the fields this script rewrites)")
    return by_id


# ---------------------------------------------------------------------------- main

def main(argv=None):
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(
        description="Adapt Hud dashboard exports (NDJSON) to an OpenSearch UI workspace or a self-managed "
                    "OpenSearch Dashboards, check them, and write the result to a separate directory.")
    ap.add_argument("inputs", nargs="*", default=[str(here / "dashboards")],
                    help="NDJSON files or directories (default: dashboards/)")
    ap.add_argument("--target", required=True, choices=["opensearch-ui", "osd"],
                    help="opensearch-ui: the Amazon OpenSearch UI application; osd: self-managed OpenSearch Dashboards")
    ap.add_argument("--out", default="out", help="output directory (default: out/)")
    ap.add_argument("--workspace-id", help="workspace whose path prefixes the dashboard links "
                                           "(required for opensearch-ui; omit for OSD without workspaces)")
    ap.add_argument("--opensearch-data-source-id", help="saved-object id of the OpenSearch data source that holds "
                                                        "the logs (required for opensearch-ui; omit for OSD "
                                                        "querying its local cluster)")
    ap.add_argument("--opensearch-data-source-title", help="its title, shown in the query editor (optional)")
    ap.add_argument("--prometheus-connection", action="append", metavar="[OLD=]NEW",
                    help="name of the Prometheus data connection (default: keep the name in the export)")
    ap.add_argument("--logs-index-pattern", action="append", metavar="[OLD=]NEW",
                    help="index pattern of the Hud logs, e.g. 'otel-logs-*' (default: keep)")
    ver = ap.add_argument_group("target version (decides how '$__all' dashboard links are written)")
    ver.add_argument("--osd-version", help="OpenSearch Dashboards version, e.g. 3.9.0")
    ver.add_argument("--osd-url", help="read the version from <url>/api/status")
    ver.add_argument("--osd-user", help="basic-auth user for --osd-url (password from $OSD_PASSWORD)")
    ver.add_argument("--insecure", action="store_true", help="do not verify TLS for --osd-url")
    ver.add_argument("--links", choices=["auto", "all-marker", "version-neutral"], default="auto",
                     help="auto (default): keep '$__all' on OSD >= 3.10, rewrite below 3.10 and when the version is "
                          "unknown (OpenSearch UI does not report it)")
    args = ap.parse_args(argv)
    report = Report()

    out = Path(args.out).resolve()
    loaded = load(args.inputs, report)
    for f in loaded:
        if out == f.resolve().parent or out / OUTPUT_NAME == f.resolve():
            report.error(f"--out {args.out} is the source directory; choose another one")
    lines = [ln for ls in loaded.values() for ln in ls]
    objs = [ln.obj for ln in lines]
    where = {id(ln.obj): ln.where for ln in lines}

    # Target version.
    version = None
    if args.osd_version:
        version = args.osd_version
    elif args.osd_url:
        try:
            version = fetch_osd_version(args.osd_url, args.osd_user, os.environ.get("OSD_PASSWORD"), args.insecure)
            report.note(f"version: {args.osd_url} reports OpenSearch Dashboards {version}")
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            report.error(f"could not read the version from {args.osd_url}/api/status ({e}); pass --osd-version")
    elif args.target == "osd" and args.links == "auto":
        report.error("--target osd needs --osd-version or --osd-url to choose the link form "
                     "(or pass --links explicitly)")
    if version:
        try:
            v = parse_version(version)
            if v < MIN_VERSION:
                report.error(f"OpenSearch Dashboards {version} has no PromQL dashboard variables; 3.9 or later is required")
            if args.links == "auto":
                args.links = "all-marker" if v >= ALL_MARKER_VERSION else "version-neutral"
        except ValueError as e:
            report.error(str(e))
    if args.links == "auto":
        args.links = "version-neutral"
    report.note(f"links: {args.links} (target {args.target}, version {version or 'unknown'})")

    # Values found in the export.
    prom_found = {ds["id"] for o in objs for ds in datasets(o)
                  if str(ds.get("type")).upper() == "PROMETHEUS" and ds.get("id")}
    ip_found = {o["attributes"]["title"] for o in objs if o.get("type") == "index-pattern"}
    ds_found = opensearch_data_sources(objs)
    ws_found = set()
    for o in objs:
        map_strings(o, lambda s: (ws_found.update(WORKSPACE_RE.findall(s)), s)[1])

    if args.target == "opensearch-ui":
        if ws_found and not args.workspace_id:
            report.error("--workspace-id is required: the dashboard links are workspace paths (/w/<id>/app/...)")
        if ds_found and not args.opensearch_data_source_id:
            report.error("--opensearch-data-source-id is required: the logs panels name an OpenSearch data source")

    prom_map = parse_mapping(args.prometheus_connection, "Prometheus connection", prom_found, report)
    ip_map = parse_mapping(args.logs_index_pattern, "index pattern", ip_found, report)
    args.prometheus_names = {prom_map.get(p, p) for p in prom_found}

    if report.errors:
        return finish(report, [])

    rewrite_prometheus(objs, prom_map)
    rewrite_index_pattern(objs, ip_map)
    if ds_found:
        rewrite_data_source(objs, args.opensearch_data_source_id, args.opensearch_data_source_title,
                            remove=not args.opensearch_data_source_id)
    if ws_found or args.workspace_id:
        rewrite_workspace(objs, args.workspace_id)
    if args.links == "version-neutral":
        version_neutral_links(objs, report)

    for label, m in (("Prometheus connection", prom_map), ("index pattern", ip_map)):
        for old, new in m.items():
            report.note(f"{label}: {old} -> {new}")
    if ds_found:
        report.note(f"OpenSearch data source: {', '.join(sorted(ds_found))} -> "
                    f"{args.opensearch_data_source_id or 'removed (local cluster)'}")
    if ws_found or args.workspace_id:
        report.note(f"workspace: {', '.join(sorted(ws_found)) or 'none'} -> {args.workspace_id or 'no prefix'}")

    old_values = {
        "Prometheus connection": set(prom_map),
        "OpenSearch data source": ds_found - {args.opensearch_data_source_id},
        "workspace": ws_found - {args.workspace_id},
    }
    check(objs, where, args, report, old_values)

    written = []
    if not report.errors:
        # One file: an import resolves references only among the objects of that same import.
        out.mkdir(parents=True, exist_ok=True)
        dest = out / OUTPUT_NAME
        body = []
        for ln in lines:
            obj = encode(ln.obj)
            body.append(ln.raw if obj == json.loads(ln.raw) else json.dumps(obj, **_style(ln.raw)))
        dest.write_text("\n".join(body) + "\n", encoding="utf-8")
        written.append(dest)
    return finish(report, written)


def finish(report, written):
    for n in report.notes:
        print(f"  {n}")
    for w in report.warnings:
        print(f"WARN  {w}")
    for e in report.errors:
        print(f"ERROR {e}")
    for f in written:
        print(f"wrote {f}")
    if report.errors:
        print(f"{len(report.errors)} error(s); nothing written")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
