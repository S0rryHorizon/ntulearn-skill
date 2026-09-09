CREATE TABLE source_observation (
    observation_key INTEGER PRIMARY KEY,
    source_object_key INTEGER NOT NULL REFERENCES source_object(source_object_key),
    sync_run_key INTEGER NOT NULL REFERENCES sync_run(sync_run_key),
    data_kind TEXT NOT NULL CHECK (data_kind IN (
        'announcement', 'assessment', 'schedule', 'due_item'
    )),
    observed_at TEXT NOT NULL,
    observation_hash TEXT NOT NULL CHECK (
        length(observation_hash) = 64
        AND observation_hash = lower(observation_hash)
        AND observation_hash NOT GLOB '*[^0-9a-f]*'
    ),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    raw_wording TEXT NOT NULL CHECK (length(raw_wording) <= 262144),
    UNIQUE(source_object_key, sync_run_key, observation_hash)
) STRICT;

CREATE TABLE announcement (
    announcement_key INTEGER PRIMARY KEY,
    source_object_key INTEGER NOT NULL UNIQUE REFERENCES source_object(source_object_key),
    course_key INTEGER NOT NULL REFERENCES course(course_key),
    current_observation_key INTEGER NOT NULL UNIQUE
        REFERENCES source_observation(observation_key),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    body TEXT NOT NULL CHECK (length(body) <= 262144),
    availability TEXT NOT NULL CHECK (availability IN (
        'ACTIVE', 'MISSING', 'UNAVAILABLE', 'REMOVED_CONFIRMED', 'UNKNOWN'
    )),
    created_time_json TEXT CHECK (created_time_json IS NULL OR json_valid(created_time_json)),
    modified_time_json TEXT CHECK (modified_time_json IS NULL OR json_valid(modified_time_json)),
    published_time_json TEXT CHECK (published_time_json IS NULL OR json_valid(published_time_json)),
    available_from_json TEXT CHECK (available_from_json IS NULL OR json_valid(available_from_json)),
    available_until_json TEXT CHECK (available_until_json IS NULL OR json_valid(available_until_json))
) STRICT;

CREATE TABLE assessment (
    assessment_key INTEGER PRIMARY KEY,
    source_object_key INTEGER NOT NULL UNIQUE REFERENCES source_object(source_object_key),
    course_key INTEGER NOT NULL REFERENCES course(course_key),
    content_key INTEGER NOT NULL REFERENCES content_node(content_key),
    grading_column_source_object_key INTEGER REFERENCES source_object(source_object_key),
    current_observation_key INTEGER NOT NULL UNIQUE
        REFERENCES source_observation(observation_key),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    subtype TEXT NOT NULL CHECK (subtype IN (
        'assignment', 'quiz', 'test', 'exam', 'presentation', 'other'
    )),
    instructions TEXT NOT NULL CHECK (length(instructions) <= 262144),
    availability TEXT NOT NULL CHECK (availability IN (
        'ACTIVE', 'MISSING', 'UNAVAILABLE', 'REMOVED_CONFIRMED', 'UNKNOWN'
    )),
    created_time_json TEXT CHECK (created_time_json IS NULL OR json_valid(created_time_json)),
    modified_time_json TEXT CHECK (modified_time_json IS NULL OR json_valid(modified_time_json)),
    available_from_json TEXT CHECK (available_from_json IS NULL OR json_valid(available_from_json)),
    available_until_json TEXT CHECK (available_until_json IS NULL OR json_valid(available_until_json)),
    open_time_json TEXT CHECK (open_time_json IS NULL OR json_valid(open_time_json)),
    close_time_json TEXT CHECK (close_time_json IS NULL OR json_valid(close_time_json)),
    due_time_json TEXT CHECK (due_time_json IS NULL OR json_valid(due_time_json)),
    grading_due_time_json TEXT CHECK (
        grading_due_time_json IS NULL OR json_valid(grading_due_time_json)
    ),
    generic_due_time_json TEXT CHECK (
        generic_due_time_json IS NULL OR json_valid(generic_due_time_json)
    )
) STRICT;

