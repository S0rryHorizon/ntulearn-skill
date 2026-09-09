CREATE TABLE parsed_document (
    parse_key INTEGER PRIMARY KEY,
    version_key INTEGER NOT NULL REFERENCES resource_version(version_key),
    resource_sha256 TEXT NOT NULL CHECK (
        length(resource_sha256) = 64
        AND resource_sha256 = lower(resource_sha256)
        AND resource_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    parser_name TEXT NOT NULL CHECK (length(trim(parser_name)) > 0),
    parser_version TEXT NOT NULL CHECK (length(trim(parser_version)) > 0),
    engine_version TEXT NOT NULL CHECK (length(trim(engine_version)) > 0),
    settings_hash TEXT NOT NULL CHECK (
        length(settings_hash) = 64
        AND settings_hash = lower(settings_hash)
        AND settings_hash NOT GLOB '*[^0-9a-f]*'
    ),
    settings_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('COMPLETE', 'PARTIAL', 'FAILED', 'UNSUPPORTED')),
    coverage TEXT NOT NULL CHECK (coverage IN ('COMPLETE', 'PARTIAL', 'FAILED', 'UNKNOWN')),
    warning_codes_json TEXT NOT NULL DEFAULT '[]',
    error_code TEXT,
    parsed_at TEXT NOT NULL
) STRICT;

CREATE UNIQUE INDEX parsed_document_reusable_cache_key
ON parsed_document(version_key, parser_name, parser_version, engine_version, settings_hash)
WHERE status IN ('COMPLETE', 'PARTIAL', 'UNSUPPORTED');

CREATE TABLE document_chunk (
    chunk_key INTEGER PRIMARY KEY,
    parse_key INTEGER NOT NULL REFERENCES parsed_document(parse_key),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('page', 'paragraph', 'table', 'other')),
    native_text TEXT NOT NULL,
    locator_json TEXT NOT NULL,
    structured_elements_json TEXT NOT NULL DEFAULT '[]',
    diagnostic_json TEXT,
    UNIQUE(parse_key, ordinal)
) STRICT;

CREATE TABLE source_locator (
    locator_key INTEGER PRIMARY KEY,
    version_key INTEGER NOT NULL REFERENCES resource_version(version_key),
    chunk_key INTEGER NOT NULL UNIQUE REFERENCES document_chunk(chunk_key),
    format TEXT NOT NULL CHECK (format IN ('pdf', 'docx')),
    physical_page_index INTEGER CHECK (physical_page_index IS NULL OR physical_page_index >= 0),
    logical_page_label TEXT,
    docx_element_index INTEGER CHECK (docx_element_index IS NULL OR docx_element_index >= 0),
    docx_paragraph_index INTEGER CHECK (
        docx_paragraph_index IS NULL OR docx_paragraph_index >= 0
    ),
    docx_table_index INTEGER CHECK (docx_table_index IS NULL OR docx_table_index >= 0),
    character_start INTEGER CHECK (character_start IS NULL OR character_start >= 0),
    character_end INTEGER CHECK (character_end IS NULL OR character_end >= 0),
    structured_json TEXT NOT NULL DEFAULT '{}',
    CHECK (
        (character_start IS NULL AND character_end IS NULL)
        OR (character_start IS NOT NULL AND character_end IS NOT NULL
            AND character_end >= character_start)
    ),
    CHECK (
        (format = 'pdf' AND physical_page_index IS NOT NULL
         AND docx_element_index IS NULL AND docx_paragraph_index IS NULL
         AND docx_table_index IS NULL)
        OR
        (format = 'docx' AND physical_page_index IS NULL
         AND docx_element_index IS NOT NULL
         AND ((docx_paragraph_index IS NOT NULL AND docx_table_index IS NULL)
              OR (docx_paragraph_index IS NULL AND docx_table_index IS NOT NULL)))
    )
) STRICT;

