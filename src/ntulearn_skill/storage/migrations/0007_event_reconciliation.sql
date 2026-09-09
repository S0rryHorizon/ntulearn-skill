CREATE TABLE event (
    event_key INTEGER PRIMARY KEY,
    stable_id TEXT NOT NULL UNIQUE CHECK (
        length(stable_id) = 64 AND stable_id = lower(stable_id)
        AND stable_id NOT GLOB '*[^0-9a-f]*'
    ),
    course_key INTEGER NOT NULL REFERENCES course(course_key),
    event_type TEXT CHECK (event_type IS NULL OR event_type IN (
        'assignment_due', 'quiz', 'test', 'exam', 'presentation', 'tutorial', 'lab',
        'lecture', 'project_milestone', 'submission', 'course_change', 'cancellation',
        'venue_change', 'generic_course_event'
    )),
    title TEXT CHECK (title IS NULL OR length(title) <= 4096),
    start_time_json TEXT CHECK (start_time_json IS NULL OR json_valid(start_time_json)),
    end_time_json TEXT CHECK (end_time_json IS NULL OR json_valid(end_time_json)),
    due_time_json TEXT CHECK (due_time_json IS NULL OR json_valid(due_time_json)),
    location TEXT CHECK (location IS NULL OR length(location) <= 4096),
    status TEXT CHECK (status IS NULL OR status IN (
        'SCHEDULED', 'TENTATIVE', 'CANCELLED', 'COMPLETED', 'UNKNOWN'
    )),
    resolution_state TEXT NOT NULL CHECK (resolution_state IN (
        'RESOLVED', 'UNRESOLVED', 'CONFLICTING'
    )),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    source_count INTEGER NOT NULL CHECK (source_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE INDEX event_course_state ON event(course_key, resolution_state, event_key);

CREATE TABLE event_source (
    event_source_key INTEGER PRIMARY KEY,
    candidate_key INTEGER NOT NULL UNIQUE REFERENCES event_candidate(candidate_key),
    course_key INTEGER NOT NULL REFERENCES course(course_key),
    event_key INTEGER REFERENCES event(event_key),
    source_kind TEXT NOT NULL CHECK (source_kind IN (
        'announcement', 'assessment', 'schedule', 'due_item', 'document'
    )),
    evidence_ref_kind TEXT NOT NULL CHECK (
        evidence_ref_kind IN ('source_observation', 'source_locator')
    ),
    evidence_ref_key INTEGER NOT NULL CHECK (evidence_ref_key > 0),
    source_object_key INTEGER REFERENCES source_object(source_object_key),
    source_timestamp TEXT,
    source_timestamp_semantics TEXT CHECK (
        source_timestamp_semantics IS NULL OR source_timestamp_semantics IN (
            'modified_at', 'published_at', 'created_at'
        )
    ),
    observed_at TEXT,
    change_kind TEXT NOT NULL CHECK (change_kind IN (
        'NONE', 'MOVE', 'CANCELLATION', 'VENUE_CHANGE'
    )),
    resolution_state TEXT NOT NULL CHECK (resolution_state IN (
        'MATCHED', 'UNRESOLVED', 'RELATED_CHANGE'
    )),
    explanation TEXT NOT NULL CHECK (length(explanation) <= 4096),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (evidence_ref_kind = 'source_observation' AND observed_at IS NOT NULL)
        OR evidence_ref_kind = 'source_locator'
    )
) STRICT;

CREATE INDEX event_source_event ON event_source(event_key, event_source_key);
CREATE INDEX event_source_course_state
ON event_source(course_key, resolution_state, event_source_key);

CREATE TABLE claim (
    claim_key INTEGER PRIMARY KEY,
    event_key INTEGER REFERENCES event(event_key),
    event_source_key INTEGER REFERENCES event_source(event_source_key),
    candidate_field_key INTEGER UNIQUE REFERENCES event_candidate_field(candidate_field_key),
    origin TEXT NOT NULL CHECK (origin IN ('SOURCE', 'LOCAL_DECISION')),
    field_name TEXT NOT NULL CHECK (field_name IN (
        'title', 'event_type', 'start_time', 'end_time', 'due_time', 'open_at', 'close_at',
        'available_from', 'available_until', 'published_at', 'location', 'status'
    )),
    value_json TEXT NOT NULL CHECK (json_valid(value_json)),
    original_text TEXT NOT NULL CHECK (length(original_text) <= 4096),
    temporal_precision TEXT CHECK (
        temporal_precision IS NULL OR temporal_precision IN (
            'EXACT_TIME', 'DATE_ONLY', 'WEEK_ONLY', 'UNKNOWN'
        )
    ),
    source_timezone TEXT,
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    decision_state TEXT NOT NULL CHECK (decision_state IN (
        'ACCEPTED', 'SUPERSEDED', 'CONFLICTING', 'REJECTED', 'UNRESOLVED'
    )),
    decision_reason TEXT NOT NULL CHECK (length(decision_reason) <= 4096),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (origin = 'SOURCE' AND event_source_key IS NOT NULL
         AND candidate_field_key IS NOT NULL)
        OR
        (origin = 'LOCAL_DECISION' AND event_source_key IS NULL
         AND candidate_field_key IS NULL AND event_key IS NOT NULL)
    )
) STRICT;

