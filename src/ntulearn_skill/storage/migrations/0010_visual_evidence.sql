CREATE TABLE visual_evidence_metadata (
    representation_key INTEGER PRIMARY KEY
        REFERENCES chunk_representation(representation_key),
    version_key INTEGER NOT NULL REFERENCES resource_version(version_key),
    source_sha256 TEXT NOT NULL CHECK (
        length(source_sha256) = 64
        AND source_sha256 = lower(source_sha256)
        AND source_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    source_page_index INTEGER NOT NULL CHECK (source_page_index >= 0),
    rendered_sha256 TEXT NOT NULL CHECK (
        length(rendered_sha256) = 64
        AND rendered_sha256 = lower(rendered_sha256)
        AND rendered_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    method_version TEXT NOT NULL CHECK (length(trim(method_version)) > 0),
    settings_json TEXT NOT NULL CHECK (json_valid(settings_json)),
    review_status TEXT NOT NULL CHECK (review_status IN ('NEEDS_REVIEW', 'PARTIAL')),
    uncertainty_json TEXT NOT NULL CHECK (json_valid(uncertainty_json)),
    source_locator_json TEXT NOT NULL CHECK (json_valid(source_locator_json)),
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX visual_evidence_version_page
ON visual_evidence_metadata(version_key, source_page_index, representation_key);

CREATE TRIGGER visual_evidence_integrity_insert
BEFORE INSERT ON visual_evidence_metadata
WHEN NOT EXISTS (
    SELECT 1
    FROM chunk_representation representation
    JOIN document_chunk chunk ON chunk.chunk_key = representation.chunk_key
    JOIN parsed_document parsed ON parsed.parse_key = chunk.parse_key
    JOIN source_locator locator ON locator.chunk_key = chunk.chunk_key
    WHERE representation.representation_key = NEW.representation_key
      AND parsed.version_key = NEW.version_key
      AND parsed.resource_sha256 = NEW.source_sha256
      AND locator.format = 'pdf'
      AND locator.physical_page_index = NEW.source_page_index
)
BEGIN
    SELECT RAISE(ABORT, 'visual evidence source mismatch');
END;

CREATE TRIGGER visual_evidence_immutable_update
BEFORE UPDATE ON visual_evidence_metadata
BEGIN
    SELECT RAISE(ABORT, 'visual evidence metadata is immutable');
END;

CREATE TRIGGER visual_evidence_immutable_delete
BEFORE DELETE ON visual_evidence_metadata
BEGIN
    SELECT RAISE(ABORT, 'visual evidence metadata is immutable');
END;

DROP INDEX extraction_version_cache_key;

CREATE UNIQUE INDEX extraction_version_cache_key
ON extraction_record(parse_key, extractor_name, extractor_version, settings_hash, input_hash)
WHERE input_kind = 'resource_version';