CREATE TABLE chunk_representation (
    representation_key INTEGER PRIMARY KEY,
    chunk_key INTEGER NOT NULL REFERENCES document_chunk(chunk_key),
    representation_kind TEXT NOT NULL CHECK (
        representation_kind IN ('ocr_text', 'vision_description', 'rendered_derivative', 'other')
    ),
    text TEXT,
    artifact_relpath TEXT,
    method TEXT NOT NULL CHECK (length(trim(method)) > 0),
    provider TEXT,
    engine_version TEXT NOT NULL CHECK (length(trim(engine_version)) > 0),
    settings_hash TEXT NOT NULL CHECK (
        length(settings_hash) = 64
        AND settings_hash = lower(settings_hash)
        AND settings_hash NOT GLOB '*[^0-9a-f]*'
    ),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    diagnostic_reason TEXT NOT NULL CHECK (length(trim(diagnostic_reason)) > 0),
    created_at TEXT NOT NULL,
    CHECK (text IS NOT NULL OR artifact_relpath IS NOT NULL),
    UNIQUE(chunk_key, representation_kind, method, engine_version, settings_hash)
) STRICT;

CREATE TABLE classification_run (
    classification_run_key INTEGER PRIMARY KEY,
    resource_key INTEGER NOT NULL REFERENCES resource(resource_key),
    version_key INTEGER REFERENCES resource_version(version_key),
    classifier_name TEXT NOT NULL CHECK (length(trim(classifier_name)) > 0),
    classifier_version TEXT NOT NULL CHECK (length(trim(classifier_version)) > 0),
    input_hash TEXT NOT NULL CHECK (
        length(input_hash) = 64
        AND input_hash = lower(input_hash)
        AND input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    settings_hash TEXT NOT NULL CHECK (
        length(settings_hash) = 64
        AND settings_hash = lower(settings_hash)
        AND settings_hash NOT GLOB '*[^0-9a-f]*'
    ),
    settings_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('COMPLETE', 'PARTIAL', 'FAILED')),
    warning_codes_json TEXT NOT NULL DEFAULT '[]',
    error_code TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE UNIQUE INDEX classification_run_cache_key
ON classification_run(
    resource_key, IFNULL(version_key, 0), classifier_name, classifier_version,
    input_hash, settings_hash
);

CREATE TABLE material_classification (
    classification_key INTEGER PRIMARY KEY,
    classification_run_key INTEGER NOT NULL
        REFERENCES classification_run(classification_run_key),
    semantic_type TEXT NOT NULL CHECK (semantic_type IN (
        'lecture_slides', 'tutorial', 'lab_manual', 'assignment_brief', 'syllabus',
        'assessment_information', 'reading', 'reference', 'unknown'
    )),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    evidence_json TEXT NOT NULL,
    user_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (user_confirmed IN (0, 1)),
    rank INTEGER NOT NULL CHECK (rank >= 0),
    UNIQUE(classification_run_key, rank)
) STRICT;

CREATE TABLE material_classification_selection (
    selection_key INTEGER PRIMARY KEY,
    resource_key INTEGER NOT NULL REFERENCES resource(resource_key),
    classification_key INTEGER NOT NULL REFERENCES material_classification(classification_key),
    supersedes_selection_key INTEGER REFERENCES material_classification_selection(selection_key),
    selected_at TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0)
) STRICT;

CREATE INDEX document_chunk_parse_ordinal
ON document_chunk(parse_key, ordinal);

CREATE INDEX material_classification_resource
ON classification_run(resource_key, created_at);

CREATE TRIGGER parsed_document_version_integrity_insert
BEFORE INSERT ON parsed_document
WHEN NOT EXISTS (
    SELECT 1 FROM resource_version v
    WHERE v.version_key = NEW.version_key
      AND v.sha256 = NEW.resource_sha256
      AND v.verification_status = 'VERIFIED'
)
BEGIN
    SELECT RAISE(ABORT, 'parsed document version evidence mismatch');
END;

CREATE TRIGGER parsed_document_immutable_update
BEFORE UPDATE ON parsed_document
BEGIN
    SELECT RAISE(ABORT, 'parsed documents are immutable');
END;

