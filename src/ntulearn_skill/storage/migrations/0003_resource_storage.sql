ALTER TABLE course ADD COLUMN browse_dir_name TEXT;

CREATE UNIQUE INDEX course_browse_dir_name_unique
ON course(browse_dir_name)
WHERE browse_dir_name IS NOT NULL;

CREATE TABLE resource (
    resource_key INTEGER PRIMARY KEY,
    source_object_key INTEGER NOT NULL UNIQUE REFERENCES source_object(source_object_key),
    content_key INTEGER NOT NULL REFERENCES content_node(content_key),
    display_title TEXT NOT NULL CHECK (length(trim(display_title)) > 0),
    availability TEXT NOT NULL CHECK (availability IN (
        'ACTIVE', 'MISSING', 'UNAVAILABLE', 'REMOVED_CONFIRMED', 'UNKNOWN'
    )),
    browse_dir_name TEXT NOT NULL UNIQUE CHECK (length(browse_dir_name) > 0),
    current_version_key INTEGER REFERENCES resource_version(version_key),
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL
) STRICT;

CREATE TABLE resource_version (
    version_key INTEGER PRIMARY KEY,
    resource_key INTEGER NOT NULL REFERENCES resource(resource_key),
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64 AND sha256 = lower(sha256) AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    file_format TEXT NOT NULL CHECK (file_format IN ('pdf', 'docx', 'pptx', 'unknown')),
    declared_mime TEXT,
    downloaded_at TEXT NOT NULL,
    blob_relpath TEXT NOT NULL CHECK (length(blob_relpath) > 0),
    browse_relpath TEXT NOT NULL CHECK (length(browse_relpath) > 0),
    verification_status TEXT NOT NULL CHECK (verification_status IN (
        'VERIFIED', 'QUARANTINED', 'FAILED'
    )),
    UNIQUE(resource_key, version_number),
    UNIQUE(resource_key, sha256)
) STRICT;

CREATE TABLE resource_observation (
    observation_key INTEGER PRIMARY KEY,
    resource_key INTEGER NOT NULL REFERENCES resource(resource_key),
    sync_run_key INTEGER NOT NULL REFERENCES sync_run(sync_run_key),
    observation_status TEXT NOT NULL CHECK (observation_status IN (
        'OBSERVED', 'NOT_OBSERVED', 'UNAVAILABLE', 'UNKNOWN'
    )),
    original_filename TEXT NOT NULL,
    sanitized_metadata_json TEXT NOT NULL DEFAULT '{}',
    metadata_fingerprint TEXT NOT NULL CHECK (
        length(metadata_fingerprint) = 64
        AND metadata_fingerprint = lower(metadata_fingerprint)
        AND metadata_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    candidate_modified_at TEXT,
    candidate_revision TEXT,
    availability TEXT NOT NULL CHECK (availability IN (
        'ACTIVE', 'MISSING', 'UNAVAILABLE', 'REMOVED_CONFIRMED', 'UNKNOWN'
    )),
    observed_at TEXT NOT NULL,
    fetch_decision TEXT NOT NULL CHECK (fetch_decision IN (
        'FETCHED', 'REUSED_VERIFIED', 'DEFERRED', 'NOT_NEEDED', 'FAILED'
    )),
    version_key INTEGER REFERENCES resource_version(version_key)
) STRICT;

CREATE TRIGGER course_browse_dir_immutable
BEFORE UPDATE OF browse_dir_name ON course
WHEN OLD.browse_dir_name IS NOT NULL AND NEW.browse_dir_name IS NOT OLD.browse_dir_name
BEGIN
    SELECT RAISE(ABORT, 'course browse directory is immutable');
END;

CREATE TRIGGER resource_source_integrity_insert
BEFORE INSERT ON resource
WHEN
    (SELECT object_kind FROM source_object
     WHERE source_object_key = NEW.source_object_key) <> 'attachment'
    OR
    (SELECT provider_key FROM source_object
     WHERE source_object_key = NEW.source_object_key) <>
    (SELECT so.provider_key
     FROM content_node n
     JOIN course c ON c.course_key = n.course_key
     JOIN source_object so ON so.source_object_key = c.source_object_key
     WHERE n.content_key = NEW.content_key)
BEGIN
    SELECT RAISE(ABORT, 'resource source kind or provider mismatch');
END;

CREATE TRIGGER resource_identity_immutable
BEFORE UPDATE OF source_object_key, content_key, browse_dir_name ON resource
WHEN NEW.source_object_key IS NOT OLD.source_object_key
  OR NEW.content_key IS NOT OLD.content_key
  OR NEW.browse_dir_name IS NOT OLD.browse_dir_name
BEGIN
    SELECT RAISE(ABORT, 'resource identity is immutable');
END;

CREATE TRIGGER resource_current_version_insert
BEFORE INSERT ON resource
WHEN NEW.current_version_key IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM resource_version v
     WHERE v.version_key = NEW.current_version_key
       AND v.resource_key = NEW.resource_key
       AND v.verification_status = 'VERIFIED'
 )
BEGIN
    SELECT RAISE(ABORT, 'current version belongs to another resource');
END;

CREATE TRIGGER resource_current_version_update
BEFORE UPDATE OF current_version_key ON resource
WHEN NEW.current_version_key IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM resource_version v
     WHERE v.version_key = NEW.current_version_key
       AND v.resource_key = NEW.resource_key
       AND v.verification_status = 'VERIFIED'
 )
BEGIN
    SELECT RAISE(ABORT, 'current version belongs to another resource');
END;

CREATE TRIGGER resource_observation_version_integrity
BEFORE INSERT ON resource_observation
WHEN NEW.version_key IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM resource_version v
     WHERE v.version_key = NEW.version_key
       AND v.resource_key = NEW.resource_key
       AND v.verification_status = 'VERIFIED'
 )
BEGIN
    SELECT RAISE(ABORT, 'observation version belongs to another resource');
END;

CREATE TRIGGER resource_version_immutable_update
BEFORE UPDATE ON resource_version
BEGIN
    SELECT RAISE(ABORT, 'resource versions are immutable');
END;

CREATE TRIGGER resource_version_immutable_delete
BEFORE DELETE ON resource_version
BEGIN
    SELECT RAISE(ABORT, 'resource versions are immutable');
END;

CREATE TRIGGER resource_observation_immutable_update
BEFORE UPDATE ON resource_observation
BEGIN
    SELECT RAISE(ABORT, 'resource observations are immutable');
END;

CREATE TRIGGER resource_observation_immutable_delete
BEFORE DELETE ON resource_observation
BEGIN
    SELECT RAISE(ABORT, 'resource observations are immutable');
END;
