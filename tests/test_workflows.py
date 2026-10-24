"""Static rules for every workflow (docs/MAINTAINING.md), so a later edit cannot quietly widen them."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
FORBIDDEN_TRIGGERS = ("pull_request_target", "workflow_run")
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

    def test_only_the_runtime_workflow_touches_secrets_and_it_uses_the_environment(self) -> None:
        for path in WORKFLOWS:
            text = code(path)
            if path.name == "runtime.yml":
                self.assertRegex(text, r"(?m)^    environment: production$")
                triggers = text.split("\njobs:")[0].split("\non:")[1]
                self.assertNotIn("pull_request", triggers)
            else:
                self.assertNotIn("secrets.", text, f"{path.name} reads secrets")

    def test_event_values_are_never_interpolated_into_run_scripts(self) -> None:
        for path in WORKFLOWS:
            text = code(path)
            for block in re.findall(r"run: \|\n((?:      .*\n?)+)", text):
                self.assertNotRegex(block, r"\$\{\{\s*(github\.event|inputs|secrets)", path.name)

    def test_the_service_role_key_is_never_named_in_the_runtime_workflow(self) -> None:
        text = code(ROOT / ".github" / "workflows" / "runtime.yml")
        self.assertNotRegex(text.lower(), r"service_role")


if __name__ == "__main__":
    unittest.main()