CREATE INDEX claim_event_field ON claim(event_key, field_name, decision_state, claim_key);

CREATE TABLE claim_supersession (
    successor_claim_key INTEGER NOT NULL REFERENCES claim(claim_key),
    predecessor_claim_key INTEGER NOT NULL REFERENCES claim(claim_key),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0 AND length(reason) <= 4096),
    created_at TEXT NOT NULL,
    PRIMARY KEY(successor_claim_key, predecessor_claim_key),
    CHECK (successor_claim_key <> predecessor_claim_key)
) STRICT;

CREATE TABLE event_field_projection (
    event_key INTEGER NOT NULL REFERENCES event(event_key),
    field_name TEXT NOT NULL CHECK (field_name IN (
        'title', 'event_type', 'start_time', 'end_time', 'due_time', 'location', 'status'
    )),
    selected_claim_key INTEGER NOT NULL REFERENCES claim(claim_key),
    value_json TEXT NOT NULL CHECK (json_valid(value_json)),
    uncertain INTEGER NOT NULL CHECK (uncertain IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(event_key, field_name)
) STRICT;

CREATE TABLE event_conflict (
    conflict_key INTEGER PRIMARY KEY,
    event_key INTEGER NOT NULL REFERENCES event(event_key),
    field_name TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('OPEN', 'RESOLVED')),
    reason TEXT NOT NULL CHECK (length(reason) <= 4096),
    resolution_kind TEXT CHECK (
        resolution_kind IS NULL OR resolution_kind IN ('AUTOMATIC', 'MANUAL')
    ),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK ((state = 'OPEN' AND resolved_at IS NULL AND resolution_kind IS NULL)
        OR (state = 'RESOLVED' AND resolved_at IS NOT NULL AND resolution_kind IS NOT NULL))
) STRICT;

CREATE UNIQUE INDEX event_conflict_open
ON event_conflict(event_key, field_name) WHERE state = 'OPEN';

CREATE TABLE event_conflict_alternative (
    conflict_key INTEGER NOT NULL REFERENCES event_conflict(conflict_key),
    claim_key INTEGER NOT NULL REFERENCES claim(claim_key),
    PRIMARY KEY(conflict_key, claim_key)
) STRICT;

