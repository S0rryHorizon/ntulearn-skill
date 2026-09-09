CREATE TRIGGER course_source_kind_insert
BEFORE INSERT ON course
WHEN (SELECT object_kind FROM source_object
      WHERE source_object_key = NEW.source_object_key) <> 'course'
BEGIN
    SELECT RAISE(ABORT, 'course requires a course source object');
END;

CREATE TRIGGER course_source_kind_update
BEFORE UPDATE OF source_object_key ON course
WHEN (SELECT object_kind FROM source_object
      WHERE source_object_key = NEW.source_object_key) <> 'course'
BEGIN
    SELECT RAISE(ABORT, 'course requires a course source object');
END;

CREATE TRIGGER content_source_integrity_insert
BEFORE INSERT ON content_node
WHEN
    (SELECT object_kind FROM source_object
     WHERE source_object_key = NEW.source_object_key) <> 'content'
    OR
    (SELECT provider_key FROM source_object
     WHERE source_object_key = NEW.source_object_key) <>
    (SELECT so.provider_key
     FROM course c
     JOIN source_object so ON so.source_object_key = c.source_object_key
     WHERE c.course_key = NEW.course_key)
BEGIN
    SELECT RAISE(ABORT, 'content source kind or provider mismatch');
END;

CREATE TRIGGER content_source_integrity_update
BEFORE UPDATE OF source_object_key, course_key ON content_node
WHEN
    (SELECT object_kind FROM source_object
     WHERE source_object_key = NEW.source_object_key) <> 'content'
    OR
    (SELECT provider_key FROM source_object
     WHERE source_object_key = NEW.source_object_key) <>
    (SELECT so.provider_key
     FROM course c
     JOIN source_object so ON so.source_object_key = c.source_object_key
     WHERE c.course_key = NEW.course_key)
BEGIN
    SELECT RAISE(ABORT, 'content source kind or provider mismatch');
END;

CREATE TRIGGER content_parent_course_insert
BEFORE INSERT ON content_node
WHEN NEW.parent_content_key IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM content_node parent
     WHERE parent.content_key = NEW.parent_content_key
       AND parent.course_key = NEW.course_key
 )
BEGIN
    SELECT RAISE(ABORT, 'content parent belongs to another course');
END;

CREATE TRIGGER content_parent_course_update
BEFORE UPDATE OF parent_content_key, course_key ON content_node
WHEN NEW.parent_content_key IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM content_node parent
     WHERE parent.content_key = NEW.parent_content_key
       AND parent.course_key = NEW.course_key
 )
BEGIN
    SELECT RAISE(ABORT, 'content parent belongs to another course');
END;

CREATE TRIGGER content_course_immutable
BEFORE UPDATE OF course_key ON content_node
WHEN NEW.course_key <> OLD.course_key
BEGIN
    SELECT RAISE(ABORT, 'content course identity is immutable');
END;

CREATE TRIGGER content_parent_acyclic
BEFORE UPDATE OF parent_content_key ON content_node
WHEN NEW.parent_content_key IS NOT NULL
 AND EXISTS (
     WITH RECURSIVE descendants(content_key) AS (
         SELECT NEW.content_key
         UNION ALL
         SELECT child.content_key
         FROM content_node child
         JOIN descendants parent ON child.parent_content_key = parent.content_key
     )
     SELECT 1 FROM descendants WHERE content_key = NEW.parent_content_key
 )
BEGIN
    SELECT RAISE(ABORT, 'content parent would create a cycle');
END;

CREATE TRIGGER source_object_identity_immutable
BEFORE UPDATE OF provider_key, object_kind, remote_key ON source_object
WHEN NEW.provider_key <> OLD.provider_key
  OR NEW.object_kind <> OLD.object_kind
  OR NEW.remote_key <> OLD.remote_key
BEGIN
    SELECT RAISE(ABORT, 'source object identity is immutable');
END;