CREATE TABLE schedule_item (
    schedule_item_key INTEGER PRIMARY KEY,
    source_object_key INTEGER NOT NULL UNIQUE REFERENCES source_object(source_object_key),
    course_key INTEGER NOT NULL REFERENCES course(course_key),
    current_observation_key INTEGER NOT NULL UNIQUE REFERENCES source_observation(observation_key),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    availability TEXT NOT NULL CHECK (availability IN (
        'ACTIVE', 'MISSING', 'UNAVAILABLE', 'REMOVED_CONFIRMED', 'UNKNOWN'
    )),
    start_time_json TEXT CHECK (start_time_json IS NULL OR json_valid(start_time_json)),
    end_time_json TEXT CHECK (end_time_json IS NULL OR json_valid(end_time_json)),
    location TEXT CHECK (location IS NULL OR length(location) <= 4096)
) STRICT;

CREATE TABLE due_item (
    due_item_key INTEGER PRIMARY KEY,
    source_object_key INTEGER NOT NULL UNIQUE REFERENCES source_object(source_object_key),
    course_key INTEGER NOT NULL REFERENCES course(course_key),
    current_observation_key INTEGER NOT NULL UNIQUE REFERENCES source_observation(observation_key),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    calendar_id TEXT NOT NULL CHECK (length(calendar_id) > 0 AND length(calendar_id) <= 1024),
    item_source_id TEXT CHECK (item_source_id IS NULL OR length(item_source_id) <= 1024),
    item_source_type TEXT CHECK (item_source_type IS NULL OR length(item_source_type) <= 200),
    availability TEXT NOT NULL CHECK (availability IN (
        'ACTIVE', 'MISSING', 'UNAVAILABLE', 'REMOVED_CONFIRMED', 'UNKNOWN'
    )),
    source_start_time_json TEXT CHECK (
        source_start_time_json IS NULL OR json_valid(source_start_time_json)
    ),
    source_end_time_json TEXT CHECK (
        source_end_time_json IS NULL OR json_valid(source_end_time_json)
    ),
    due_time_json TEXT CHECK (due_time_json IS NULL OR json_valid(due_time_json))
) STRICT;

CREATE TABLE extraction_record (
    extraction_record_key INTEGER PRIMARY KEY,
    input_kind TEXT NOT NULL CHECK (input_kind IN ('source_observation', 'resource_version')),
    source_observation_key INTEGER REFERENCES source_observation(observation_key),
    version_key INTEGER REFERENCES resource_version(version_key),
    parse_key INTEGER REFERENCES parsed_document(parse_key),
    extractor_name TEXT NOT NULL CHECK (length(trim(extractor_name)) > 0),
    extractor_version TEXT NOT NULL CHECK (length(trim(extractor_version)) > 0),
    settings_hash TEXT NOT NULL CHECK (
        length(settings_hash) = 64
        AND settings_hash = lower(settings_hash)
        AND settings_hash NOT GLOB '*[^0-9a-f]*'
    ),
    input_hash TEXT NOT NULL CHECK (
        length(input_hash) = 64
        AND input_hash = lower(input_hash)
        AND input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    status TEXT NOT NULL CHECK (status IN ('COMPLETE', 'PARTIAL', 'FAILED')),
    warning_codes_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(warning_codes_json)),
    extracted_at TEXT NOT NULL,
    CHECK (
        (input_kind = 'source_observation' AND source_observation_key IS NOT NULL
         AND version_key IS NULL AND parse_key IS NULL)
        OR
        (input_kind = 'resource_version' AND source_observation_key IS NULL
         AND version_key IS NOT NULL AND parse_key IS NOT NULL)
    )
) STRICT;

CREATE UNIQUE INDEX extraction_source_cache_key
ON extraction_record(
    source_observation_key, extractor_name, extractor_version, settings_hash
)
WHERE input_kind = 'source_observation';

CREATE UNIQUE INDEX extraction_version_cache_key
ON extraction_record(parse_key, extractor_name, extractor_version, settings_hash)
WHERE input_kind = 'resource_version';