CREATE TABLE reconciliation_run (
    reconciliation_run_key INTEGER PRIMARY KEY,
    course_key INTEGER NOT NULL REFERENCES course(course_key),
    resolver_name TEXT NOT NULL,
    resolver_version TEXT NOT NULL,
    input_hash TEXT NOT NULL CHECK (
        length(input_hash) = 64 AND input_hash = lower(input_hash)
        AND input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    status TEXT NOT NULL CHECK (status IN ('COMPLETE', 'FAILED')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(course_key, resolver_name, resolver_version, input_hash)
) STRICT;

CREATE TABLE reconciliation_decision (
    decision_key INTEGER PRIMARY KEY,
    reconciliation_run_key INTEGER NOT NULL
        REFERENCES reconciliation_run(reconciliation_run_key),
    candidate_key INTEGER NOT NULL REFERENCES event_candidate(candidate_key),
    event_key INTEGER REFERENCES event(event_key),
    result TEXT NOT NULL CHECK (result IN (
        'CREATED', 'MERGED', 'RETAINED', 'UNRESOLVED', 'RELATED_CHANGE', 'MANUAL_RETAINED'
    )),
    considered_event_keys_json TEXT NOT NULL CHECK (json_valid(considered_event_keys_json)),
    accepted_signals_json TEXT NOT NULL CHECK (json_valid(accepted_signals_json)),
    rejected_signals_json TEXT NOT NULL CHECK (json_valid(rejected_signals_json)),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    explanation TEXT NOT NULL CHECK (length(explanation) <= 4096),
    decided_at TEXT NOT NULL,
    UNIQUE(reconciliation_run_key, candidate_key)
) STRICT;

CREATE TABLE event_source_possible_match (
    event_source_key INTEGER NOT NULL REFERENCES event_source(event_source_key),
    event_key INTEGER NOT NULL REFERENCES event(event_key),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    signals_json TEXT NOT NULL CHECK (json_valid(signals_json)),
    PRIMARY KEY(event_source_key, event_key)
) STRICT;

CREATE TABLE manual_field_resolution (
    manual_resolution_key INTEGER PRIMARY KEY,
    event_key INTEGER NOT NULL REFERENCES event(event_key),
    field_name TEXT NOT NULL CHECK (field_name IN (
        'title', 'event_type', 'start_time', 'end_time', 'due_time', 'location', 'status'
    )),
    selected_claim_key INTEGER REFERENCES claim(claim_key),
    local_claim_key INTEGER REFERENCES claim(claim_key),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0 AND length(reason) <= 4096),
    decided_at TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    CHECK ((selected_claim_key IS NOT NULL) <> (local_claim_key IS NOT NULL))
) STRICT;

CREATE UNIQUE INDEX manual_field_resolution_active
ON manual_field_resolution(event_key, field_name) WHERE active = 1;

CREATE TABLE manual_identity_resolution (
    manual_identity_resolution_key INTEGER PRIMARY KEY,
    event_source_key INTEGER NOT NULL REFERENCES event_source(event_source_key),
    event_key INTEGER NOT NULL REFERENCES event(event_key),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0 AND length(reason) <= 4096),
    decided_at TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1))
) STRICT;

CREATE UNIQUE INDEX manual_identity_resolution_active
ON manual_identity_resolution(event_source_key) WHERE active = 1;

CREATE TRIGGER event_source_integrity_insert BEFORE INSERT ON event_source
WHEN NOT EXISTS (
    SELECT 1 FROM event_candidate candidate
    WHERE candidate.candidate_key = NEW.candidate_key
      AND candidate.course_key = NEW.course_key
      AND (
        (NEW.evidence_ref_kind = 'source_observation' AND EXISTS (
            SELECT 1 FROM event_candidate_field field
            WHERE field.candidate_key = candidate.candidate_key
              AND field.source_observation_key = NEW.evidence_ref_key
        ))
        OR
        (NEW.evidence_ref_kind = 'source_locator' AND EXISTS (
            SELECT 1 FROM event_candidate_field field
            WHERE field.candidate_key = candidate.candidate_key
              AND field.locator_key = NEW.evidence_ref_key
        ))
      )
      AND (NEW.event_key IS NULL OR EXISTS (
          SELECT 1 FROM event e WHERE e.event_key = NEW.event_key
            AND e.course_key = NEW.course_key
      ))
)
BEGIN SELECT RAISE(ABORT, 'event source evidence integrity failure'); END;

CREATE TRIGGER event_source_integrity_update BEFORE UPDATE OF event_key ON event_source
WHEN NEW.event_key IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM event e WHERE e.event_key = NEW.event_key AND e.course_key = NEW.course_key
)
BEGIN SELECT RAISE(ABORT, 'event source course mismatch'); END;

