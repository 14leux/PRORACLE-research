"""The intake's documentation, the command list in the Edge Function and the migration must agree."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNCTION = ROOT / "supabase" / "functions" / "telegram-intake"
DOC = (ROOT / "docs" / "INTAKE.md").read_text(encoding="utf-8")
COMMANDS_TS = (FUNCTION / "commands.ts").read_text(encoding="utf-8")


class IntakeDocsTests(unittest.TestCase):
    def test_every_command_and_limit_in_the_function_is_in_the_doc(self) -> None:
        usages = re.findall(r'usage: "([^"]+)"', COMMANDS_TS)
        self.assertEqual(len(usages), 8)
        for usage in usages:
            self.assertIn(f"`{usage}`", DOC, usage)

    def test_everything_the_bot_cannot_do_is_in_the_doc(self) -> None:
        block = COMMANDS_TS.split("export const CANNOT")[1].split("];")[0]
        items = re.findall(r'"([^"]+)"', block)
        self.assertEqual(len(items), 5)
        for item in items:
            self.assertIn(item, DOC, item)

    def test_the_documented_limits_match_the_code(self) -> None:
        self.assertIn("export const MAX_FIXTURES = 20;", COMMANDS_TS)
        self.assertIn("export const MAX_FILE_BYTES = 10 * 1024 * 1024;", COMMANDS_TS)
        self.assertIn("export const PENDING_RULE_MINUTES = 60;", COMMANDS_TS)
        for text in ("at most 20 fixtures per ticket", "at most 10 vision reads per UTC day", "files up to 10 MB", "waits 60 minutes"):
            self.assertIn(text, DOC)
        index = (FUNCTION / "index.ts").read_text(encoding="utf-8")
        self.assertIn('intEnv("INTAKE_DAILY_CALL_CAP", 10, 1, 100)', index)
        self.assertIn('need("VISION_MODEL") || "claude-sonnet-5-5"', index)

    def test_every_secret_the_function_reads_is_documented_and_none_is_a_literal(self) -> None:
        index = (FUNCTION / "index.ts").read_text(encoding="utf-8")
        names = set(re.findall(r'need\("([A-Z_]+)"\)', index)) | set(re.findall(r'intEnv\("([A-Z_]+)"', index))
        self.assertTrue({"TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "ALLOWED_CHAT_ID", "ANTHROPIC_API_KEY", "GITHUB_DISPATCH_TOKEN"} <= names)
        for name in names - {"SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"}:
            self.assertIn(f"`{name}`", DOC, name)
        self.assertNotRegex(index, r"sk-ant-|ghp_|github_pat_|\d{8,}:[A-Za-z0-9_-]{30,}")

    def test_the_function_reads_no_secret_from_the_request_and_telegram_is_never_polled(self) -> None:
        sources = "\n".join(p.read_text(encoding="utf-8") for p in FUNCTION.glob("*.ts") if not p.name.endswith("_test.ts") and p.name != "testkit.ts")
        self.assertNotIn("getUpdates", sources)
        self.assertNotIn("parse_mode", sources)  # replies are plain text
        for path in (ROOT / "proracle").glob("*.py"):
            self.assertNotIn("getUpdates", path.read_text(encoding="utf-8"), path.name)

    def test_the_function_config_turns_off_jwt_verification_for_this_function_only(self) -> None:
        config = (ROOT / "supabase" / "config.toml").read_text(encoding="utf-8")
        self.assertIn("[functions.telegram-intake]\nverify_jwt = false", config.replace("\r\n", "\n"))
        self.assertEqual(config.count("verify_jwt"), 1)

    def test_the_doc_states_the_data_policy_points(self) -> None:
        for text in ("never stored", "parsed values", "never committed, shared or published", "in memory", "Actions: read and write"):
            self.assertIn(text, DOC)


if __name__ == "__main__":
    unittest.main()
