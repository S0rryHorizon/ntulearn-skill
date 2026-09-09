CREATE TABLE search_index_state (
    singleton_key INTEGER PRIMARY KEY CHECK (singleton_key = 1),
    source_generation INTEGER NOT NULL CHECK (source_generation >= 0),
    indexed_generation INTEGER NOT NULL CHECK (indexed_generation >= -1)
) STRICT;

INSERT INTO search_index_state(singleton_key, source_generation, indexed_generation)
VALUES (1, 0, -1);

CREATE TABLE search_dirty_course (
    course_key INTEGER PRIMARY KEY CHECK (course_key > 0)
) STRICT;

CREATE TABLE search_dirty_resource (
    resource_key INTEGER PRIMARY KEY CHECK (resource_key > 0)
) STRICT;

CREATE TABLE search_document (
    search_document_key INTEGER PRIMARY KEY,
    entity_kind TEXT NOT NULL CHECK (
        entity_kind IN ('course', 'content', 'material', 'chunk')
    ),
    entity_key INTEGER NOT NULL CHECK (entity_key > 0),
    course_key INTEGER NOT NULL,
    resource_key INTEGER,
    version_key INTEGER,
    chunk_key INTEGER,
    representation_key INTEGER,
    source_ref_kind TEXT NOT NULL CHECK (
        source_ref_kind IN ('source_object', 'source_locator')
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
        parse_coverage IS NULL
        OR parse_coverage IN ('COMPLETE', 'PARTIAL', 'STALE', 'UNKNOWN', 'FAILED')
    ),
    text_origin TEXT NOT NULL CHECK (
        text_origin = 'metadata'
        OR text_origin = 'native'
        OR text_origin GLOB 'derived:*'
    ),
    body TEXT NOT NULL,
    CHECK (
        (entity_kind IN ('course', 'content', 'material')
         AND source_ref_kind = 'source_object' AND chunk_key IS NULL
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

CREATE INDEX search_document_course_kind
ON search_document(course_key, entity_kind);

CREATE INDEX search_document_filters
ON search_document(semantic_type, file_format, availability);

CREATE VIRTUAL TABLE search_document_fts USING fts5(
    course_code,
    course_title,
    content_title,
    title,
    filename,
    semantic_type,
    body,
    content = 'search_document',
    content_rowid = 'search_document_key',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER search_document_fts_insert
AFTER INSERT ON search_document
BEGIN
    INSERT INTO search_document_fts(
        rowid, course_code, course_title, content_title, title, filename, semantic_type, body
    ) VALUES (
        NEW.search_document_key, NEW.course_code, NEW.course_title, NEW.content_title,
        NEW.title, NEW.filename, NEW.semantic_type, NEW.body
    );
END;

CREATE TRIGGER search_document_fts_delete
AFTER DELETE ON search_document
BEGIN
    INSERT INTO search_document_fts(
        search_document_fts, rowid, course_code, course_title, content_title,
        title, filename, semantic_type, body
    ) VALUES (
        'delete', OLD.search_document_key, OLD.course_code, OLD.course_title,
        OLD.content_title, OLD.title, OLD.filename, OLD.semantic_type, OLD.body
    );
END;

CREATE TRIGGER search_document_fts_update
AFTER UPDATE ON search_document
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

CREATE TRIGGER search_dirty_course_insert AFTER INSERT ON course
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key) VALUES (NEW.course_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_course_update AFTER UPDATE ON course
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key) VALUES (NEW.course_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_course_delete AFTER DELETE ON course
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key) VALUES (OLD.course_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;

CREATE TRIGGER search_dirty_content_insert AFTER INSERT ON content_node
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key) VALUES (NEW.course_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_content_update AFTER UPDATE ON content_node
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key) VALUES (NEW.course_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_content_delete AFTER DELETE ON content_node
BEGIN
    INSERT OR IGNORE INTO search_dirty_course(course_key) VALUES (OLD.course_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;

CREATE TRIGGER search_dirty_resource_insert AFTER INSERT ON resource
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key) VALUES (NEW.resource_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_resource_update AFTER UPDATE ON resource
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key) VALUES (NEW.resource_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_resource_delete AFTER DELETE ON resource
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key) VALUES (OLD.resource_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;

CREATE TRIGGER search_dirty_version_insert AFTER INSERT ON resource_version
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key) VALUES (NEW.resource_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_version_update AFTER UPDATE ON resource_version
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key) VALUES (NEW.resource_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_version_delete AFTER DELETE ON resource_version
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key) VALUES (OLD.resource_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;

CREATE TRIGGER search_dirty_observation_insert AFTER INSERT ON resource_observation
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key) VALUES (NEW.resource_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_observation_update AFTER UPDATE ON resource_observation
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key) VALUES (NEW.resource_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_observation_delete AFTER DELETE ON resource_observation
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key) VALUES (OLD.resource_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;

CREATE TRIGGER search_dirty_parse_insert AFTER INSERT ON parsed_document
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT resource_key FROM resource_version WHERE version_key = NEW.version_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_parse_update AFTER UPDATE ON parsed_document
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT resource_key FROM resource_version WHERE version_key = NEW.version_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_parse_delete AFTER DELETE ON parsed_document
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT resource_key FROM resource_version WHERE version_key = OLD.version_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;

CREATE TRIGGER search_dirty_chunk_insert AFTER INSERT ON document_chunk
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT v.resource_key
    FROM parsed_document p JOIN resource_version v ON v.version_key = p.version_key
    WHERE p.parse_key = NEW.parse_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_chunk_update AFTER UPDATE ON document_chunk
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT v.resource_key
    FROM parsed_document p JOIN resource_version v ON v.version_key = p.version_key
    WHERE p.parse_key = NEW.parse_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_chunk_delete AFTER DELETE ON document_chunk
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT v.resource_key
    FROM parsed_document p JOIN resource_version v ON v.version_key = p.version_key
    WHERE p.parse_key = OLD.parse_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;

CREATE TRIGGER search_dirty_locator_insert AFTER INSERT ON source_locator
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT resource_key FROM resource_version WHERE version_key = NEW.version_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_locator_update AFTER UPDATE ON source_locator
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT resource_key FROM resource_version WHERE version_key = NEW.version_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_locator_delete AFTER DELETE ON source_locator
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT resource_key FROM resource_version WHERE version_key = OLD.version_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;

CREATE TRIGGER search_dirty_representation_insert AFTER INSERT ON chunk_representation
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT v.resource_key
    FROM document_chunk chunk
    JOIN parsed_document p ON p.parse_key = chunk.parse_key
    JOIN resource_version v ON v.version_key = p.version_key
    WHERE chunk.chunk_key = NEW.chunk_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_representation_update AFTER UPDATE ON chunk_representation
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT v.resource_key
    FROM document_chunk chunk
    JOIN parsed_document p ON p.parse_key = chunk.parse_key
    JOIN resource_version v ON v.version_key = p.version_key
    WHERE chunk.chunk_key = NEW.chunk_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_representation_delete AFTER DELETE ON chunk_representation
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT v.resource_key
    FROM document_chunk chunk
    JOIN parsed_document p ON p.parse_key = chunk.parse_key
    JOIN resource_version v ON v.version_key = p.version_key
    WHERE chunk.chunk_key = OLD.chunk_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;

CREATE TRIGGER search_dirty_classification_insert AFTER INSERT ON material_classification
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT resource_key FROM classification_run
    WHERE classification_run_key = NEW.classification_run_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_classification_update AFTER UPDATE ON material_classification
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT resource_key FROM classification_run
    WHERE classification_run_key = NEW.classification_run_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_classification_delete AFTER DELETE ON material_classification
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key)
    SELECT resource_key FROM classification_run
    WHERE classification_run_key = OLD.classification_run_key;
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;

CREATE TRIGGER search_dirty_selection_insert AFTER INSERT ON material_classification_selection
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key) VALUES (NEW.resource_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_selection_update AFTER UPDATE ON material_classification_selection
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key) VALUES (NEW.resource_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
CREATE TRIGGER search_dirty_selection_delete AFTER DELETE ON material_classification_selection
BEGIN
    INSERT OR IGNORE INTO search_dirty_resource(resource_key) VALUES (OLD.resource_key);
    UPDATE search_index_state SET source_generation = source_generation + 1 WHERE singleton_key = 1;
END;
