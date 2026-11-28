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
        self.assertEqual(len(usages), 12)  # ticket_file, ticket, slate, rule, retire, yes, no, rules, today, scoreboard, eli5, help
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

    def test_the_database_allows_every_view_event_and_audit_action_the_code_uses(self) -> None:
        migrations = ROOT / "supabase" / "migrations"
        intake = (migrations / "20261127000000_proracle_intake.sql").read_text(encoding="utf-8")
        multi = (migrations / "20261128000000_proracle_multi_rule.sql").read_text(encoding="utf-8")
        slate = (migrations / "20261129000000_proracle_ticket_slate.sql").read_text(encoding="utf-8")
        store = (FUNCTION / "store.ts").read_text(encoding="utf-8")
        adapter = (ROOT / "proracle" / "supabase_adapter.py").read_text(encoding="utf-8")
        for view in ("intake_active_rules", "intake_pending_retirement", "intake_scoreboard"):
            self.assertIn(f"create or replace view proracle.{view}", multi, view)
            self.assertTrue(f'"{view}"' in store or f'"{view}"' in adapter, f"nothing reads {view}")
        self.assertIn('"intake_active_rules"', adapter)  # the Python workflow reads every active rule
        # the newest check constraint is the one in force, so the values must be in the multi-rule file
        events = set(re.findall(r'ruleEvent\(rule, "([A-Z_]+)"', store))
        self.assertEqual(events, {"ACTIVATED", "DECLINED", "RETIRE_REQUESTED", "RETIRED", "RETIRE_DECLINED"})
        for event in events | {"PROPOSED"}:
            self.assertIn(f"'{event}'", multi, event)
        actions = set(re.findall(r'^\s*\| "([A-Z_]+)"', store.split("export type AuditAction")[1].split(";")[0], re.M))
        self.assertGreaterEqual(len(actions), 17)
        # the newest check constraint is the tickets-and-slates one: every action the code writes must be in it
        for action in actions:
            self.assertIn(f"'{action}'", slate, action)
        for action in ("SLATE", "PENDING", "REUSED", "TOKEN_CAP"):
            self.assertIn(action, actions)
        # everything the older migration allowed is still allowed
        for action in re.findall(r"'([A-Z_]+)'", intake.split("action text not null check (action in (")[1].split("))")[0]):
            self.assertIn(f"'{action}'", multi, action)

    def test_everything_the_function_writes_for_tickets_and_slates_exists_in_the_migration(self) -> None:
        migration = (ROOT / "supabase" / "migrations" / "20261129000000_proracle_ticket_slate.sql").read_text(encoding="utf-8")
        store = (FUNCTION / "store.ts").read_text(encoding="utf-8")
        for name in (
            "intake_pending_uploads", "intake_pending_upload", "intake_file_hashes", "intake_rejected_rows", "intake_add_tokens",
            "pending_upload_id", "read_kind", "match_via", "unmatched_reason", "input_tokens", "output_tokens", "file_sha256",
        ):
            self.assertIn(name, store, f"the store no longer uses {name}")
            self.assertIn(name, migration, f"the migration lacks {name}")
        for reason in re.findall(r'"(NOT_A_FIXTURE|MISSING_TEAM|SAME_TEAM|OVER_LIMIT)"', (FUNCTION / "vision.ts").read_text(encoding="utf-8")):
            self.assertIn(f"'{reason}'", migration, reason)

    def test_the_documented_ticket_and_slate_limits_match_the_code_and_the_migration(self) -> None:
        migration = (ROOT / "supabase" / "migrations" / "20261129000000_proracle_ticket_slate.sql").read_text(encoding="utf-8")
        self.assertIn("export const SLATE_MAX_ROWS = 100;", COMMANDS_TS)
        self.assertIn("export const PENDING_UPLOAD_MINUTES = 60;", COMMANDS_TS)
        self.assertIn("export const DEFAULT_TOKEN_BUDGET = 2_000_000;", COMMANDS_TS)
        self.assertIn("rows_kept between 0 and 100", migration)
        self.assertIn("interval '60 minutes'", migration)
        for text in ("up to 100 rows", "kept 60 minutes", "refused, never cut", "2,000,000", "SHA-256", "Telegram file id", "/slate", "/today"):
            self.assertIn(text, DOC, text)
        policy = (ROOT / "docs" / "DATA_POLICY.md").read_text(encoding="utf-8")
        for text in ("Telegram file id", "SHA-256", "never stores or publishes the file"):
            self.assertIn(text, policy, text)
        index = (FUNCTION / "index.ts").read_text(encoding="utf-8")
        self.assertIn('intEnv("INTAKE_DAILY_TOKEN_BUDGET", DEFAULT_TOKEN_BUDGET', index)

    def test_everything_eli5_reads_exists_in_the_migrations_and_is_documented(self) -> None:
        migrations = ROOT / "supabase" / "migrations"
        ticket_slate = (migrations / "20261129000000_proracle_ticket_slate.sql").read_text(encoding="utf-8")
        slate_score = (migrations / "20261130000000_proracle_slate_score.sql").read_text(encoding="utf-8")
        store = (FUNCTION / "store.ts").read_text(encoding="utf-8")
        # the function reads three views; each must be created by a migration and granted to the function's role
        for view, text in (
            ("intake_scoreboard", (migrations / "20261128000000_proracle_multi_rule.sql").read_text(encoding="utf-8")),
            ("intake_recent_window", ticket_slate),
            ("intake_slate_score", slate_score),
        ):
            self.assertIn(f'"{view}"', store, f"the store no longer reads {view}")
            self.assertIn(f"create or replace view proracle.{view} with (security_invoker = true)", text, view)
        self.assertIn("grant select on table proracle.intake_slate_score to service_role;", slate_score)
        self.assertNotIn("proracle_runtime", slate_score.split("revoke all privileges")[1].split("grant select")[1])
        self.assertIn("'ELI5'", ticket_slate)  # the audit action /eli5 writes
        self.assertIn("| `/eli5` |", DOC)  # its row in the command table, not only a mention elsewhere
        for text in ("/eli5", "20 settled picks", "too early to tell", "28 days", "no model call", "20261130000000_proracle_slate_score.sql"):
            self.assertIn(text, DOC, text)
        eli5 = (FUNCTION / "eli5.ts").read_text(encoding="utf-8")
        self.assertIn("MIN_SETTLED_FOR_VERDICT = 20;", eli5)
        self.assertNotIn("fetch(", eli5)  # built from templates and counts: no network, no model call
        self.assertNotRegex(eli5, r"anthropic|vision", )

    def test_a_complete_ticket_means_the_same_in_the_views_the_review_and_the_docs(self) -> None:
        migrations = ROOT / "supabase" / "migrations"
        text = (migrations / "20261201000000_proracle_complete_tickets.sql").read_text(encoding="utf-8")
        for view in ("intake_scoreboard", "intake_recent_window"):
            self.assertIn(f"create or replace view proracle.{view} with (security_invoker = true)", text, view)
        self.assertEqual(text.count("having count(*) <> t.fixture_count"), 2)  # both views carry the rule
        self.assertEqual(text.count("p.arm = 'A')"), 2)
        self.assertEqual(text.count("p.arm = 'C')"), 2)
        self.assertIsNone(re.search(r"^\s*(grant|revoke)\b", text, re.M | re.I))  # no privilege change: only definitions are replaced
        review = (ROOT / "proracle" / "review.py").read_text(encoding="utf-8")
        self.assertIn("def picks_complete(", review)
        tickets_doc = (ROOT / "docs" / "TICKETS.md").read_text(encoding="utf-8")
        for needle in ("exactly one pick per fixture", "`incomplete`", "exits with code 5", "20261201000000_proracle_complete_tickets.sql"):
            self.assertIn(needle, tickets_doc, needle)
        self.assertIn("20261201000000_proracle_complete_tickets.sql", DOC)

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
