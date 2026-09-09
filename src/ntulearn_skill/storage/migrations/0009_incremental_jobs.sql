CREATE TABLE resource_fetch_receipt (
    fetch_receipt_key INTEGER PRIMARY KEY,
    resource_key INTEGER NOT NULL REFERENCES resource(resource_key),
    observation_key INTEGER NOT NULL UNIQUE
        REFERENCES resource_observation(observation_key),
    version_key INTEGER REFERENCES resource_version(version_key),
    metadata_fingerprint TEXT NOT NULL CHECK (
        length(metadata_fingerprint) = 64
        AND metadata_fingerprint = lower(metadata_fingerprint)
        AND metadata_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    fetch_decision TEXT NOT NULL CHECK (fetch_decision IN (
        'FETCHED', 'REUSED_VERIFIED', 'NOT_NEEDED', 'FAILED'
    )),
    policy_reason TEXT NOT NULL CHECK (policy_reason IN (
        'new_resource', 'metadata_changed', 'verification_expired',
        'verification_requested', 'metadata_unknown', 'within_verification_interval',
        'source_failure', 'storage_failure'
    )),
    binary_changed INTEGER CHECK (binary_changed IS NULL OR binary_changed IN (0, 1)),
    verified_at TEXT,
    observed_at TEXT NOT NULL,
    error_category TEXT CHECK (
        error_category IS NULL
        OR (length(trim(error_category)) > 0 AND length(error_category) <= 120)
    ),
    CHECK (
        (fetch_decision IN ('FETCHED', 'REUSED_VERIFIED')
         AND version_key IS NOT NULL AND verified_at IS NOT NULL
         AND error_category IS NULL)
        OR
        (fetch_decision = 'NOT_NEEDED'
         AND version_key IS NOT NULL AND verified_at IS NOT NULL
         AND binary_changed IS NULL AND error_category IS NULL)
        OR
        (fetch_decision = 'FAILED'
         AND version_key IS NULL AND verified_at IS NULL
         AND binary_changed IS NULL AND error_category IS NOT NULL)
    )
) STRICT;

CREATE INDEX resource_fetch_receipt_resource_time
ON resource_fetch_receipt(resource_key, observed_at DESC, fetch_receipt_key DESC);

CREATE TABLE local_job (
    job_key INTEGER PRIMARY KEY,
    job_kind TEXT NOT NULL CHECK (job_kind IN ('parse', 'index', 'extract', 'reconcile')),
    input_identity TEXT NOT NULL CHECK (
        length(input_identity) = 64
        AND input_identity = lower(input_identity)
        AND input_identity NOT GLOB '*[^0-9a-f]*'
    ),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    depends_on_job_key INTEGER REFERENCES local_job(job_key),
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    available_at TEXT NOT NULL,
    lease_expires_at TEXT,
    worker_token TEXT,
    last_error_category TEXT CHECK (
        last_error_category IS NULL
        OR (length(trim(last_error_category)) > 0 AND length(last_error_category) <= 120)
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(job_kind, input_identity),
    CHECK (
        (status = 'RUNNING' AND lease_expires_at IS NOT NULL AND worker_token IS NOT NULL)
        OR
        (status <> 'RUNNING' AND lease_expires_at IS NULL AND worker_token IS NULL)
    ),
    CHECK (
        (status = 'SUCCEEDED') = (completed_at IS NOT NULL)
        AND (status = 'SUCCEEDED') = (result_json IS NOT NULL)
    )
) STRICT;

CREATE INDEX local_job_claimable
ON local_job(status, available_at, job_key);

CREATE INDEX local_job_dependency
ON local_job(depends_on_job_key, status);
