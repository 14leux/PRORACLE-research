# Decision 0002: Keep the Supabase CLI Config Minimal

- **Status:** Accepted by the maintainer
- **Date:** 2026-10-07

## Context

The B2 config was a 414-line scaffold from Supabase CLI 2.117.0. It included settings for services PRORACLE does not configure, referenced a seed file that is not in the repository, and set `api.auto_expose_new_tables`, which Supabase has scheduled to remove. Its project identifier and PostgreSQL major version were the project-specific settings needed to reproduce the verified local migration setup.

## Decision

Keep only `project_id = "proracle"` and `db.major_version = 17` in `supabase/config.toml`. Let the CLI supply other service defaults. Keep table access boundaries in the migrations, where explicit revokes, grants, and the SQL verifier define and test them.

Do not add a Supabase CLI license notice for the removed generated scaffold: the committed config contains only the two project settings and short project-authored comments, not copied scaffold text.

## Verification

With Supabase CLI 2.117.0, a fresh local stack started from this config and applied both migrations. The rollback-only SQL verifier passed; all four application tables had zero rows afterward. No hosted project was linked or accessed. See `docs/DATABASE.md` for the local verification instructions and access model.

## Trade-off

The small config depends on current CLI defaults for local services. If PRORACLE needs a non-default service setting, add only that setting with a reason and test it against the supported CLI version.
