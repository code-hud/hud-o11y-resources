"""Checks the Prometheus alert rules in opensearch-ui/rules.

Run from the repository root:
    python3 -m unittest discover -s opensearch-ui/tests -v

Standard library only. promtool, when it is on PATH, also parses every file.
"""

import pathlib
import shutil
import subprocess
import unittest

RULES = pathlib.Path(__file__).resolve().parent.parent / "rules"


def text(name):
    return (RULES / name).read_text(encoding="utf-8")


class AlertRulesTest(unittest.TestCase):
    def test_promtool_parses_every_file(self):
        promtool = shutil.which("promtool")
        if promtool is None:
            self.skipTest("promtool is not installed")
        result = subprocess.run(
            [promtool, "check", "rules", *sorted(str(p) for p in RULES.glob("*.yaml"))],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_flow_errors_split_sqs_from_other_flows(self):
        body = text("hud-flow-errors.rules.yaml")
        self.assertEqual(body.count("alert: HudFlowErrors"), 2)
        other, sqs = body.split("alert: HudFlowErrors")[1:]
        self.assertIn('code_flow_type!="sqs"', other)
        self.assertIn("code_flow_duration_seconds", other)
        self.assertNotIn("hud_flow_messages_total", other)
        self.assertIn('code_flow_type="sqs"', sqs)
        self.assertIn("hud_flow_messages_total", sqs)
        self.assertNotIn("code_flow_duration_seconds", sqs)
        for part in (other, sqs):
            self.assertIn('http_response_status_code!~"4.."', part)
            self.assertIn("[10m] offset 10m", part)
            self.assertIn("group_left", part)
            self.assertIn(">= 0.01", part)
            self.assertIn(">= 1", part)

    def test_error_rate_spike_uses_messages_for_sqs_only(self):
        body = text("hud-flow-error-rate-spike.rules.yaml")
        expr = body.split("expr: |", 1)[1].split("for:", 1)[0]
        self.assertEqual(expr.count('code_flow_type!="sqs"'), 4)
        self.assertEqual(expr.count('code_flow_type="sqs"'), 4)
        self.assertEqual(expr.count("hud_flow_messages_total"), 4)
        self.assertNotIn("code_flow_duration_seconds[1h]", expr)
        self.assertIn('http_response_status_code!~"4.."', expr)
        self.assertIn("[8d:1h] offset 1h", expr)
        self.assertIn(">= 0.05", expr)
        self.assertIn(">= 50", expr)
        self.assertIn(">= 20", expr)

    def test_degradation_rules_keep_their_windows(self):
        wow = text("hud-flow-week-over-week.rules.yaml")
        dow = text("hud-flow-day-over-week.rules.yaml")
        spike = text("hud-flow-latency-spike.rules.yaml")
        function = text("hud-function-day-over-week.rules.yaml")
        self.assertIn("[7d:1h] offset 7d", wow)
        self.assertIn(">= 84", wow)
        self.assertIn(">= 350", wow)
        self.assertIn(">= 1.7", wow)
        self.assertIn("2.58", wow)
        self.assertIn("[7d:1h] offset 1d", dow)
        self.assertIn("[1d:1h]", dow)
        self.assertIn(">= 12", dow)
        self.assertIn(">= 100", dow)
        self.assertIn("[23h50m:10m] offset 10m", spike)
        self.assertNotIn("increase(code_flow_duration_seconds[1h])", spike)
        self.assertIn(">= 72", spike)
        self.assertIn(">= 0.3", spike)
        self.assertIn("10m p90", spike)
        self.assertIn("__NO_FUNCTION_SELECTED__", function)
        self.assertIn("code_function_calls_total", function)
        self.assertNotIn("2.58", function)
        for body in (wow, dow, spike, function):
            self.assertIn("deployment_environment_name", body)


if __name__ == "__main__":
    unittest.main()