CREATE TABLE event_candidate (
    candidate_key INTEGER PRIMARY KEY,
    extraction_record_key INTEGER NOT NULL REFERENCES extraction_record(extraction_record_key),
    course_key INTEGER NOT NULL REFERENCES course(course_key),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    source_kind TEXT NOT NULL CHECK (source_kind IN (
        'announcement', 'assessment', 'schedule', 'due_item', 'document'
    )),
    raw_wording TEXT NOT NULL CHECK (length(raw_wording) <= 4096),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    resolution_state TEXT NOT NULL DEFAULT 'UNRESOLVED'
        CHECK (resolution_state = 'UNRESOLVED'),
    UNIQUE(extraction_record_key, ordinal)
) STRICT;

CREATE TABLE event_candidate_field (
    candidate_field_key INTEGER PRIMARY KEY,
    candidate_key INTEGER NOT NULL REFERENCES event_candidate(candidate_key),
    field_name TEXT NOT NULL CHECK (field_name IN (
        'title', 'event_type', 'start_time', 'end_time', 'due_time', 'open_at', 'close_at',
        'available_from', 'available_until', 'published_at', 'location', 'status'
    )),
    value_json TEXT NOT NULL CHECK (json_valid(value_json)),
    original_text TEXT NOT NULL CHECK (length(original_text) <= 4096),
    source_path TEXT NOT NULL CHECK (length(source_path) > 0 AND length(source_path) <= 500),
    temporal_precision TEXT CHECK (
        temporal_precision IS NULL OR temporal_precision IN (
            'EXACT_TIME', 'DATE_ONLY', 'WEEK_ONLY', 'UNKNOWN'
        )
    ),
    source_timezone TEXT,
    source_observation_key INTEGER REFERENCES source_observation(observation_key),
    locator_key INTEGER REFERENCES source_locator(locator_key),
    UNIQUE(candidate_key, field_name),
    CHECK (
        (source_observation_key IS NOT NULL AND locator_key IS NULL)
        OR (source_observation_key IS NULL AND locator_key IS NOT NULL)
    )
) STRICT;

CREATE TRIGGER source_observation_immutable_update BEFORE UPDATE ON source_observation
BEGIN SELECT RAISE(ABORT, 'source observations are immutable'); END;
CREATE TRIGGER source_observation_immutable_delete BEFORE DELETE ON source_observation
BEGIN SELECT RAISE(ABORT, 'source observations are immutable'); END;
CREATE TRIGGER extraction_record_immutable_update BEFORE UPDATE ON extraction_record
BEGIN SELECT RAISE(ABORT, 'extraction records are immutable'); END;
CREATE TRIGGER extraction_record_immutable_delete BEFORE DELETE ON extraction_record
BEGIN SELECT RAISE(ABORT, 'extraction records are immutable'); END;
CREATE TRIGGER event_candidate_immutable_update BEFORE UPDATE ON event_candidate
BEGIN SELECT RAISE(ABORT, 'event candidates are immutable'); END;
CREATE TRIGGER event_candidate_immutable_delete BEFORE DELETE ON event_candidate
BEGIN SELECT RAISE(ABORT, 'event candidates are immutable'); END;
CREATE TRIGGER event_candidate_field_immutable_update BEFORE UPDATE ON event_candidate_field
BEGIN SELECT RAISE(ABORT, 'event candidate fields are immutable'); END;
CREATE TRIGGER event_candidate_field_immutable_delete BEFORE DELETE ON event_candidate_field
BEGIN SELECT RAISE(ABORT, 'event candidate fields are immutable'); END;

CREATE TRIGGER announcement_integrity_insert BEFORE INSERT ON announcement
WHEN NOT EXISTS (
    SELECT 1 FROM source_observation observation
    JOIN source_object object ON object.source_object_key = observation.source_object_key
    JOIN course course_row ON course_row.course_key = NEW.course_key
    JOIN source_object course_object
      ON course_object.source_object_key = course_row.source_object_key
    WHERE observation.observation_key = NEW.current_observation_key
      AND observation.source_object_key = NEW.source_object_key
      AND observation.data_kind = 'announcement'
      AND object.object_kind = 'announcement'
      AND object.provider_key = course_object.provider_key
)
BEGIN SELECT RAISE(ABORT, 'announcement source integrity failure'); END;

