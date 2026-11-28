# Telegram intake (optional)

Send your own bot a screenshot or PDF of fixtures from a betting site. The intake reads it, matches the fixtures to the ones PRORACLE already stores, then freezes what each arm would have picked **before kickoff** so the arms can be scored later. It is a measurement tool for calibration and for testing rules; it is not betting advice and makes no claim about returns.

It is optional, built as a Supabase Edge Function plus one workflow, and **it does nothing until you deploy it and register a webhook**. Nothing in this repository deploys it.

New to it? [USER_MANUAL.html](USER_MANUAL.html) is a picture-first walk-through (open it in a browser; it works offline) with Telegram examples, a rule builder and copy-paste queries to see the data. This page is the reference.

## How it works

1. Telegram calls your Edge Function `telegram-intake` (a webhook).
2. The function checks the secret-token header in constant time, then that the message comes from your private chat only. Anything else is ignored and logged without its content.
3. For a photo, an image file or a PDF the caption decides what it is: the word `ticket` or `slate` reads it as that. With no marker (or both words) it asks "ticket or slate?" and does nothing else: nothing is downloaded, read or counted against a daily limit until you answer `/ticket` or `/slate` (see [Tickets and slates](#tickets-and-slates)). A read then takes one of today's vision calls (a daily cap, and a daily token budget beside it), reads the file in memory with one call to the Anthropic API (the model is told to call a single tool; any other reply is discarded), and validates the result in code: strict field checks, odds only when all three prices are valid. Rows it cannot use are not dropped silently: each is stored as a rejected row with a fixed reason (not a fixture, a team name missing or unreadable, the same team on both sides, or past the 100-row limit). The reading prompt tells the model to leave out the small market counter some odds listings print after the away team (16, 23 or 25 seen) but to keep digits that belong to a real name (`Stade Brest 29`), and to read the country label and, for finished games, the final score when they are printed; the country and score are validated and used as hints only for now (not stored).
4. It matches each row to a stored scheduled fixture whose kickoff is within three hours, home with home and away with away, at the best level any candidate reaches: **exact** (names equal after normalising case, accents, apostrophes and club affixes such as FC, with the `aa`, `oe` and `ae` spellings folded to one letter so `Tromsoe` equals `Tromsø`; the same words in a different order also count, so `Volos NPS` equals `NPS Volos`), then **remembered** (a name that an earlier ticket of yours already matched to that stored name, or a name on the built-in list of official names and English spellings in `seed_aliases.ts`, every entry of which was seen on a real ticket; the memory wins where the two disagree), then **similar** (every word of the shorter name is in the longer one, for example `BRIGHTON` and `Brighton & Hove Albion`; not when the extra words mark another side, such as U21, Women, II or B, and not for names that are only generic words such as United or City). If exactly one fixture fits at that level it is used; two or more is reported as ambiguous. Only when no fixture fits both names does **one team** decide: if exactly one fixture inside the three hours fits one team (at any level above, home with home or away with away) and the other name fits nothing, the other name is taken as another spelling of that fixture's other team, because a team cannot play twice within three hours; two such fixtures is ambiguous. Set `INTAKE_ANCHOR_MATCHING` to `off` to turn this fallback off without a code change. Rows matched by similarity or by one team are listed in the reply so you can check them, and every match result carries how it matched (exact, remembered, similar or one-team; the reply and the audit line show it, and it is not yet stored per row), and every match is remembered for the next ticket (read back from the stored tickets, so nothing extra is kept). Anything else is reported as unmatched and is not used.
5. It stores the parsed rows (teams, kickoff, odds, bookmaker label, and how each one matched or why it did not) with the read's kind and token counts. For a ticket it then asks GitHub to run the `Ticket intake` workflow with a narrow token; a slate is stored and nothing is frozen.
6. `python -m proracle.ticket_mode` freezes the arms in the existing ticket tables and replies in your chat. The reply shows the ticket's last kickoff and its review due time (36 hours after it, or sooner once every fixture has a result), each in UTC and EAT. A stored slate is never frozen, even if one were dispatched by mistake.
7. After the fixtures, `python -m proracle.review` (started by hand from the `Ticket review` workflow) sends one report per settled or overdue ticket; see [TICKETS.md](TICKETS.md#tickets-slates-and-the-review).

## What you can do

Everything below comes from one list in `supabase/functions/telegram-intake/commands.ts`; `/help` and Telegram's command menu are generated from it, and a test fails if a command or limit is missing from the help or if the menu and the slash commands differ.

**Changing a command:** edit its entry in that list only (`usage`, `summary` and, for a slash command, `menu`), update the table below, then deploy. Tests fail if the routing table, `/help`, the menu or this table disagree with the list, and the deployed function brings Telegram's menu in step with the next message you send it.

| Command | What it does |
|---|---|
| `send a photo, an image file (PNG, JPEG, WebP) or a PDF of fixtures` | With `ticket` or `slate` in the caption it reads the file as that; without one it asks which and reads nothing yet. A ticket (up to 20 rows) is matched and arm A, arm C and one arm B per active rule are frozen before kickoff. A slate (up to 100 rows) is matched and stored for testing models with nothing frozen. |
| `/ticket` | Answers the question about the upload waiting (the latest one, kept 60 minutes): reads it as a ticket. More than 20 rows is refused, never cut. With nothing waiting it asks you to send the file first. |
| `/slate` | Answers the question about the upload waiting: reads it as a slate, up to 100 rows, no frozen picks, off the scoreboard. With nothing waiting it asks you to send the file first. |
| `/rule <directives separated by ;>` | Proposes a rule for arm B. It is echoed back and used only after YES. Up to 5 rules run at once, each as its own arm (rule v3 is arm B3). |
| `/retire <version>` | Asks to stop one active rule. It stops only after YES (it waits 60 minutes); its earlier picks stay and are still scored. |
| `YES` | Confirms what was last asked about: activates the proposed rule, or retires the rule asked about (it waits 60 minutes). |
| `NO` | Cancels the proposed rule, or the retirement asked about. |
| `/rules` | Shows every active rule and any proposal or retirement waiting. |
| `/today` | Shows today's tickets and each arm's picks. |
| `/scoreboard` | Hit rate of each arm and each rule on settled ticket picks, with what arms A and C picked on the same fixtures and a rough margin of error, labelled a calibration report card. |
| `/eli5` | How the picks are doing, in plain words: each arm over the last 7 days and the last 28 days beside arm A on the same matches, and whether the two windows agree. An arm is called working or dropping only with at least 20 settled picks in a window and a gap from A bigger than the margin of error; otherwise it says too early to tell. It also points out any ticket whose hit rate was unusually high or low against the same arm's other tickets (and says when there are too few tickets to tell), and adds one line on slates once some have settled. Built from counts with templates and no model call, and never about stakes or profit. See [Reading `/eli5`](#reading-eli5). |
| `/help` | Shows the list, the limits and what the bot cannot do. |

**Limits:** at most 20 fixtures per ticket (a longer one is refused, never cut, with a pointer to `/slate`); a slate holds up to 100 rows; at most 10 vision reads per UTC day (`INTAKE_DAILY_CALL_CAP`); at most 2,000,000 reading tokens per UTC day (`INTAKE_DAILY_TOKEN_BUDGET`); files up to 10 MB; an upload waiting for your answer is kept 60 minutes; rows that cannot be matched are reported and not used; at most 5 rules active at once.

**What it cannot do:**

- Change the model or arm A.
- Put odds into arm A or arm C (odds are used only by arm B and the market comparison).
- Give stake, bankroll or profit advice.
- Apply a rule change without a YES from you (starting or stopping a rule).
- Act for any other chat.

## Rules for arm B

A rule message is turned into a fixed structure by a small deterministic grammar, not by a model, so free text cannot add behaviour. Directives are separated by `;`:

- `N draws` forces the N fixtures with the highest draw score to a draw (N is 1 to 10).
- `N upsets by margin` or `N upsets by odds` reverses N picks: home becomes away and away becomes home; draw picks are never reversed. `by margin` takes the picks with the smallest model margin; `by odds` takes the largest gap between the model's probability and the de-vigged market probability, and only rows with odds are eligible.
- `only <league>, <league>` or `skip <league>, <league>` limits arm B to (or excludes) leagues by the name shown in your fixtures, up to 10.

Example: `/rule 5 draws; 4 upsets by margin; only Premier League, La Liga`.

Each proposal and decision is an append-only event with an audit row.

### Several rules at once

Up to five rules are active at the same time. Each one is its own arm B pick set (`arm = B`, `arm_version = rule-v<N>`, shown as B<N>: rule v3 is arm B3), computed on exactly the same fixtures as arm A and arm C, so they can be compared like for like. Activating a rule never retires another. A rule that repeats an active one (same effect, leagues in any order or case) is refused, because it would only add a duplicate arm. When five are active, a sixth proposal is refused until you stop one with `/retire <version>` and confirm with YES. A retired rule keeps all its earlier picks, which stay in the scoreboard; starting the same rule again gives it a new version, and its sample starts again from zero.

A ticket uses the rules that are active when it is frozen. Both ticket kinds carry them:

- A **screenshot ticket** gets every active rule.
- The **daily simulated ticket** also gets every active rule except one that uses odds (`N upsets by odds`): a simulated ticket has no odds, so that rule only ever appears on screenshot tickets that show odds. The simulated ticket is written once a day and never amended, so a rule activated later in the day first appears on the next day's ticket. If the rules cannot be read when the daily ticket is due, it waits for the next run rather than being written without the rule arms.

Why this matters for reading results: a rule's pick equals arm A's except where a directive adjusted it, so the only place a rule can beat or lose to arm A is on its adjusted picks. `/scoreboard` therefore shows, for every arm and rule, its hit rate with a rough 95% margin, what arms A and C picked on the same fixtures, and (for a rule or arm C) how it did on the picks where it differs from A by design. A few dozen settled picks cannot separate rules that differ by a few points; the margin and the sample sizes are shown so that it is not mistaken for evidence. It is a calibration report card, not evidence of an edge.

## The arms on a ticket

| Arm | Pick |
|---|---|
| A | The rating model's most probable outcome (the earliest pre-kickoff prediction). Never uses odds. |
| B (one per active rule) | Arm A adjusted by that rule; absent when no rule is active. The only arm that can use odds. |
| C | `draw-score-v1`: the five highest draw scores forced to a draw (fewer on a short ticket), otherwise arm A. Never uses odds. |

Fixtures that already kicked off, have no rating prediction or were not matched are left off the ticket and listed in the reply. Details of the stored tables are in [TICKETS.md](TICKETS.md).

## Tickets and slates

A stored read is one of two kinds, and you say which.

- **Ticket:** a slip you declared, up to 20 rows. It is matched, then arms A, B and C are frozen before kickoff, and it is scored on the scoreboard. More than 20 rows is refused with a pointer to `/slate`; it is never cut to 20.
- **Slate:** any size, up to 100 rows per read, matched and stored for testing models. No picks are frozen, no computation is started, and it stays off the arm scoreboard. Rows past 100 are listed as rejected.

**Saying which:** put the word `ticket` or `slate` in the caption of the photo or file. With no marker, or both words, the bot asks and waits. It keeps only the Telegram file id, the type, the size and the update id of that upload, only the latest one, and for 60 minutes. Nothing is downloaded, read or counted against the daily call cap or token budget while it waits. `/ticket` or `/slate` then reads that upload as the kind chosen, and uses it up. A bare `/ticket` or `/slate` with nothing waiting (or after it expired) says to send the file first.

**The reply** lists every row read, then the rejected rows with their reasons, then the matching result: how each row matched (exact, remembered, similar or one team) or why it did not. The same facts are stored per row. A reply longer than Telegram's message limit arrives as several messages, in order, never cut. A read that may take a while first says it is reading.

**Tokens and the budget:** the input and output tokens of every vision read are stored with the read and added to the day's total. Beside the daily call cap there is a daily token budget (`INTAKE_DAILY_TOKEN_BUDGET`, default 2,000,000 input plus output tokens, far above a normal day). When the day's total has reached it, a new read is refused with a plain message and resets at 00:00 UTC. A missing, empty or invalid setting gives the default, so a bad setting can never make the bot refuse everything.

**Repeat uploads:** the SHA-256 of the file's bytes is stored with the read. The same file sent again reuses the stored rows: no new vision call, nothing from the call cap, no tokens. Only the hash is kept, never the file. The reused rows are matched again and stored as the kind you asked for this time.

## Reading `/eli5`

`/eli5` is the scoreboard in plain words. It reads four things the database already holds, with no model call and no new writes: the per-arm scoreboard (`intake_scoreboard`, since the start), the window summary (`intake_window_summary`: the last 7 days, the last 28 days and the 28 days before those, on settled picks of complete tickets, with what arm A picked on the same fixtures), the per-ticket hits (`intake_ticket_hits`) and the settled slate counts (`intake_slate_score`).

- **Working or dropping, in both windows.** For each arm other than A, in the last 7 days and again in the last 28: if it has fewer than 20 settled picks in that window, the message says too early to tell. With 20 or more, the gap from arm A on the same matches (in points) is compared with the margin of error `/scoreboard` already shows (`98 / sqrt(n)` points). Ahead by more than the margin is working, behind by more than the margin is dropping, and anything inside the margin is too early to tell. Arm A is the yardstick and is never judged. The 28 days before the last 28 and the whole history are shown beside it as context and never decide the verdict.
- **The comparison.** One line per arm: the same verdict in both windows ("same story"), or "this week differs from the month" with both verdicts, naming whichever window is thin (fewer than 20 picks).
- **Ticket spikes, either way.** For each complete ticket with a settled kickoff in the last 28 days and each arm that has at least 8 settled picks on it, the ticket's hit rate is compared with the same arm's hit rate over the other tickets of the same kind (simulated or screenshot) and, for a rule, the same rule version, in the last 28 days. The ticket is left out of its own baseline. A baseline needs at least 3 other tickets and at least 40 settled picks between them, and some spread (not every pick right, and not none). A ticket more than 2 standard errors (`sqrt(p(1-p)/n)`) above its arm's usual rate is "unusually high", more than 2 below is "unusually low"; exactly 2 is not a spike. At most 5 are shown, newest first, then "and N more". When no ticket has a baseline yet the message says there are not enough tickets to call a spike, and when some were checked and none stood out it says so. About 1 ticket in 20 will look like a spike by chance alone and the message says that; it never calls a ticket wrong or says what to do about it. Slates have no tickets and are not checked.
- **Slates.** One line, only once slates have settled: how often the rating model's pick was right on the settled slate matches (each counted once however many uploads hold it) and, where all three prices were printed, the market's favourite beside the model. Counts only.
- **Failures.** If the slate view or the ticket view cannot be read, the rest of the message still arrives without that section. If the scoreboard or the window summary cannot be read, you get a plain error.
- **Never.** No stakes, bankroll, profit or advice on what to back; the message ends by saying it is a calibration report card and not betting advice.

## Data policy

- The image or PDF is read in memory and **never stored** by this project. It is sent to the Anthropic API for the read; Anthropic's own retention terms apply to that request, so check them before use.
- Only **parsed values** are stored, in your own Supabase project: team names, kickoff time, the three decimal odds, the bookmaker label as printed, the observed time, which stored fixture a row matched and how, the reasons rows were rejected, and the input and output token counts of each read.
- Two small things are kept so the intake can ask before reading and avoid reading twice. For an upload it asked about, the **Telegram file id** (a reference into Telegram's own storage, not the file), its type and size, for 60 minutes and only the latest one; and a **SHA-256 fingerprint** of each file it has read, so an identical repeat reuses the earlier read. Neither is the file, neither can rebuild it, and neither is published.
- A screenshot is third-party content and prices belong to their source. They stay in your private database, are used only by arm B and the model-versus-market comparison, and are never committed, shared or published ([DATA_POLICY.md](DATA_POLICY.md)). Do not use the intake with a site whose terms forbid you from copying its prices.
- The audit log holds short fixed details and never message or image content. A stranger's message produces a row with no content. When a call to the reading service or to Telegram fails, the row also carries the HTTP status and the service's own short error text (cut to 100 characters, control characters removed), for example `vision REQUEST_FAILED HTTP 401 authentication_error invalid x-api-key`; it never carries a key, a token, the file or the request. A menu sync that fails is recorded the same way (`menu_sync ...`).

## Bring your own keys

You need your own Telegram bot, Anthropic key and Supabase project. Nothing here is shared with the maintainer.

1. **Apply the migrations** (`supabase/migrations/`, in order, including `20261126000000_proracle_p0_capture.sql`, `20261127000000_proracle_intake.sql` and `20261128000000_proracle_multi_rule.sql`, `20261129000000_proracle_ticket_slate.sql` and `20261130000000_proracle_slate_score.sql` and `20261201000000_proracle_complete_tickets.sql` and `20261202000000_proracle_window_ticket_views.sql`) to your project. Review them first; they only add `proracle` tables, views and one function. Add `proracle` to the exposed schemas.
2. **Telegram.** Create a bot with `@BotFather`. Send it one message from your account and read your chat id with `getUpdates` **before** you register a webhook ([OPERATIONS.md](OPERATIONS.md)). Choose a long random webhook secret.
3. **Anthropic.** Create a key in its own workspace with a spend limit (limits apply per workspace, not per key). Use a dedicated key for this function only.
4. **GitHub token.** Create a fine-grained token for **this one repository** with the single permission *Actions: read and write* and a short expiry. It can start the `Ticket intake` workflow and nothing else. In a fork, also set `GITHUB_REPOSITORY`.
5. **Workflow secrets.** The `Ticket intake` workflow uses the same `production` environment secrets as the runtime workflow (the restricted `proracle_runtime` token, the project URL and publishable key, and the Telegram bot token and chat id).
6. **Function secrets**, set in your project's secret store (never in the repository, a chat or a public environment):

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the bot token |
| `TELEGRAM_WEBHOOK_SECRET` | the webhook secret from step 2 |
| `ALLOWED_CHAT_ID` | your numeric chat id |
| `ANTHROPIC_API_KEY` | the key from step 3 |
| `GITHUB_DISPATCH_TOKEN` | the token from step 4 |
| `VISION_MODEL` | optional; default `claude-sonnet-5-5` |
| `INTAKE_DAILY_CALL_CAP` | optional; 1 to 100, default 10 |
| `INTAKE_DAILY_TOKEN_BUDGET` | optional; reading tokens (input plus output) per UTC day, 10,000 to 1,000,000,000, default 2,000,000; a missing or invalid value gives the default |
| `INTAKE_UTC_OFFSET_MINUTES` | optional; the zone your screenshots print times in, default 180 (East Africa Time) |
| `INTAKE_ANCHOR_MATCHING` | optional; `off`, `0`, `false` or `no` switches the one-team match off; anything else, or unset, leaves it on |
| `GITHUB_REPOSITORY` | optional; default `14leux/PRORACLE-research` |

   Supabase supplies `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` to the function itself. The function uses that key only for the intake and ticket tables.
7. **Deploy and register** (once, on your own machine, with secrets taken from your environment rather than typed into a command history): deploy with `supabase functions deploy telegram-intake`, then register the webhook with Telegram's `setWebhook`, passing your function URL, your `secret_token` and `allowed_updates=["message"]`. Send `/help` to check. The commands also appear in Telegram's menu (the button beside the message box). The function keeps that menu equal to the command list itself: after it answers your first message following a deploy, it compares the menu Telegram holds with the list and sets it only if they differ (no extra permissions, no extra secret). To set it by hand instead, put `TELEGRAM_BOT_TOKEN` in your shell environment and run `deno run --allow-net=api.telegram.org --allow-env=TELEGRAM_BOT_TOKEN supabase/functions/telegram-intake/set_menu.ts`; `deno run supabase/functions/telegram-intake/set_menu.ts --print` shows the menu.

The function refuses every call without the secret header, so it is safe to deploy before registering the webhook. If a key leaks, rotate it in its own console and update the secret; deleting the webhook (`deleteWebhook`) switches the intake off.

## Checking it

With Deno installed, from the repository root (no network, no files, no secrets; CI runs the same):

```powershell
deno check --no-remote supabase/functions/telegram-intake/*.ts
deno test --no-prompt --no-remote supabase/functions/telegram-intake/
```

The Python side is covered by `tests/test_ticket_mode.py`. To prove the migration on a disposable local database, follow [DATABASE.md](DATABASE.md) and run `supabase/tests/verify_intake.sql` and `supabase/tests/verify_multi_rule.sql` with `psql` inside the local database container; `supabase/tests/intake_catalog_fingerprint.sql` run before and after re-applying the migration must give identical output.

The code for tickets and slates (caption marker, the question, the pending upload, the 20-row refusal, token budget, hash reuse, split replies) is covered by `ticket_slate_test.ts` beside the other function tests. The tickets-and-slates migration (`20261129000000_proracle_ticket_slate.sql`) is proved the same way with `supabase/tests/verify_ticket_slate.sql` and `supabase/tests/ticket_slate_catalog_fingerprint.sql`. It only adds: nothing the deployed function or the workflows read is removed, and it is safe to apply before the code that uses it. Its rollback, `supabase/tests/rollback_ticket_slate.sql`, removes only what it added, refuses to run if that would delete stored data (unless `-v allow_data_loss=1`), and must be tried on a database copy first. Apply the migration to production first, then merge, then redeploy.

The complete-tickets migration (`20261201000000_proracle_complete_tickets.sql`) replaces the scoreboard and recent-window views so a ticket whose arm picks stopped short no longer counts; it is proved with `supabase/tests/verify_complete_tickets.sql`, its rollback `supabase/tests/rollback_complete_tickets.sql` restores the two earlier definitions, and the same order applies (migration first, then merge). It changes no column or grant.

The window and ticket views migration (`20261202000000_proracle_window_ticket_views.sql`) adds two read-only views for `/eli5` (`intake_window_summary`, `intake_ticket_hits`; security invoker, select for the intake function's role only). It is proved with `supabase/tests/verify_window_ticket_views.sql` and `supabase/tests/window_ticket_views_catalog_fingerprint.sql`, and its rollback `supabase/tests/rollback_window_ticket_views.sql` drops only those two views. It leaves `intake_recent_window` and the scoreboard as they are. Same order: migration first, then merge, then redeploy.

`/eli5` is covered by `eli5_test.ts`. Its read-only view comes from the slate-score migration (`20261130000000_proracle_slate_score.sql`), proved with `supabase/tests/verify_slate_score.sql` and `supabase/tests/slate_score_catalog_fingerprint.sql`; it adds one view and nothing else, and its rollback, `supabase/tests/rollback_slate_score.sql`, drops only that view. As with the previous migration, apply it to production first, then merge, then redeploy.
