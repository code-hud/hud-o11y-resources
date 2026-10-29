import contextlib
import importlib.util
import io
import json
import re
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARDS = ROOT / "dashboards"

_spec = importlib.util.spec_from_file_location("dashboard_loader", ROOT / "scripts" / "dashboard-loader.py")
loader = importlib.util.module_from_spec(_spec)
sys.modules["dashboard_loader"] = loader
_spec.loader.exec_module(loader)

TARGET_ARN = "arn:aws:opensearch:us-east-1:123456789012:datasource/prom-0"
LINK_RE = re.compile(r"(?:/w/[^/\"\\]+)?/app/dashboards#/view/([^?\"\\]+)\?[^\"\\]*")


def read_ndjson(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def source_objects():
    return [o for f in sorted(DASHBOARDS.glob("*.ndjson")) for o in read_ndjson(f) if "type" in o]


def variables(o):
    return json.loads(o["attributes"].get("variablesJSON") or "{}").get("variables", [])


def datasets(x, found=None):
    found = [] if found is None else found
    if isinstance(x, str) and x[:1] in "{[":
        try:
            x = json.loads(x)
        except ValueError:
            return found
    if isinstance(x, dict):
        if isinstance(x.get("dataset"), dict):
            found.append(x["dataset"])
        for v in x.values():
            datasets(v, found)
    elif isinstance(x, list):
        for v in x:
            datasets(v, found)
    return found


def links(text):
    return [m.group(0) for m in LINK_RE.finditer(text.replace('\\"', '"'))]


class DashboardLoaderTest(unittest.TestCase):
    """Runs against the dashboards in dashboards/, whatever they are."""

    @classmethod
    def setUpClass(cls):
        cls.objs = source_objects()
        if not cls.objs:
            raise unittest.SkipTest("no saved objects under dashboards/")
        cls.text = "\n".join(f.read_text() for f in sorted(DASHBOARDS.glob("*.ndjson")))
        cls.prometheus = {d["id"] for o in cls.objs for d in datasets(o) if str(d.get("type")).upper() == "PROMETHEUS"}
        cls.index_patterns = {o["attributes"]["title"] for o in cls.objs if o["type"] == "index-pattern"}
        cls.has_data_source = '"data-source"' in cls.text or "OpenSearch" in cls.text
        unique = {(o["type"], o["id"]): o for o in cls.objs}
        cls.keys = set(unique)
        cls.all_links = [ln for o in unique.values() for ln in links(json.dumps(o)) if "'$__all'" in ln]
        cls.arns = {d["dataSource"]["meta"]["arn"] for o in cls.objs for d in datasets(o)
                    if isinstance(d.get("dataSource"), dict) and isinstance(d["dataSource"].get("meta"), dict)
                    and d["dataSource"]["meta"].get("arn")}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_loader(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = loader.main([str(a) for a in argv])
        return code, buf.getvalue()

    def opensearch_ui_args(self, out):
        args = ["--target", "opensearch-ui", "--workspace-id", "ws42", "--opensearch-data-source-id", "ds-9",
                "--out", out]
        args += [f"--prometheus-connection={p}=prom-{i}" for i, p in enumerate(sorted(self.prometheus))]
        args += [f"--logs-index-pattern={p}=renamed-{i}-*" for i, p in enumerate(sorted(self.index_patterns))]
        if self.arns:
            args += ["--prometheus-arn", TARGET_ARN]
        return args

    def copy_dashboards(self, edit=lambda text: text):
        src = self.tmp / "src"
        src.mkdir()
        for f in DASHBOARDS.glob("*.ndjson"):
            (src / f.name).write_text(edit(f.read_text()))
        return src

    def test_unchanged_objects_stay_byte_identical(self):
        report = loader.Report()
        for lines in loader.load([DASHBOARDS], report).values():
            for ln in lines:
                self.assertEqual(json.dumps(loader.encode(ln.obj), **loader._style(ln.raw)), ln.raw, ln.where)

    def test_opensearch_ui(self):
        out = self.tmp / "out"
        code, log = self.run_loader(DASHBOARDS, *self.opensearch_ui_args(out))
        self.assertEqual(code, 0, log)
        result = read_ndjson(out / "hud.ndjson")
        text = (out / "hud.ndjson").read_text()
        self.assertEqual({(o["type"], o["id"]) for o in result}, self.keys)
        self.assertEqual(len(result), len(self.keys))
        self.assertIsNone(loader.PLACEHOLDER_RE.search(text))
        for ln in links(text):
            self.assertTrue(ln.startswith("/w/ws42/app/"), ln)
            self.assertNotIn("'$__all'", ln)
        for d in (d for o in result for d in datasets(o)):
            if str(d.get("type")).upper() == "PROMETHEUS":
                self.assertTrue(d["id"].startswith("prom-"), d)
            if isinstance(d.get("dataSource"), dict) and d["dataSource"].get("id"):
                self.assertEqual(d["dataSource"]["id"], "ds-9")
            if isinstance(d.get("dataSource"), dict) and isinstance(d["dataSource"].get("meta"), dict):
                self.assertEqual(d["dataSource"]["meta"].get("arn"), TARGET_ARN)
        ids = {(o["type"], o["id"]) for o in result}
        for o in result:
            for r in o.get("references", []):
                if r["type"] == "data-source":
                    self.assertEqual(r["id"], "ds-9")
                else:
                    self.assertIn((r["type"], r["id"]), ids, o["id"])
            if o["type"] == "index-pattern":
                self.assertTrue(o["attributes"]["title"].startswith("renamed-"))
        for pattern in self.index_patterns:
            self.assertNotIn(f"source = {pattern}", text)
        self.assertEqual("\n".join(f.read_text() for f in sorted(DASHBOARDS.glob("*.ndjson"))), self.text)

    def test_opensearch_ui_requires_ids(self):
        if not self.has_data_source and not any("/w/" in ln for ln in links(self.text)):
            self.skipTest("the dashboards name no workspace or OpenSearch data source")
        code, log = self.run_loader(DASHBOARDS, "--target", "opensearch-ui", "--out", self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("is required", log)
        self.assertFalse((self.tmp / "out").exists())

    def test_osd_local_cluster_keeps_all_marker_on_3_10(self):
        out = self.tmp / "out"
        code, log = self.run_loader(DASHBOARDS, "--target", "osd", "--osd-version", "3.10.0", "--out", out)
        self.assertEqual(code, 0, log)
        result = read_ndjson(out / "hud.ndjson")
        text = (out / "hud.ndjson").read_text()
        self.assertFalse(any(r["type"] == "data-source" for o in result for r in o.get("references", [])))
        self.assertNotIn("arn:aws", text)
        for ln in links(text):
            self.assertTrue(ln.startswith("/app/"), ln)
        self.assertEqual(len([ln for ln in links(text) if "'$__all'" in ln]), len(self.all_links))

    def test_version_neutral_links_chain_variables(self):
        if not self.all_links:
            self.skipTest("no dashboard link uses '$__all'")
        out = self.tmp / "out"
        code, log = self.run_loader(DASHBOARDS, "--target", "osd", "--osd-version", "3.9.0", "--out", out)
        self.assertEqual(code, 0, log)
        result = {o["id"]: o for o in read_ndjson(out / "hud.ndjson")}
        source = {o["id"]: o for o in self.objs}
        for ln in self.all_links:
            target = LINK_RE.match(ln).group(1)
            entries = re.findall(r"\(id:([^,()]+),current:!\(([^()]*)\)\)", ln)
            kept = [i for i, c in entries if c != "'$__all'"]
            dropped = [i for i, c in entries if c == "'$__all'"]
            got = {v["name"]: v for v in variables(result[target])}
            names = [v["name"] for v in variables(result[target])]
            self.assertEqual(sorted(names), sorted(v["name"] for v in variables(source[target])))
            for k in kept:
                for d in dropped:
                    self.assertLess(names.index(k), names.index(d), f"{target}: {k} before {d}")
                    matchers = got[d]["promQLResourceQuery"]["matchers"]
                    self.assertIn({"label": got[k]["promQLResourceQuery"]["label"], "operator": "=",
                                   "value": "$" + k}, matchers, f"{target}: {d}")
        for ln in links((out / "hud.ndjson").read_text()):
            self.assertNotIn("'$__all'", ln)
        for dash in (o for o in result.values() if o["type"] == "dashboard"):
            names = [v["name"] for v in variables(dash)]
            for i, v in enumerate(variables(dash)):
                self.assertNotIn("$__all", json.dumps(v.get("current")), f"{dash['id']}: {v['name']}")
                for m in (v.get("promQLResourceQuery") or {}).get("matchers", []):
                    refs = set(re.findall(r"\$\{?(\w+)", m["value"])) & set(names)
                    self.assertTrue(refs <= set(names[:i]), f"{dash['id']}: {v['name']} filters on {m['value']}")

    def test_opensearch_ui_requires_prometheus_arn(self):
        if not self.arns:
            self.skipTest("the dashboards carry no Prometheus data source ARN")
        args = self.opensearch_ui_args(self.tmp / "out")
        args = args[:args.index("--prometheus-arn")]
        code, log = self.run_loader(DASHBOARDS, *args)
        self.assertEqual(code, 1)
        self.assertIn("--prometheus-arn is required", log)

    def test_copies_of_an_object_that_differ_are_an_error(self):
        target = next(o for o in self.objs if o["type"] == "dashboard")
        src = self.copy_dashboards()
        changed = dict(target, attributes=dict(target["attributes"], title="changed"))
        (src / "zz-extra.ndjson").write_text(json.dumps(changed) + "\n")
        code, log = self.run_loader(src, "--target", "osd", "--osd-version", "3.10", "--out", self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn(f"dashboard {target['id']} differs between", log)

    def test_dangling_dashboard_link_is_an_error(self):
        targets = {LINK_RE.match(ln).group(1) for ln in links(self.text)}
        if not targets:
            self.skipTest("no links between dashboards")
        target = sorted(targets)[0]
        src = self.copy_dashboards(lambda text: "\n".join(
            ln for ln in text.splitlines() if not (ln.strip() and json.loads(ln).get("id") == target
                                                   and json.loads(ln).get("type") == "dashboard")) + "\n")
        code, log = self.run_loader(src, "--target", "osd", "--osd-version", "3.10", "--out", self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn(f"link to dashboard {target}, which is not in the output", log)

    def test_dangling_index_pattern_is_an_error(self):
        pattern = next((o for o in self.objs if o["type"] == "index-pattern"), None)
        if pattern is None:
            self.skipTest("no index pattern")
        src = self.copy_dashboards(lambda text: "\n".join(
            ln.replace(f'"id": "{pattern["id"]}"', '"id": "other"').replace(f'"id":"{pattern["id"]}"', '"id":"other"')
            if ln.strip() and json.loads(ln).get("type") == "index-pattern" else ln
            for ln in text.splitlines()) + "\n")
        code, log = self.run_loader(src, "--target", "osd", "--osd-version", "3.10", "--out", self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn(f"index-pattern {pattern['id']} is not in the output", log)
        self.assertFalse((self.tmp / "out").exists())

    def test_leftover_placeholder_is_an_error(self):
        target = next(iter(self.prometheus or self.index_patterns), None)
        if target is None:
            self.skipTest("no Prometheus connection or index pattern to replace")
        src = self.copy_dashboards(lambda text: text.replace(target, "HUD_SOMETHING"))
        code, log = self.run_loader(src, "--target", "osd", "--osd-version", "3.10", "--out", self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("placeholder HUD_SOMETHING is still present", log)

    def test_refuses_to_write_into_the_source_directory(self):
        src = self.copy_dashboards()
        before = {f.name: f.read_text() for f in src.iterdir()}
        for out in (src, src / "."):
            code, log = self.run_loader(src, "--target", "osd", "--osd-version", "3.10", "--out", out)
            self.assertEqual(code, 1)
            self.assertIn("is the source directory", log)
        self.assertEqual({f.name: f.read_text() for f in src.iterdir()}, before)

    def test_version_below_3_9_is_rejected(self):
        code, log = self.run_loader(DASHBOARDS, "--target", "osd", "--osd-version", "2.19.3", "--out", self.tmp / "o")
        self.assertEqual(code, 1)
        self.assertIn("3.9 or later is required", log)

    def test_version_from_api_status(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["path"], seen["auth"] = self.path, self.headers.get("Authorization")
                body = json.dumps({"version": {"number": "3.9.0"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        out = self.tmp / "out"
        code, log = self.run_loader(DASHBOARDS, "--target", "osd", "--osd-url",
                                    f"http://127.0.0.1:{server.server_port}", "--osd-user", "admin", "--out", out)
        self.assertEqual(code, 0, log)
        self.assertEqual(seen["path"], "/api/status")
        self.assertTrue(seen["auth"].startswith("Basic "))
        self.assertIn("reports OpenSearch Dashboards 3.9.0", log)
        self.assertIn("links: version-neutral", log)


if __name__ == "__main__":
    unittest.main()
