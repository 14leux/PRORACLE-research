# Telegram intake (optional)

Send your own bot a screenshot or PDF of fixtures from a betting site. The intake reads it, matches the fixtures to the ones PRORACLE already stores, then freezes what each arm would have picked **before kickoff** so the arms can be scored later. It is a measurement tool for calibration and for testing rules; it is not betting advice and makes no claim about returns.

It is optional, built as a Supabase Edge Function plus one workflow, and **it does nothing until you deploy it and register a webhook**. Nothing in this repository deploys it.

New to it? [USER_MANUAL.html](USER_MANUAL.html) is a picture-first walk-through (open it in a browser; it works offline) with Telegram examples, a rule builder and copy-paste queries to see the data. This page is the reference.

## How it works

1. Telegram calls your Edge Function `telegram-intake` (a webhook).
2. The function checks the secret-token header in constant time, then that the message comes from your private chat only. Anything else is ignored and logged without its content.
3. For a photo, an image file or a PDF it takes one of today's vision calls (a daily cap), reads the file in memory with one call to the Anthropic API (the model is told to call a single tool; any other reply is discarded), and validates the result in code: at most 20 fixtures, strict field checks, odds only when all three prices are valid.
4. It matches each row to a stored scheduled fixture whose kickoff is within three hours, home with home and away with away, at the best level any candidate reaches: **exact** (names equal after normalising case, accents and club affixes such as FC), then **remembered** (a name that an earlier ticket of yours already matched to that stored name), then **similar** (every word of the shorter name is in the longer one, for example `BRIGHTON` and `Brighton & Hove Albion`; not when the extra words mark another side, such as U21, Women, II or B, and not for names that are only generic words such as United or City). If exactly one fixture fits at that level it is used; two or more is reported as ambiguous. Rows matched by similarity are listed in the reply so you can check them, and every match is remembered for the next ticket (read back from the stored tickets, so nothing extra is kept). Anything else is reported as unmatched and is not used.
5. It stores the parsed rows (teams, kickoff, odds, bookmaker label) and asks GitHub to run the `Ticket intake` workflow with a narrow token.
6. `python -m proracle.ticket_mode` freezes the arms in the existing ticket tables and replies in your chat.

## What you can do

Everything below comes from one list in `supabase/functions/telegram-intake/commands.ts`; `/help` and Telegram's command menu are generated from it, and a test fails if a command or limit is missing from the help or if the menu and the slash commands differ.

**Changing a command:** edit its entry in that list only (`usage`, `summary` and, for a slash command, `menu`), update the table below, then deploy. Tests fail if the routing table, `/help`, the menu or this table disagree with the list, and the deployed function brings Telegram's menu in step with the next message you send it.

| Command | What it does |
|---|---|
| `send a photo, an image file (PNG, JPEG, WebP) or a PDF of fixtures` | Reads up to 20 fixtures, matches them, freezes arm A, arm C and one arm B per active rule before kickoff and replies. |
| `/rule <directives separated by ;>` | Proposes a rule for arm B. It is echoed back and used only after YES. Up to 5 rules run at once, each as its own arm (rule v3 is arm B3). |
| `/retire <version>` | Asks to stop one active rule. It stops only after YES (it waits 60 minutes); its earlier picks stay and are still scored. |
| `YES` | Confirms what was last asked about: activates the proposed rule, or retires the rule asked about (it waits 60 minutes). |
| `NO` | Cancels the proposed rule, or the retirement asked about. |
| `/rules` | Shows every active rule and any proposal or retirement waiting. |
| `/ticket` | Shows today's tickets and each arm's picks. |
| `/scoreboard` | Hit rate of each arm and each rule on settled ticket picks, with what arms A and C picked on the same fixtures and a rough margin of error, labelled a calibration report card. |
| `/help` | Shows the list, the limits and what the bot cannot do. |

**Limits:** at most 20 fixtures per ticket; at most 10 vision reads per UTC day (`INTAKE_DAILY_CALL_CAP`); files up to 10 MB; rows that cannot be matched are reported and not used; at most 5 rules active at once.

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

## Data policy

- The image or PDF is read in memory and **never stored** by this project. It is sent to the Anthropic API for the read; Anthropic's own retention terms apply to that request, so check them before use.
- Only **parsed values** are stored, in your own Supabase project: team names, kickoff time, the three decimal odds, the bookmaker label as printed, the observed time, and which stored fixture a row matched.
- A screenshot is third-party content and prices belong to their source. They stay in your private database, are used only by arm B and the model-versus-market comparison, and are never committed, shared or published ([DATA_POLICY.md](DATA_POLICY.md)). Do not use the intake with a site whose terms forbid you from copying its prices.
- The audit log holds short fixed details and never message or image content. A stranger's message produces a row with no content. When a call to the reading service or to Telegram fails, the row also carries the HTTP status and the service's own short error text (cut to 100 characters, control characters removed), for example `vision REQUEST_FAILED HTTP 401 authentication_error invalid x-api-key`; it never carries a key, a token, the file or the request. A menu sync that fails is recorded the same way (`menu_sync ...`).

## Bring your own keys

You need your own Telegram bot, Anthropic key and Supabase project. Nothing here is shared with the maintainer.

1. **Apply the migrations** (`supabase/migrations/`, in order, including `20261126000000_proracle_p0_capture.sql`, `20261127000000_proracle_intake.sql` and `20261128000000_proracle_multi_rule.sql`) to your project. Review them first; they only add `proracle` tables, views and one function. Add `proracle` to the exposed schemas.
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
| `INTAKE_UTC_OFFSET_MINUTES` | optional; the zone your screenshots print times in, default 180 (East Africa Time) |
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
