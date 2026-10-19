# Restricted runtime role

The scheduled runtime does not need an all-access key. The migration
`supabase/migrations/20261019000000_proracle_runtime_role.sql` creates `proracle_runtime`, a database
role that can read and insert PRORACLE records and update only a fixture's kickoff and status. It has
no access to any other schema, cannot update or delete predictions or results, and does not bypass
row-level security.

## What it can and cannot do

| Can | Cannot |
|---|---|
| Read and insert rows in the four `proracle` tables | Read any table outside `proracle` |
| Update `fixtures.kickoff_at` and `fixtures.status` | Update or delete predictions, results or failure events |
| | Create objects, truncate, or bypass row-level security |

Two read-only statistics views in the `extensions` schema are readable by every role in a Supabase
project by default; they are not specific to this role.

## Using it

1. Apply the migration to your project and make sure `proracle` is in the exposed schemas.
2. Create a JWT with the claims `{"iss": "supabase", "ref": "<project ref>", "role": "proracle_runtime"}`,
   signed HS256 with your project's legacy JWT secret, with an expiry you choose. Do this on your own
   machine; never paste the signing secret anywhere else. A token only selects the role; it grants
   nothing beyond the role's privileges.
3. Run the runtime with `SUPABASE_SERVICE_KEY` set to that token and `SUPABASE_API_KEY` set to your
   project's publishable key. The gateway rejects a custom token in the `apikey` header, so the public
   key goes there and the token goes in `Authorization`.
4. Revoke at any time, without rotating the project's signing secret:
   `revoke proracle_runtime from authenticator;`

## What a leaked token can do

It can read the PRORACLE tables and append new rows to them until it expires or you revoke the role.
It cannot read your other application data. Treat it as a secret anyway, store it only in the runtime's
secret store, and set a short expiry.

## Verified on 2026-10-19

On a live project: a request with the publishable key as `apikey` and the token as bearer read
`proracle.fixtures` and `proracle.predictions`; the same token received `permission denied` (HTTP 403,
code 42501) when reading `public.fixtures`; and a token sent in the `apikey` header was rejected
(HTTP 401, "Invalid API key").
