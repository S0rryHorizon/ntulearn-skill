CREATE TABLE source_provider (
    provider_key INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    capability_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE source_object (
    source_object_key INTEGER PRIMARY KEY,
    provider_key INTEGER NOT NULL REFERENCES source_provider(provider_key),
    object_kind TEXT NOT NULL CHECK (object_kind IN (
        'course', 'content', 'attachment', 'announcement', 'assessment',
        'grading_column', 'calendar_item'
    )),
    remote_key TEXT NOT NULL CHECK (length(remote_key) > 0),
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    UNIQUE (provider_key, object_kind, remote_key)
) STRICT;

CREATE TABLE course (
    course_key INTEGER PRIMARY KEY,
    source_object_key INTEGER NOT NULL UNIQUE REFERENCES source_object(source_object_key),
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    term TEXT,
    availability TEXT NOT NULL CHECK (availability IN (
        'ACTIVE', 'MISSING', 'UNAVAILABLE', 'REMOVED_CONFIRMED', 'UNKNOWN'
    )),
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL
) STRICT;

CREATE TABLE content_node (
    content_key INTEGER PRIMARY KEY,
    source_object_key INTEGER NOT NULL UNIQUE REFERENCES source_object(source_object_key),
    course_key INTEGER NOT NULL REFERENCES course(course_key),
    parent_content_key INTEGER REFERENCES content_node(content_key),
    handler_kind TEXT NOT NULL,
    title TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    availability TEXT NOT NULL CHECK (availability IN (
        'ACTIVE', 'MISSING', 'UNAVAILABLE', 'REMOVED_CONFIRMED', 'UNKNOWN'
    )),
    sanitized_metadata_json TEXT NOT NULL DEFAULT '{}',
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL
) STRICT;

CREATE TABLE sync_run (
    sync_run_key INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    requested_scope_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'RUNNING', 'SUCCEEDED', 'SUCCEEDED_WITH_WARNINGS', 'FAILED', 'CANCELLED'
    )),
    counts_json TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    error_category TEXT
) STRICT;

CREATE TABLE sync_scope_result (
    scope_result_key INTEGER PRIMARY KEY,
    sync_run_key INTEGER NOT NULL REFERENCES sync_run(sync_run_key),
    provider_key INTEGER NOT NULL REFERENCES source_provider(provider_key),
    course_key INTEGER REFERENCES course(course_key),
    data_kind TEXT NOT NULL,
    window_key TEXT NOT NULL DEFAULT '',
    coverage TEXT NOT NULL CHECK (coverage IN ('COMPLETE', 'PARTIAL', 'STALE', 'UNKNOWN', 'FAILED')),
    pages_seen INTEGER NOT NULL CHECK (pages_seen >= 0),
    items_seen INTEGER NOT NULL CHECK (items_seen >= 0),
    pagination_complete INTEGER NOT NULL CHECK (pagination_complete IN (0, 1)),
    failure_category TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    observed_at TEXT NOT NULL
) STRICT;

CREATE TABLE sync_state (
    sync_state_key INTEGER PRIMARY KEY,
    provider_key INTEGER NOT NULL REFERENCES source_provider(provider_key),
    course_key INTEGER REFERENCES course(course_key),
    data_kind TEXT NOT NULL,
    window_key TEXT NOT NULL DEFAULT '',
    latest_attempt_run_key INTEGER NOT NULL REFERENCES sync_run(sync_run_key),
    latest_success_run_key INTEGER REFERENCES sync_run(sync_run_key),
    latest_complete_run_key INTEGER REFERENCES sync_run(sync_run_key),
    coverage TEXT NOT NULL CHECK (coverage IN ('COMPLETE', 'PARTIAL', 'STALE', 'UNKNOWN', 'FAILED')),
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    capability_json TEXT NOT NULL DEFAULT '{}',
    stale_after TEXT,
    updated_at TEXT NOT NULL
) STRICT;

CREATE UNIQUE INDEX sync_state_provider_scope
ON sync_state(provider_key, IFNULL(course_key, 0), data_kind, window_key);
