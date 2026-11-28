"""The user manual, the docs and the bot's command list must agree (no network, no secrets).

The bot's Telegram menu comes from one list in the Edge Function (`commands.ts`). The manual's cheat sheet
has to show every command in that menu, so the manual cannot drift behind a new command. The old meaning of
`/ticket` (showing today's tickets) is now `/today`, and no document may still say otherwise.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNCTION = ROOT / "supabase" / "functions" / "telegram-intake"
COMMANDS_TS = (FUNCTION / "commands.ts").read_text(encoding="utf-8")
MANUAL = (ROOT / "docs" / "USER_MANUAL.html").read_text(encoding="utf-8")


def menu_commands() -> list[str]:
    """Names of the commands that have a Telegram menu entry (a `menu:` field), in list order."""
    body = COMMANDS_TS.split("export const COMMANDS", 1)[1].split("/** Telegram's command menu", 1)[0]
    entries = body.split('name: "')[1:]
    names = []
    for entry in entries:
        name = entry.split('"', 1)[0]
        # each entry runs up to the next `name:`; a menu entry is a `menu:` field inside it
        if re.search(r'\bmenu: "', entry):
            names.append(name)
    return names


def cheat_sheet() -> str:
    section = MANUAL.split('<section id="card">', 1)[1].split("</section>", 1)[0]
    return section


class ManualTests(unittest.TestCase):
    def test_the_menu_has_the_commands_this_check_expects(self) -> None:
        names = menu_commands()
        for expected in ("ticket", "slate", "rule", "retire", "rules", "today", "scoreboard", "eli5", "help"):
            self.assertIn(expected, names)
        self.assertNotIn("yes", names)  # YES and NO are plain words, not slash commands

    def test_every_menu_command_appears_in_the_manuals_cheat_sheet(self) -> None:
        sheet = cheat_sheet()
        shown = set(re.findall(r"<code>(/[a-z0-9_]+)", sheet))
        for name in menu_commands():
            self.assertIn(f"/{name}", shown, f"/{name} is in the bot's menu but not in the manual's cheat sheet")

    def test_the_cheat_sheet_names_no_command_the_bot_does_not_have(self) -> None:
        known = {f"/{name}" for name in re.findall(r'name: "([a-z0-9_]+)"', COMMANDS_TS)} | {"/start"}
        shown = set(re.findall(r"<code>(/[a-z0-9_]+)", cheat_sheet()))
        self.assertEqual(shown - known, set())

    def test_the_manual_covers_tickets_slates_the_question_the_refusal_the_review_and_eli5(self) -> None:
        for text in (
            "Ticket or slate?",
            "Is this a ticket or a slate?",
            "up to 20 fixtures",
            "up to 100 fixtures",
            "I did not cut it",
            "one report per ticket",
            "36 hours after the last kickoff",
            "/eli5",
            "too early to tell",
            "at least 20 settled picks",
            "never touches the report card",
        ):
            self.assertIn(text, MANUAL, text)

    def test_no_document_still_uses_the_ticket_command_for_todays_tickets(self) -> None:
        paths = [ROOT / "README.md", ROOT / "docs" / "USER_MANUAL.html", *sorted((ROOT / "docs").glob("*.md"))]
        for path in paths:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                line = re.sub(r"<[^>]+>", " ", line)  # the manual is HTML: read the words, not the tags
                # `/ticket` described as showing today's tickets, in either order (a list of commands is fine)
                if re.search(r"/ticket\b[^,/\n]{0,40}\btoday|\btoday[^,/\n]{0,40}/ticket\b", line, re.I):
                    self.fail(f"{path.name}:{number} ties /ticket to today: {line[:120]}")
        # and the sample help in the manual no longer lists /ticket as the way to see today's tickets
        self.assertIn("/today", cheat_sheet())

    def test_the_manuals_limits_match_the_code(self) -> None:
        for text in ("export const MAX_FIXTURES = 20;", "export const SLATE_MAX_ROWS = 100;", "export const PENDING_UPLOAD_MINUTES = 60;"):
            self.assertIn(text, COMMANDS_TS)
        sheet = cheat_sheet()
        for text in ("20 fixtures per ticket, 100 per slate", "60 minutes"):
            self.assertIn(text, sheet)


if __name__ == "__main__":
    unittest.main()
