ALTER TABLE sync_state ADD COLUMN last_attempt_at TEXT;

ALTER TABLE sync_state ADD COLUMN last_attempt_outcome TEXT
CHECK (last_attempt_outcome IN ('SUCCEEDED', 'FAILED'));

ALTER TABLE sync_state ADD COLUMN last_success_at TEXT;

ALTER TABLE sync_state ADD COLUMN last_complete_at TEXT;

ALTER TABLE sync_state ADD COLUMN warning_codes_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE sync_state ADD COLUMN configured_max_age_seconds INTEGER
CHECK (configured_max_age_seconds > 0);

UPDATE sync_state
SET last_attempt_at = (
        SELECT COALESCE(sync_run.ended_at, sync_run.started_at)
        FROM sync_run
        WHERE sync_run.sync_run_key = sync_state.latest_attempt_run_key
    ),
    last_attempt_outcome = CASE
        WHEN coverage = 'FAILED' THEN 'FAILED'
        ELSE 'SUCCEEDED'
    END,
    last_success_at = CASE
        WHEN latest_success_run_key IS NULL THEN NULL
        ELSE (
            SELECT COALESCE(sync_run.ended_at, sync_run.started_at)
            FROM sync_run
            WHERE sync_run.sync_run_key = sync_state.latest_success_run_key
        )
    END,
    last_complete_at = CASE
        WHEN latest_complete_run_key IS NULL THEN NULL
        ELSE (
            SELECT COALESCE(sync_run.ended_at, sync_run.started_at)
            FROM sync_run
            WHERE sync_run.sync_run_key = sync_state.latest_complete_run_key
        )
    END;
