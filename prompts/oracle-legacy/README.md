# ORACLE legacy prompts

These are the prompt files used by ORACLE, the private predecessor of PRORACLE, published unchanged as research history. Copyright 2026 Mailu Mwau, licensed under the Apache License, Version 2.0 (see the repository `LICENSE` and `NOTICE`). Prompt text was drafted and revised with AI assistants (Anthropic Claude and OpenAI Codex) under the maintainer's direction, as described in `NOTICE`.

## What this is and is not

- A record of what ORACLE asked a language model to do, so researchers can study, reuse, and improve it. It is not a recommendation to use these prompts.
- Not run by PRORACLE. PRORACLE's current predictor is a baseline with no language model.
- Files are byte-for-byte what ORACLE used. They contain no provider data, only `{placeholder}` names for inputs. Comment lines starting with `#` in `patches/` are ORACLE's own notes; ORACLE removed them before use.
- `.retired` files were withdrawn by ORACLE after its own review. They are kept so nobody repeats the same mistake. A retired draw-signal patch, for example, pushed 22 of 22 relegation-battle predictions to DRAW; only 3 of those were draws (see the comment block in `patches/draw_signal_v2.txt`).
- Some comments cite ORACLE's private decision and improvement logs (for example `DECISION 047`, `IMP-037`). Those logs are not published.
- Outputs from these prompts were not calibrated: ORACLE's stated confidence ran above its observed accuracy. See `docs/BASELINE.md`. Treat the prompts as material to learn from, not as evidence of predictive skill.

## Files

| Path | Role |
|---|---|
| `predict_v1.txt` | First general prompt |
| `predict_v2_standard.txt` and `predict_v2_*.txt` | Prompts by fixture type: standard, derby, dead rubber, post-European, relegation six-pointer, cup rotation |
| `predict_intl_v1.txt` | International tournaments |
| `patches/` | Add-on instruction blocks: tag patches, `confidence_collapse`, `draw_signal` |

Integrity: SHA-256 of every file is in `SHA256SUMS`. Check with `sha256sum -c SHA256SUMS` from this folder.

## Rules for changes

Follow `CONTRIBUTING.md`: never overwrite a released prompt; add a new versioned file; compare any change against a fixed baseline and an untouched evaluation period. Odds are not allowed as prediction inputs.