CREATE TRIGGER parsed_document_immutable_delete
BEFORE DELETE ON parsed_document
BEGIN
    SELECT RAISE(ABORT, 'parsed documents are immutable');
END;

CREATE TRIGGER document_chunk_immutable_update
BEFORE UPDATE ON document_chunk
BEGIN
    SELECT RAISE(ABORT, 'document chunks are immutable');
END;

CREATE TRIGGER document_chunk_immutable_delete
BEFORE DELETE ON document_chunk
BEGIN
    SELECT RAISE(ABORT, 'document chunks are immutable');
END;

CREATE TRIGGER chunk_representation_immutable_update
BEFORE UPDATE ON chunk_representation
BEGIN
    SELECT RAISE(ABORT, 'chunk representations are immutable');
END;

CREATE TRIGGER chunk_representation_immutable_delete
BEFORE DELETE ON chunk_representation
BEGIN
    SELECT RAISE(ABORT, 'chunk representations are immutable');
END;

CREATE TRIGGER source_locator_version_integrity_insert
BEFORE INSERT ON source_locator
WHEN NOT EXISTS (
    SELECT 1
    FROM document_chunk c
    JOIN parsed_document p ON p.parse_key = c.parse_key
    WHERE c.chunk_key = NEW.chunk_key AND p.version_key = NEW.version_key
)
BEGIN
    SELECT RAISE(ABORT, 'source locator version and chunk mismatch');
END;

CREATE TRIGGER source_locator_immutable_update
BEFORE UPDATE ON source_locator
BEGIN
    SELECT RAISE(ABORT, 'source locators are immutable');
END;

CREATE TRIGGER source_locator_immutable_delete
BEFORE DELETE ON source_locator
BEGIN
    SELECT RAISE(ABORT, 'source locators are immutable');
END;

CREATE TRIGGER classification_run_version_integrity_insert
BEFORE INSERT ON classification_run
WHEN NEW.version_key IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM resource_version v
    WHERE v.version_key = NEW.version_key
      AND v.resource_key = NEW.resource_key
      AND v.verification_status = 'VERIFIED'
)
BEGIN
    SELECT RAISE(ABORT, 'classification version belongs to another resource');
END;

CREATE TRIGGER classification_run_immutable_update
BEFORE UPDATE ON classification_run
BEGIN
    SELECT RAISE(ABORT, 'classification runs are immutable');
END;

CREATE TRIGGER classification_run_immutable_delete
BEFORE DELETE ON classification_run
BEGIN
    SELECT RAISE(ABORT, 'classification runs are immutable');
END;

CREATE TRIGGER material_classification_immutable_update
BEFORE UPDATE ON material_classification
BEGIN
    SELECT RAISE(ABORT, 'material classifications are immutable');
END;

CREATE TRIGGER material_classification_immutable_delete
BEFORE DELETE ON material_classification
BEGIN
    SELECT RAISE(ABORT, 'material classifications are immutable');
END;

CREATE TRIGGER material_classification_selection_integrity_insert
BEFORE INSERT ON material_classification_selection
WHEN NOT EXISTS (
    SELECT 1
    FROM material_classification c
    JOIN classification_run r ON r.classification_run_key = c.classification_run_key
    WHERE c.classification_key = NEW.classification_key
      AND r.resource_key = NEW.resource_key
)
OR (
    NEW.supersedes_selection_key IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM material_classification_selection s
        WHERE s.selection_key = NEW.supersedes_selection_key
          AND s.resource_key = NEW.resource_key
    )
)
BEGIN
    SELECT RAISE(ABORT, 'classification selection resource mismatch');
END;

CREATE TRIGGER material_classification_selection_immutable_update
BEFORE UPDATE ON material_classification_selection
BEGIN
    SELECT RAISE(ABORT, 'classification selections are immutable');
END;

CREATE TRIGGER material_classification_selection_immutable_delete
BEFORE DELETE ON material_classification_selection
BEGIN
    SELECT RAISE(ABORT, 'classification selections are immutable');
END;
