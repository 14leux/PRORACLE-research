-- B4: idempotent local source keys and safe append-only failure events.
-- No provider response, request detail, credential, or free-form message is stored.

create unique index fixtures_source_key_unique_idx
    on proracle.fixtures (source_name, source_event_key)
    where source_name is not null and source_event_key is not null;

create table proracle.prediction_failures (
    event_id uuid primary key default gen_random_uuid(),
    fixture_id uuid not null references proracle.fixtures(id) on delete restrict,
    contract_version text not null check (contract_version = '1.0'),
    event_type text not null default 'prediction_failed'
        check (event_type = 'prediction_failed'),
    occurred_at timestamptz not null,
    kickoff_at timestamptz not null,
    stage text not null check (stage in (
        'INPUT_VALIDATION', 'SOURCE_REQUEST', 'MODEL_REQUEST',
        'RESPONSE_VALIDATION', 'PERSISTENCE'
    )),
    error_code text not null check (error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
    retryable boolean not null,
    created_at timestamptz not null default now(),
    check (occurred_at < kickoff_at),
    check (
        (stage = 'INPUT_VALIDATION'
            and error_code in ('INVALID_INPUT', 'FEATURES_MISSING', 'FEATURES_STALE'))
        or (stage = 'SOURCE_REQUEST'
            and error_code in (
                'SOURCE_TIMEOUT', 'SOURCE_RATE_LIMITED', 'SOURCE_QUOTA_EXHAUSTED',
                'SOURCE_UNAVAILABLE', 'SOURCE_UNAUTHORIZED', 'SOURCE_INVALID_RESPONSE'
            ))
        or (stage = 'MODEL_REQUEST'
            and error_code in (
                'MODEL_TIMEOUT', 'MODEL_RATE_LIMIT', 'MODEL_UNAVAILABLE', 'COST_LIMIT_REACHED'
            ))
        or (stage = 'RESPONSE_VALIDATION' and error_code = 'MODEL_INVALID_OUTPUT')
        or (stage = 'PERSISTENCE' and error_code = 'PERSISTENCE_FAILED')
    )
);

comment on table proracle.prediction_failures is
    'Append-only allowlisted failure metadata. Never store raw provider/model text or payloads.';
comment on column proracle.prediction_failures.error_code is
    'Stable stage-specific code only; raw exception and response text are intentionally excluded.';

create index prediction_failures_fixture_occurred_at_idx
    on proracle.prediction_failures (fixture_id, occurred_at desc);

-- Grants are scoped to this table so unrelated application schemas are unchanged.
revoke all privileges on table proracle.prediction_failures
    from public, anon, authenticated, service_role;
grant select, insert on table proracle.prediction_failures to service_role;
alter table proracle.prediction_failures enable row level security;