CREATE TRIGGER source_claim_integrity_insert BEFORE INSERT ON claim
WHEN NEW.origin = 'SOURCE' AND NOT EXISTS (
    SELECT 1 FROM event_source source
    JOIN event_candidate_field field ON field.candidate_field_key = NEW.candidate_field_key
    WHERE source.event_source_key = NEW.event_source_key
      AND field.candidate_key = source.candidate_key
      AND field.field_name = NEW.field_name
      AND field.value_json = NEW.value_json
      AND (NEW.event_key IS NULL OR NEW.event_key = source.event_key)
)
BEGIN SELECT RAISE(ABORT, 'claim evidence integrity failure'); END;

CREATE TRIGGER source_claim_event_update BEFORE UPDATE OF event_key ON claim
WHEN NEW.origin = 'SOURCE' AND NOT EXISTS (
    SELECT 1 FROM event_source source
    WHERE source.event_source_key = NEW.event_source_key
      AND NEW.event_key IS source.event_key
)
BEGIN SELECT RAISE(ABORT, 'claim event integrity failure'); END;

CREATE TRIGGER projection_integrity_insert BEFORE INSERT ON event_field_projection
WHEN NOT EXISTS (
    SELECT 1 FROM claim c WHERE c.claim_key = NEW.selected_claim_key
      AND c.event_key = NEW.event_key AND c.field_name = NEW.field_name
      AND c.decision_state = 'ACCEPTED' AND c.value_json = NEW.value_json
)
BEGIN SELECT RAISE(ABORT, 'event projection claim mismatch'); END;

CREATE TRIGGER projection_integrity_update BEFORE UPDATE ON event_field_projection
WHEN NOT EXISTS (
    SELECT 1 FROM claim c WHERE c.claim_key = NEW.selected_claim_key
      AND c.event_key = NEW.event_key AND c.field_name = NEW.field_name
      AND c.decision_state = 'ACCEPTED' AND c.value_json = NEW.value_json
)
BEGIN SELECT RAISE(ABORT, 'event projection claim mismatch'); END;

CREATE TRIGGER supersession_same_event_field_insert BEFORE INSERT ON claim_supersession
WHEN NOT EXISTS (
    SELECT 1 FROM claim successor JOIN claim predecessor
    WHERE successor.claim_key = NEW.successor_claim_key
      AND predecessor.claim_key = NEW.predecessor_claim_key
      AND successor.event_key = predecessor.event_key
      AND successor.event_key IS NOT NULL
      AND successor.field_name = predecessor.field_name
)
BEGIN SELECT RAISE(ABORT, 'claim supersession mismatch'); END;

CREATE TRIGGER manual_field_selected_integrity BEFORE INSERT ON manual_field_resolution
WHEN NEW.selected_claim_key IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM claim c WHERE c.claim_key = NEW.selected_claim_key
      AND c.event_key = NEW.event_key AND c.field_name = NEW.field_name
)
BEGIN SELECT RAISE(ABORT, 'manual resolution claim mismatch'); END;

CREATE TRIGGER manual_field_local_integrity BEFORE INSERT ON manual_field_resolution
WHEN NEW.local_claim_key IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM claim c WHERE c.claim_key = NEW.local_claim_key
      AND c.event_key = NEW.event_key AND c.field_name = NEW.field_name
      AND c.origin = 'LOCAL_DECISION'
)
BEGIN SELECT RAISE(ABORT, 'manual local claim mismatch'); END;

CREATE TRIGGER manual_identity_integrity BEFORE INSERT ON manual_identity_resolution
WHEN NOT EXISTS (
    SELECT 1 FROM event_source source JOIN event e ON e.event_key = NEW.event_key
    WHERE source.event_source_key = NEW.event_source_key
      AND source.course_key = e.course_key
)
BEGIN SELECT RAISE(ABORT, 'manual identity course mismatch'); END;

DROP TRIGGER search_document_fts_insert;
DROP TRIGGER search_document_fts_delete;
DROP TRIGGER search_document_fts_update;
DROP TABLE search_document_fts;
DROP INDEX search_document_course_kind;
DROP INDEX search_document_filters;
ALTER TABLE search_document RENAME TO search_document_m6;

