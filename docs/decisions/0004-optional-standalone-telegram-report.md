# Decision 0004: Optional, Standalone Telegram Report Sender

- **Status:** Accepted by the maintainer (B6 scope)
- **Date:** 2026-10-09

## Context

Operators want the retrospective summary pushed to their own phone. Notification code is the likeliest place for a credential to leak (the bot token is part of the request URL) and for a delivery problem to be mistaken for a prediction problem.

## Decision

- **Decoupled.** `proracle.telegram_report` reads a finished report file and sends one message. It imports nothing from prediction, result, or database code, and nothing imports it.
- **Preview by default.** Without `--send` it prints the message and sends nothing.
- **Operator's own bot.** Credentials are `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from the environment. Missing or malformed values stop the command before any network call.
- **No secret reaches output.** Errors are fixed codes only. Network exceptions are discarded because their text can embed the token-bearing URL; response bodies are never printed.
- **Quality, not betting.** The message states sample size and 95% intervals, flags fewer than 30 fixtures as descriptive only, and always ends with the no-profitability-claim line.
- **No retries and no scheduling inside the module.** Scheduling is the operator's job (see `docs/OPERATIONS.md`).

## Trade-off

A single message per run with a short summary loses detail. The full JSON report stays in `reports/`, which is not published.
