"""Static rules for every workflow (docs/MAINTAINING.md), so a later edit cannot quietly widen them."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
FORBIDDEN_TRIGGERS = ("pull_request_target", "workflow_run")
SECRET_WORKFLOWS = ("runtime.yml", "ticket_intake.yml")
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)", re.MULTILINE)
SHA_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def code(path: Path) -> str:
    """The workflow text without comment lines, so rules are checked against what actually runs."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#")) + "\n"


class WorkflowRulesTest(unittest.TestCase):
    def test_workflows_exist(self) -> None:
        self.assertTrue(WORKFLOWS)

    def test_no_dangerous_triggers(self) -> None:
        for path in WORKFLOWS:
            text = code(path)
            for trigger in FORBIDDEN_TRIGGERS:
                self.assertNotIn(trigger, text, f"{path.name} uses {trigger}")

    def test_every_action_is_pinned_to_a_full_sha(self) -> None:
        for path in WORKFLOWS:
            for ref in USES.findall(code(path)):
                self.assertRegex(ref, SHA_PIN, f"{path.name}: {ref} is not SHA-pinned")

    def test_every_workflow_declares_read_only_permissions_and_timeouts(self) -> None:
        for path in WORKFLOWS:
            text = code(path)
            self.assertRegex(text, r"(?m)^permissions:\n  contents: read$", path.name)
            self.assertNotRegex(text, r"(?m)^\s+(contents|id-token|actions): write", path.name)
            self.assertIn("timeout-minutes:", text, path.name)

    def test_only_the_runtime_workflows_touch_secrets_and_they_use_the_environment(self) -> None:
        for path in WORKFLOWS:
            text = code(path)
            if path.name in SECRET_WORKFLOWS:
                self.assertRegex(text, r"(?m)^    environment: production$", path.name)
                triggers = text.split("\njobs:")[0].split("\non:")[1]
                self.assertNotIn("pull_request", triggers, path.name)
                if path.name == "ticket_intake.yml":
                    # started only by the Edge Function's dispatch, never by a push or a schedule
                    self.assertRegex(triggers, r"(?m)^  workflow_dispatch:$")
                    self.assertNotIn("push", triggers)
                    self.assertNotIn("schedule", triggers)
            else:
                self.assertNotIn("secrets.", text, f"{path.name} reads secrets")

    def test_the_intake_workflow_gets_its_dispatched_value_only_through_env(self) -> None:
        text = code(ROOT / ".github" / "workflows" / "ticket_intake.yml")
        self.assertIn("INTAKE_ID: ${{ github.event.inputs.intake_id }}", text)
        self.assertIn('--intake-id "$INTAKE_ID"', text)
        self.assertEqual(len(re.findall(r"github\.event\.inputs\.intake_id", text)), 1)
        self.assertNotRegex(text.lower(), r"service_role")

    def test_ci_runs_the_deno_tests_from_a_checksummed_release_with_no_third_party_action_or_secrets(self) -> None:
        text = code(ROOT / ".github" / "workflows" / "ci.yml")
        # The repository allows GitHub-owned actions only, so a third-party setup action would stop
        # the whole workflow from starting (startup_failure); Deno is a pinned, verified download.
        self.assertNotIn("setup-deno", text)
        self.assertRegex(text, r"VERSION=2\.9\.8")
        self.assertRegex(text, r"SHA256=[0-9a-f]{64}")
        self.assertIn('sha256sum --check -', text)
        self.assertIn("deno test --no-prompt --no-remote supabase/functions/telegram-intake/", text)
        self.assertNotIn("--allow-", text)  # the tests get no permissions at all
        self.assertNotIn("secrets.", text)

    def test_event_values_are_never_interpolated_into_run_scripts(self) -> None:
        for path in WORKFLOWS:
            text = code(path)
            for block in re.findall(r"run: \|\n((?:      .*\n?)+)", text):
                self.assertNotRegex(block, r"\$\{\{\s*(github\.event|inputs|secrets)", path.name)

    def test_the_service_role_key_is_never_named_in_the_runtime_workflow(self) -> None:
        text = code(ROOT / ".github" / "workflows" / "runtime.yml")
        self.assertNotRegex(text.lower(), r"service_role")

    def test_the_p0_capture_switches_are_set_to_exactly_one_only_in_the_runtime_workflow(self) -> None:
        for path in WORKFLOWS:
            text = code(path)
            if path.name == "runtime.yml":
                for name in ("PRORACLE_CAPTURE_SCORES", "PRORACLE_DAILY_TICKET"):
                    self.assertEqual(re.findall(rf'(?m)^\s+{name}: "1"$', text), [f'          {name}: "1"'])
            else:
                self.assertNotIn("PRORACLE_CAPTURE_SCORES", text, path.name)
                self.assertNotIn("PRORACLE_DAILY_TICKET", text, path.name)


if __name__ == "__main__":
    unittest.main()