CREATE TRIGGER assessment_integrity_insert BEFORE INSERT ON assessment
WHEN NOT EXISTS (
    SELECT 1 FROM source_observation observation
    JOIN source_object object ON object.source_object_key = observation.source_object_key
    JOIN content_node content ON content.content_key = NEW.content_key
    WHERE observation.observation_key = NEW.current_observation_key
      AND observation.source_object_key = NEW.source_object_key
      AND observation.data_kind = 'assessment'
      AND object.object_kind = 'assessment'
      AND content.course_key = NEW.course_key
)
BEGIN SELECT RAISE(ABORT, 'assessment source integrity failure'); END;

CREATE TRIGGER candidate_field_evidence_insert BEFORE INSERT ON event_candidate_field
WHEN NOT EXISTS (
    SELECT 1
    FROM event_candidate candidate
    JOIN extraction_record extraction
      ON extraction.extraction_record_key = candidate.extraction_record_key
    WHERE candidate.candidate_key = NEW.candidate_key
      AND (
        (extraction.input_kind = 'source_observation'
         AND NEW.source_observation_key = extraction.source_observation_key
         AND NEW.locator_key IS NULL)
        OR
        (extraction.input_kind = 'resource_version'
         AND NEW.source_observation_key IS NULL
         AND EXISTS (
             SELECT 1 FROM source_locator locator
             WHERE locator.locator_key = NEW.locator_key
               AND locator.version_key = extraction.version_key
         ))
      )
)
BEGIN SELECT RAISE(ABORT, 'candidate field evidence mismatch'); END;

CREATE TRIGGER extraction_parse_integrity_insert BEFORE INSERT ON extraction_record
WHEN NEW.input_kind = 'resource_version' AND NOT EXISTS (
    SELECT 1 FROM parsed_document parsed
    WHERE parsed.parse_key = NEW.parse_key AND parsed.version_key = NEW.version_key
)
BEGIN SELECT RAISE(ABORT, 'extraction parse input mismatch'); END;

DROP TRIGGER search_document_fts_insert;
DROP TRIGGER search_document_fts_delete;
DROP TRIGGER search_document_fts_update;
DROP TABLE search_document_fts;
DROP INDEX search_document_course_kind;
DROP INDEX search_document_filters;
ALTER TABLE search_document RENAME TO search_document_m5;

CREATE TABLE search_document (
    search_document_key INTEGER PRIMARY KEY,
    entity_kind TEXT NOT NULL CHECK (
        entity_kind IN ('course', 'content', 'material', 'chunk', 'announcement', 'assessment')
    ),
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
    parse_coverage TEXT CHECK (
        parse_coverage IS NULL OR parse_coverage IN (
            'COMPLETE', 'PARTIAL', 'STALE', 'UNKNOWN', 'FAILED'
        )
    ),
    text_origin TEXT NOT NULL CHECK (
        text_origin = 'metadata' OR text_origin = 'native' OR text_origin GLOB 'derived:*'
    ),
    body TEXT NOT NULL,
    CHECK (
        (entity_kind IN ('course', 'content', 'material')
         AND source_ref_kind = 'source_object' AND chunk_key IS NULL
         AND representation_key IS NULL AND text_origin = 'metadata')
        OR
        (entity_kind IN ('announcement', 'assessment')
         AND source_ref_kind = 'source_observation' AND chunk_key IS NULL
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

INSERT INTO search_document SELECT * FROM search_document_m5;
DROP TABLE search_document_m5;

CREATE INDEX search_document_course_kind ON search_document(course_key, entity_kind);
CREATE INDEX search_document_filters
ON search_document(semantic_type, file_format, availability);

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

CREATE TRIGGER search_dirty_announcement_insert AFTER INSERT ON announcement
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key) VALUES (NEW.course_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_announcement_update AFTER UPDATE ON announcement
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key) VALUES (NEW.course_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_assessment_insert AFTER INSERT ON assessment
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key) VALUES (NEW.course_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_assessment_update AFTER UPDATE ON assessment
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key) VALUES (NEW.course_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