CREATE TABLE search_document (
    search_document_key INTEGER PRIMARY KEY,
    entity_kind TEXT NOT NULL CHECK (entity_kind IN (
        'course', 'content', 'material', 'chunk', 'announcement', 'assessment', 'event', 'claim'
    )),
    entity_key INTEGER NOT NULL CHECK (entity_key > 0),
    course_key INTEGER NOT NULL,
    resource_key INTEGER,
    version_key INTEGER,
    chunk_key INTEGER,
    representation_key INTEGER,
    source_ref_kind TEXT NOT NULL CHECK (
        source_ref_kind IN ('source_object', 'source_observation', 'source_locator')
    ),
    source_ref_key INTEGER NOT NULL CHECK (source_ref_key > 0),
    course_code TEXT NOT NULL,
    course_title TEXT NOT NULL,
    content_title TEXT NOT NULL,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    semantic_type TEXT NOT NULL,
    classification_confidence REAL CHECK (
        classification_confidence IS NULL
        OR (classification_confidence >= 0.0 AND classification_confidence <= 1.0)
    ),
    file_format TEXT NOT NULL,
    availability TEXT NOT NULL CHECK (availability IN (
        'ACTIVE', 'MISSING', 'UNAVAILABLE', 'REMOVED_CONFIRMED', 'UNKNOWN'
    )),
    parse_coverage TEXT CHECK (parse_coverage IS NULL OR parse_coverage IN (
        'COMPLETE', 'PARTIAL', 'STALE', 'UNKNOWN', 'FAILED'
    )),
    text_origin TEXT NOT NULL CHECK (
        text_origin = 'metadata' OR text_origin = 'native' OR text_origin GLOB 'derived:*'
    ),
    body TEXT NOT NULL,
    CHECK (
        (entity_kind IN ('course', 'content', 'material')
         AND source_ref_kind = 'source_object' AND chunk_key IS NULL
         AND representation_key IS NULL AND text_origin = 'metadata')
        OR
        (entity_kind IN ('announcement', 'assessment', 'event', 'claim')
         AND source_ref_kind IN ('source_observation', 'source_locator') AND chunk_key IS NULL
         AND representation_key IS NULL AND text_origin = 'metadata')
        OR
        (entity_kind = 'chunk' AND source_ref_kind = 'source_locator'
         AND chunk_key IS NOT NULL AND version_key IS NOT NULL)
    ),
    CHECK (
        (text_origin = 'native' AND representation_key IS NULL)
        OR (text_origin GLOB 'derived:*' AND representation_key IS NOT NULL)
        OR text_origin = 'metadata'
    ),
    UNIQUE(entity_kind, entity_key, text_origin)
) STRICT;

INSERT INTO search_document SELECT * FROM search_document_m6;
DROP TABLE search_document_m6;
CREATE INDEX search_document_course_kind ON search_document(course_key, entity_kind);
CREATE INDEX search_document_filters ON search_document(semantic_type, file_format, availability);

