import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
UI_EXPORT = FIXTURES / "opensearch-ui-export"
CATALOG = FIXTURES / "catalog-pr1"
sys.path.insert(0, str(ROOT / "scripts"))

import adapt  # noqa: E402

CATALOG_BAD_PATTERN = "f2654070-c6dd-11f1-a994-5ff5adef1b8f"
CATALOG_PATTERN = "hud-logs-otel-index-pattern"


def read_ndjson(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def dashboards_variables(out_dir):
    found = {}
    for f in Path(out_dir).glob("*.ndjson"):
        for o in read_ndjson(f):
            if o["type"] == "dashboard":
                found[o["id"]] = json.loads(o["attributes"].get("variablesJSON") or "{}").get("variables", [])
    return found


class AdaptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_adapt(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = adapt.main([str(a) for a in argv])
        return code, buf.getvalue()

    def fixed_catalog(self):
        d = self.tmp / "catalog"
        d.mkdir()
        for f in CATALOG.glob("*.ndjson"):
            (d / f.name).write_text(f.read_text().replace(CATALOG_BAD_PATTERN, CATALOG_PATTERN))
        return d

    def test_unchanged_objects_stay_byte_identical(self):
        for src in (UI_EXPORT, CATALOG):
            report = adapt.Report()
            for f, lines in adapt.load([src], report).items():
                for ln in lines:
                    self.assertEqual(json.dumps(adapt.encode(ln.obj), **adapt._style(ln.raw)), ln.raw, ln.where)

    def test_opensearch_ui(self):
        out = self.tmp / "out"
        code, log = self.run_adapt(UI_EXPORT, "--target", "opensearch-ui", "--workspace-id", "ws42",
                                   "--opensearch-data-source-id", "ds-9", "--prometheus-connection", "my_amp",
                                   "--logs-index-pattern", "otel-logs-*", "--out", out)
        self.assertEqual(code, 0, log)
        text = (out / "hud.ndjson").read_text()
        for gone in ("HUD_WORKSPACE_ID", "HUD_OPENSEARCH_DATA_SOURCE_ID", "hud_amp", "logs-otel-v1*", "'$__all'"):
            self.assertNotIn(gone, text)
        self.assertEqual(text.count("/w/ws42/app/dashboards#"), 15)
        self.assertIn("source = otel-logs-*", text)
        objs = read_ndjson(out / "hud.ndjson")
        pattern = next(o for o in objs if o["type"] == "index-pattern")
        self.assertEqual(pattern["attributes"]["title"], "otel-logs-*")
        self.assertEqual(pattern["references"], [{"id": "ds-9", "name": "dataSource", "type": "data-source"}])
        names = [v["name"] for v in dashboards_variables(out)["hud-dog-endpoint-dashboard-sdk"]]
        self.assertEqual(names, ["endpoint", "service"])
        self.assertEqual(UI_EXPORT.joinpath("hud.ndjson").read_text().count("HUD_WORKSPACE_ID"), 15)

    def test_opensearch_ui_requires_ids(self):
        code, log = self.run_adapt(UI_EXPORT, "--target", "opensearch-ui", "--out", self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("--workspace-id is required", log)
        self.assertIn("--opensearch-data-source-id is required", log)
        self.assertFalse((self.tmp / "out").exists())

    def test_osd_local_cluster_keeps_all_marker_on_3_10(self):
        out = self.tmp / "out"
        code, log = self.run_adapt(UI_EXPORT, "--target", "osd", "--osd-version", "3.10.0", "--out", out)
        self.assertEqual(code, 0, log)
        text = (out / "hud.ndjson").read_text()
        self.assertNotIn("/w/", text)
        self.assertNotIn("data-source", text)
        self.assertNotIn("HUD_OPENSEARCH_DATA_SOURCE_ID", text)
        self.assertEqual(text.count("'$__all'"), 15)
        self.assertIn('"/app/dashboards#/view/', text.replace('\\"', '"'))

    def test_catalog_version_neutral_matches_verified_prototype(self):
        out = self.tmp / "out"
        code, log = self.run_adapt(self.fixed_catalog(), "--target", "osd", "--osd-version", "3.9.0", "--out", out)
        self.assertEqual(code, 0, log)
        expected = json.loads((FIXTURES / "catalog-pr1-version-neutral-variables.json").read_text())
        got = dashboards_variables(out)
        for dash, variables in expected.items():
            self.assertEqual(got[dash], variables, dash)
        self.assertNotIn("'$__all'", (out / "hud.ndjson").read_text())

    def test_catalog_dangling_index_pattern_is_reported(self):
        code, log = self.run_adapt(CATALOG, "--target", "osd", "--osd-version", "3.10.0", "--out", self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertEqual(log.count(f"index-pattern {CATALOG_BAD_PATTERN} is not in the output"), 14)
        self.assertFalse((self.tmp / "out").exists())

    def test_several_files_and_rename_with_old_new(self):
        out = self.tmp / "out"
        code, log = self.run_adapt(self.fixed_catalog(), "--target", "osd", "--osd-version", "3.10",
                                   "--prometheus-connection", "hud_prometheus=prom2", "--out", out)
        self.assertEqual(code, 0, log)
        self.assertEqual([f.name for f in out.iterdir()], ["hud.ndjson"])
        sources = sum(len(read_ndjson(f)) for f in CATALOG.glob("*.ndjson"))
        self.assertEqual(len(read_ndjson(out / "hud.ndjson")), sources)
        self.assertNotIn("hud_prometheus", (out / "hud.ndjson").read_text())

    def test_dangling_dashboard_link_and_placeholder_are_errors(self):
        src = self.tmp / "src"
        src.mkdir()
        lines = (UI_EXPORT / "hud.ndjson").read_text().splitlines()
        kept = [ln for ln in lines if '"id": "hud-dog-endpoint-dashboard-sdk"' not in ln
                and '"id":"hud-dog-endpoint-dashboard-sdk"' not in ln]
        self.assertEqual(len(kept), len(lines) - 1)
        (src / "hud.ndjson").write_text("\n".join(kept) + "\n")
        code, log = self.run_adapt(src, "--target", "osd", "--osd-version", "3.10", "--out", self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("link to dashboard hud-dog-endpoint-dashboard-sdk, which is not in the output", log)

        (src / "hud.ndjson").write_text((UI_EXPORT / "hud.ndjson").read_text().replace("hud_amp", "HUD_PROMETHEUS"))
        code, log = self.run_adapt(src, "--target", "osd", "--osd-version", "3.10", "--out", self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("placeholder HUD_PROMETHEUS is still present", log)

    def test_refuses_to_write_into_the_source_directory(self):
        src = self.tmp / "src"
        shutil.copytree(UI_EXPORT, src)
        before = (src / "hud.ndjson").read_text()
        for out in (src, src / "."):
            code, log = self.run_adapt(src / "hud.ndjson", "--target", "osd", "--osd-version", "3.10", "--out", out)
            self.assertEqual(code, 1)
            self.assertIn("is the source directory", log)
        self.assertEqual((src / "hud.ndjson").read_text(), before)

    def test_version_below_3_9_is_rejected(self):
        code, log = self.run_adapt(UI_EXPORT, "--target", "osd", "--osd-version", "2.19.3", "--out", self.tmp / "o")
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
        code, log = self.run_adapt(UI_EXPORT, "--target", "osd", "--osd-url",
                                   f"http://127.0.0.1:{server.server_port}", "--osd-user", "admin", "--out", out)
        self.assertEqual(code, 0, log)
        self.assertEqual(seen["path"], "/api/status")
        self.assertTrue(seen["auth"].startswith("Basic "))
        self.assertIn("reports OpenSearch Dashboards 3.9.0", log)
        self.assertNotIn("'$__all'", (out / "hud.ndjson").read_text())


if __name__ == "__main__":
    unittest.main()
