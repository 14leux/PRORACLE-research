# Running PRORACLE yourself

This guide covers one operator running PRORACLE on their own machine and accounts. Nothing here uses the maintainer's credentials, database, or bot. The offline path and disposable local Supabase checks have been verified; no hosted deployment or live provider run has been verified. Provider use remains subject to your own agreement and local law.

## 1. What you need

- Python 3.11 or newer. The package uses only the standard library; nothing to install.
- A BSD API token of your own (the fixture and final-result source).
- A Supabase project of your own. Use a disposable or test project first. See [database access](DATABASE.md) for the blank local stack and how to verify grants and row-level security.
- Optional: a Telegram bot of your own for report messages.

## 2. Secrets

Set these in the process environment of the shell or scheduler that runs the commands. Never commit them, paste them into issues, or put them in a shared `.env` file.

| Variable | Used by | Notes |
| --- | --- | --- |
| `BSD_API_TOKEN` | `proracle.self_hosted` | Your own provider key. |
| `SUPABASE_URL` | self-hosted runner, retrospective | Your project URL. |
| `SUPABASE_SERVICE_KEY` | self-hosted runner, retrospective | Server-side secret. Bypasses row-level security; never in browser code. |
| `PRORACLE_DB_SCHEMA` | self-hosted runner, retrospective | Defaults to `proracle`; use only a schema created by the PRORACLE migrations and exposed in your Supabase API settings. |
| `PRORACLE_LOOKAHEAD_DAYS` | self-hosted runner | Optional. Days of upcoming fixtures to fetch; integer 1 to 14, default 2. |
| `PRORACLE_HISTORY_DAYS` | self-hosted runner | Optional. Days of finished results to fetch as training history; integer 1 to 365, default 30. |
| `TELEGRAM_BOT_TOKEN` | `proracle.telegram_report` | From BotFather. |
| `TELEGRAM_CHAT_ID` | `proracle.telegram_report` | Your own chat or channel id. |

The code reads credentials only from the environment and prints only fixed error codes, never a key, URL, or provider response.

## 3. Telegram setup (optional)

The report examples below write private files under `reports/`. Create that directory before running the commands that redirect output into it:

```sh
python -c "from pathlib import Path; Path('reports').mkdir(parents=True, exist_ok=True)"
```

1. In Telegram, message `@BotFather`, send `/newbot`, and follow the prompts. Keep the token it gives you private.
2. Send any message to your new bot from the account that should receive reports.
3. Find your chat id by opening `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser on your own machine and reading `message.chat.id`. Do not share that URL; it contains your token.
4. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in your environment.

Preview first. This prints the message and sends nothing:

```sh
python -m proracle.retrospective --from 2026-11-01 --until 2026-12-01 > reports/report.json
python -m proracle.telegram_report --report reports/report.json
```

Then send it:

```sh
python -m proracle.telegram_report --report reports/report.json --send
```

The sender is separate from prediction and result ingestion. If it fails (wrong token, rejected chat, rate limit, network), nothing else changes and the exit code is 3 with a fixed reason such as `UNAUTHORIZED`, `CHAT_OR_REQUEST_REJECTED`, `RATE_LIMITED`, or `NETWORK_ERROR`. It does not retry; run it again if the reason is `RATE_LIMITED` or `NETWORK_ERROR`.

## 4. Scheduling

Run each step as its own scheduled command so one failure does not hide another. A sensible local order is: self-hosted run, wait for results to arrive, retrospective report, Telegram message.

- Linux or macOS: one `cron` line per command.
- Windows: Task Scheduler, one task per command.
- Set the environment variables in the scheduled task's own environment, not in a file in the repository.
- Do not schedule two copies of the same step at overlapping times; the runner can be re-run safely, but each extra run adds prediction rows for still-upcoming fixtures and duplicate messages are annoying.

## 5. Costs and limits

- The baseline path calls no language model, so there is no model cost. You pay only what your BSD plan and Supabase plan charge. Check both providers' current limits and terms yourself; this repository does not vouch for them.
- Telegram bot messages are free for ordinary personal use, subject to Telegram's own rate limits.
- Reports measure prediction quality only. They make no statement about betting return.

## 6. Safe failure behavior

| Situation | What happens |
| --- | --- |
| Missing or malformed credentials | A missing or malformed `BSD_API_TOKEN` stops the command before any request. With `--execute`, the Supabase variables are checked after the provider fetch and before any database write, so a missing Supabase variable still uses provider quota. The message names the problem category. |
| Provider or Telegram outage | Fixed error code; no partial prediction is recorded as valid. |
| Report sender fails | Predictions, results, and reports are unaffected. |
| Rerun after failure | No prediction or result is overwritten (a fixture's kickoff time and status can be updated). A repeated result is not written twice. A rerun while a fixture is still upcoming appends another prediction row for it; the retrospective report scores only the earliest per fixture and lists the rest as `ADDITIONAL_PREDICTION_NOT_SCORED`. |

## 7. Teardown

- Local stack: `supabase stop` keeps the local data; `supabase db reset --local` deletes it.
- Hosted test project: delete the project in your Supabase dashboard.
- Telegram: send `/revoke` to `@BotFather` to invalidate the token, or delete the bot.
- Remove the scheduled tasks and unset the environment variables.
- Delete `reports/` if it holds real rows; it is ignored by Git but still sits on your disk.

If you ever paste a real token anywhere public, revoke it at once (BSD dashboard, Supabase API settings, BotFather `/revoke`) and issue a new one.