CREATE VIRTUAL TABLE search_document_fts USING fts5(
    course_code, course_title, content_title, title, filename, semantic_type, body,
    content = 'search_document', content_rowid = 'search_document_key',
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER search_document_fts_insert AFTER INSERT ON search_document
BEGIN
    INSERT INTO search_document_fts(
        rowid, course_code, course_title, content_title, title, filename, semantic_type, body
    ) VALUES (
        NEW.search_document_key, NEW.course_code, NEW.course_title, NEW.content_title,
        NEW.title, NEW.filename, NEW.semantic_type, NEW.body
    );
END;
CREATE TRIGGER search_document_fts_delete AFTER DELETE ON search_document
BEGIN
    INSERT INTO search_document_fts(
        search_document_fts, rowid, course_code, course_title, content_title,
        title, filename, semantic_type, body
    ) VALUES (
        'delete', OLD.search_document_key, OLD.course_code, OLD.course_title,
        OLD.content_title, OLD.title, OLD.filename, OLD.semantic_type, OLD.body
    );
END;
CREATE TRIGGER search_document_fts_update AFTER UPDATE ON search_document
BEGIN
    INSERT INTO search_document_fts(
        search_document_fts, rowid, course_code, course_title, content_title,
        title, filename, semantic_type, body
    ) VALUES (
        'delete', OLD.search_document_key, OLD.course_code, OLD.course_title,
        OLD.content_title, OLD.title, OLD.filename, OLD.semantic_type, OLD.body
    );
    INSERT INTO search_document_fts(
        rowid, course_code, course_title, content_title, title, filename, semantic_type, body
    ) VALUES (
        NEW.search_document_key, NEW.course_code, NEW.course_title, NEW.content_title,
        NEW.title, NEW.filename, NEW.semantic_type, NEW.body
    );
END;

INSERT INTO search_document_fts(search_document_fts) VALUES ('rebuild');
UPDATE search_index_state SET indexed_generation = -1 WHERE singleton_key = 1;

CREATE TRIGGER search_dirty_event_insert AFTER INSERT ON event
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key) VALUES (NEW.course_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_event_update AFTER UPDATE ON event
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key) VALUES (NEW.course_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_claim_insert AFTER INSERT ON claim
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key)
    SELECT course_key FROM event WHERE event_key = NEW.event_key
    UNION
    SELECT course_key FROM event_source WHERE event_source_key = NEW.event_source_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_claim_update AFTER UPDATE ON claim
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key)
    SELECT course_key FROM event WHERE event_key IN (NEW.event_key, OLD.event_key)
    UNION
    SELECT course_key FROM event_source
    WHERE event_source_key IN (NEW.event_source_key, OLD.event_source_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;

CREATE TRIGGER search_dirty_event_projection_insert AFTER INSERT ON event_field_projection
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key)
    SELECT course_key FROM event WHERE event_key = NEW.event_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_event_projection_update AFTER UPDATE ON event_field_projection
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key)
    SELECT course_key FROM event WHERE event_key IN (NEW.event_key, OLD.event_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_event_projection_delete AFTER DELETE ON event_field_projection
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key)
    SELECT course_key FROM event WHERE event_key = OLD.event_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;

-- Asserted evidence and historical decisions are append-only. Their current
-- event assignment, reconciliation state, and active selection remain mutable.
CREATE TRIGGER event_source_evidence_immutable BEFORE UPDATE ON event_source
WHEN NEW.event_source_key IS NOT OLD.event_source_key
  OR NEW.candidate_key IS NOT OLD.candidate_key
  OR NEW.course_key IS NOT OLD.course_key
  OR NEW.source_kind IS NOT OLD.source_kind
  OR NEW.evidence_ref_kind IS NOT OLD.evidence_ref_kind
  OR NEW.evidence_ref_key IS NOT OLD.evidence_ref_key
  OR NEW.source_object_key IS NOT OLD.source_object_key
  OR NEW.source_timestamp IS NOT OLD.source_timestamp
  OR NEW.source_timestamp_semantics IS NOT OLD.source_timestamp_semantics
  OR NEW.observed_at IS NOT OLD.observed_at
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'event source evidence is immutable'); END;

CREATE TRIGGER event_source_evidence_no_delete BEFORE DELETE ON event_source
BEGIN SELECT RAISE(ABORT, 'event source evidence is immutable'); END;

CREATE TRIGGER claim_assertion_immutable BEFORE UPDATE ON claim
WHEN NEW.claim_key IS NOT OLD.claim_key
  OR NEW.event_source_key IS NOT OLD.event_source_key
  OR NEW.candidate_field_key IS NOT OLD.candidate_field_key
  OR NEW.origin IS NOT OLD.origin
  OR NEW.field_name IS NOT OLD.field_name
  OR NEW.value_json IS NOT OLD.value_json
  OR NEW.original_text IS NOT OLD.original_text
  OR NEW.temporal_precision IS NOT OLD.temporal_precision
  OR NEW.source_timezone IS NOT OLD.source_timezone
  OR NEW.confidence IS NOT OLD.confidence
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'claim assertion is immutable'); END;

CREATE TRIGGER claim_assertion_no_delete BEFORE DELETE ON claim
BEGIN SELECT RAISE(ABORT, 'claim assertion is immutable'); END;

CREATE TRIGGER claim_supersession_acyclic BEFORE INSERT ON claim_supersession
WHEN EXISTS (
    WITH RECURSIVE predecessors(claim_key) AS (
        SELECT NEW.predecessor_claim_key
        UNION
        SELECT edge.predecessor_claim_key
        FROM claim_supersession edge JOIN predecessors path
          ON edge.successor_claim_key = path.claim_key
    )
    SELECT 1 FROM predecessors WHERE claim_key = NEW.successor_claim_key
)
BEGIN SELECT RAISE(ABORT, 'claim supersession cycle'); END;

CREATE TRIGGER claim_supersession_immutable_update BEFORE UPDATE ON claim_supersession
BEGIN SELECT RAISE(ABORT, 'claim supersession audit is immutable'); END;
CREATE TRIGGER claim_supersession_immutable_delete BEFORE DELETE ON claim_supersession
BEGIN SELECT RAISE(ABORT, 'claim supersession audit is immutable'); END;

CREATE TRIGGER conflict_alternative_integrity_insert BEFORE INSERT ON event_conflict_alternative
WHEN NOT EXISTS (
    SELECT 1 FROM event_conflict conflict JOIN claim c
      ON c.event_key = conflict.event_key AND c.field_name = conflict.field_name
    WHERE conflict.conflict_key = NEW.conflict_key AND c.claim_key = NEW.claim_key
)
BEGIN SELECT RAISE(ABORT, 'conflict alternative claim mismatch'); END;

CREATE TRIGGER conflict_alternative_integrity_update BEFORE UPDATE ON event_conflict_alternative
WHEN NOT EXISTS (
    SELECT 1 FROM event_conflict conflict JOIN claim c
      ON c.event_key = conflict.event_key AND c.field_name = conflict.field_name
    WHERE conflict.conflict_key = NEW.conflict_key AND c.claim_key = NEW.claim_key
)
BEGIN SELECT RAISE(ABORT, 'conflict alternative claim mismatch'); END;

CREATE TRIGGER reconciliation_decision_immutable_update BEFORE UPDATE ON reconciliation_decision
BEGIN SELECT RAISE(ABORT, 'reconciliation decision audit is immutable'); END;
CREATE TRIGGER reconciliation_decision_immutable_delete BEFORE DELETE ON reconciliation_decision
BEGIN SELECT RAISE(ABORT, 'reconciliation decision audit is immutable'); END;

CREATE TRIGGER manual_field_resolution_content_immutable BEFORE UPDATE ON manual_field_resolution
WHEN NEW.manual_resolution_key IS NOT OLD.manual_resolution_key
  OR NEW.event_key IS NOT OLD.event_key
  OR NEW.field_name IS NOT OLD.field_name
  OR NEW.selected_claim_key IS NOT OLD.selected_claim_key
  OR NEW.local_claim_key IS NOT OLD.local_claim_key
  OR NEW.reason IS NOT OLD.reason
  OR NEW.decided_at IS NOT OLD.decided_at
BEGIN SELECT RAISE(ABORT, 'manual field decision audit is immutable'); END;
CREATE TRIGGER manual_field_resolution_no_delete BEFORE DELETE ON manual_field_resolution
BEGIN SELECT RAISE(ABORT, 'manual field decision audit is immutable'); END;

CREATE TRIGGER manual_identity_resolution_content_immutable
BEFORE UPDATE ON manual_identity_resolution
WHEN NEW.manual_identity_resolution_key IS NOT OLD.manual_identity_resolution_key
  OR NEW.event_source_key IS NOT OLD.event_source_key
  OR NEW.event_key IS NOT OLD.event_key
  OR NEW.reason IS NOT OLD.reason
  OR NEW.decided_at IS NOT OLD.decided_at
BEGIN SELECT RAISE(ABORT, 'manual identity decision audit is immutable'); END;
CREATE TRIGGER manual_identity_resolution_no_delete BEFORE DELETE ON manual_identity_resolution
BEGIN SELECT RAISE(ABORT, 'manual identity decision audit is immutable'); END;
