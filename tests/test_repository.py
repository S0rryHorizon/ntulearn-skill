import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ntulearn_skill.core import AttachmentId, Availability, ContentId, CourseId
from ntulearn_skill.storage import Database, DomainRepository, StorageError


@pytest.fixture
def repository(tmp_path: Path) -> DomainRepository:
    value = DomainRepository(Database(tmp_path / "private" / "db" / "metadata.sqlite3"))
    assert value.initialize() == 7
    return value


def test_course_and_nested_content_round_trip(repository: DomainRepository) -> None:
    observed_at = datetime(2027, 1, 8, 3, 4, 5, tzinfo=UTC)
    course_id = CourseId("synthetic", "course-alpha")
    course = repository.put_course(
        course_id,
        code="PH0000",
        title="Example Physics Course",
        term="Synthetic Term",
        observed_at=observed_at,
    )
    root = repository.put_content_node(
        ContentId("synthetic", "content-root"),
        course_id=course_id,
        handler_kind="folder",
        title="Invented Materials",
        position=0,
        sanitized_metadata={"display_style": "synthetic"},
        observed_at=observed_at,
    )
    child_id = ContentId("synthetic", "content-child")
    child = repository.put_content_node(
        child_id,
        course_id=course_id,
        parent_id=root.remote_id,
        handler_kind="document",
        title="Invented Reading",
        position=1,
        availability=Availability.UNAVAILABLE,
        observed_at=observed_at,
    )

    assert repository.get_course(course_id) == course
    assert repository.get_content_node(child_id) == child
    assert child.parent_key == root.key
    assert child.course_key == course.key
    assert child.availability is Availability.UNAVAILABLE


def test_wrong_identifier_namespace_is_rejected_before_sql(repository: DomainRepository) -> None:
    same_value = "opaque-1"
    repository.put_course(
        CourseId("synthetic", same_value),
        code="CS0000",
        title="Example Computing Course",
    )

    with pytest.raises(TypeError, match="CourseId"):
        repository.get_course(ContentId("synthetic", same_value))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ContentId"):
        repository.put_content_node(  # type: ignore[arg-type]
            AttachmentId("synthetic", same_value),
            course_id=CourseId("synthetic", same_value),
            handler_kind="folder",
            title="Invented",
            position=0,
        )


def test_cross_course_parent_is_rejected_without_partial_write(
    repository: DomainRepository,
) -> None:
    first = CourseId("synthetic", "course-one")
    second = CourseId("synthetic", "course-two")
    repository.put_course(first, code="AA0000", title="Example One")
    repository.put_course(second, code="BB0000", title="Example Two")
    parent = repository.put_content_node(
        ContentId("synthetic", "parent"),
        course_id=first,
        handler_kind="folder",
        title="Parent",
        position=0,
    )
    child_id = ContentId("synthetic", "invalid-child")

    with pytest.raises(ValueError, match="same course"):
        repository.put_content_node(
            child_id,
            course_id=second,
            parent_id=parent.remote_id,
            handler_kind="document",
            title="Child",
            position=0,
        )

    assert repository.get_content_node(child_id) is None


def test_non_allowlisted_metadata_is_rejected_and_not_persisted(
    repository: DomainRepository,
) -> None:
    course_id = CourseId("synthetic", "metadata-course")
    repository.put_course(course_id, code="PH0000", title="Example Physics Course")
    content_id = ContentId("synthetic", "metadata-content")

    with pytest.raises(ValueError, match="public allowlist"):
        repository.put_content_node(
            content_id,
            course_id=course_id,
            handler_kind="document",
            title="Invented",
            position=0,
            sanitized_metadata={"nested": {"signed-url": "fake-private-value"}},
        )

    assert repository.get_content_node(content_id) is None


def test_content_tree_rejects_self_parent_and_ancestor_cycles(
    repository: DomainRepository,
) -> None:
    course_id = CourseId("synthetic", "tree-course")
    repository.put_course(course_id, code="CS0000", title="Example Computing Course")
    root_id = ContentId("synthetic", "tree-root")
    child_id = ContentId("synthetic", "tree-child")
    repository.put_content_node(
        root_id,
        course_id=course_id,
        handler_kind="folder",
        title="Root",
        position=0,
    )
    repository.put_content_node(
        child_id,
        course_id=course_id,
        parent_id=root_id,
        handler_kind="folder",
        title="Child",
        position=0,
    )

    with pytest.raises(ValueError, match="cycle"):
        repository.put_content_node(
            root_id,
            course_id=course_id,
            parent_id=child_id,
            handler_kind="folder",
            title="Root",
            position=0,
        )
    with pytest.raises(ValueError, match="cycle"):
        repository.put_content_node(
            child_id,
            course_id=course_id,
            parent_id=child_id,
            handler_kind="folder",
            title="Child",
            position=0,
        )


def test_content_course_and_source_identity_are_immutable(
    repository: DomainRepository,
) -> None:
    first = CourseId("synthetic", "stable-one")
    second = CourseId("synthetic", "stable-two")
    repository.put_course(first, code="AA0000", title="Example One")
    repository.put_course(second, code="BB0000", title="Example Two")
    content_id = ContentId("synthetic", "stable-content")
    content = repository.put_content_node(
        content_id,
        course_id=first,
        handler_kind="document",
        title="Stable",
        position=0,
    )

    with pytest.raises(ValueError, match="cannot move"):
        repository.put_content_node(
            content_id,
            course_id=second,
            handler_kind="document",
            title="Stable",
            position=0,
        )

    connection = repository.database.connect()
    try:
        source_key = connection.execute(
            "SELECT source_object_key FROM content_node WHERE content_key = ?", (content.key,)
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            connection.execute(
                "UPDATE source_object SET remote_key = ? WHERE source_object_key = ?",
                ("changed", source_key),
            )
        with pytest.raises(sqlite3.IntegrityError, match="course identity is immutable"):
            second_key = repository.get_course(second)
            assert second_key is not None
            connection.execute(
                "UPDATE content_node SET course_key = ? WHERE content_key = ?",
                (second_key.key, content.key),
            )
    finally:
        connection.close()


def test_database_constraint_rejects_content_cycle(repository: DomainRepository) -> None:
    course_id = CourseId("synthetic", "database-tree")
    repository.put_course(course_id, code="CS0000", title="Example Computing Course")
    root = repository.put_content_node(
        ContentId("synthetic", "database-root"),
        course_id=course_id,
        handler_kind="folder",
        title="Root",
        position=0,
    )
    child = repository.put_content_node(
        ContentId("synthetic", "database-child"),
        course_id=course_id,
        parent_id=root.remote_id,
        handler_kind="folder",
        title="Child",
        position=0,
    )

    connection = repository.database.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="create a cycle"):
            connection.execute(
                "UPDATE content_node SET parent_content_key = ? WHERE content_key = ?",
                (child.key, root.key),
            )
    finally:
        connection.close()


def test_database_constraints_reject_wrong_source_kind(repository: DomainRepository) -> None:
    course_id = CourseId("synthetic", "constraint-course")
    course = repository.put_course(course_id, code="PH0000", title="Example Physics Course")
    connection = repository.database.connect()
    try:
        source_key = connection.execute(
            "SELECT source_object_key FROM course WHERE course_key = ?", (course.key,)
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="content source kind"):
            connection.execute(
                """
                INSERT INTO content_node(
                    source_object_key, course_key, handler_kind, title, position,
                    availability, first_observed_at, last_observed_at
                ) VALUES (?, ?, 'folder', 'Invalid', 0, 'ACTIVE', ?, ?)
                """,
                (
                    source_key,
                    course.key,
                    datetime.now(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
    finally:
        connection.close()


def test_storage_errors_do_not_echo_private_values(repository: DomainRepository) -> None:
    course_id = CourseId("synthetic", "private-looking-opaque-value")
    repository.put_course(course_id, code="PH0000", title="Example Physics Course")
    connection = repository.database.connect()
    try:
        connection.execute("DROP TABLE course")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StorageError) as captured:
        repository.get_course(course_id)
    assert course_id.value not in str(captured.value)


def test_older_course_observation_preserves_newer_state_and_orders_timestamps(
    repository: DomainRepository,
) -> None:
    course_id = CourseId("synthetic", "ordered-course")
    newer = datetime(2027, 1, 2, tzinfo=UTC)
    older = datetime(2027, 1, 1, tzinfo=UTC)
    repository.put_course(
        course_id,
        code="PH0000",
        title="Newer Synthetic Title",
        availability=Availability.ACTIVE,
        observed_at=newer,
    )
    result = repository.put_course(
        course_id,
        code="PH0000-OLD",
        title="Older Synthetic Title",
        availability=Availability.UNAVAILABLE,
        observed_at=older,
    )

    assert result.code == "PH0000"
    assert result.title == "Newer Synthetic Title"
    assert result.availability is Availability.ACTIVE
    assert result.first_observed_at == older
    assert result.last_observed_at == newer

    connection = repository.database.connect()
    try:
        row = connection.execute(
            """
            SELECT p.created_at, p.updated_at, so.first_observed_at, so.last_observed_at
            FROM course c
            JOIN source_object so ON so.source_object_key = c.source_object_key
            JOIN source_provider p ON p.provider_key = so.provider_key
            WHERE c.course_key = ?
            """,
            (result.key,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert datetime.fromisoformat(row["created_at"]) == older
    assert datetime.fromisoformat(row["updated_at"]) == newer
    assert datetime.fromisoformat(row["first_observed_at"]) == older
    assert datetime.fromisoformat(row["last_observed_at"]) == newer


def test_older_content_observation_preserves_newer_fields_and_parent(
    repository: DomainRepository,
) -> None:
    course_id = CourseId("synthetic", "ordered-content-course")
    other_course_id = CourseId("synthetic", "other-content-course")
    older = datetime(2027, 1, 1, tzinfo=UTC)
    newer = datetime(2027, 1, 2, tzinfo=UTC)
    repository.put_course(
        course_id, code="CS0000", title="Example Computing Course", observed_at=older
    )
    repository.put_course(
        other_course_id, code="EE0000", title="Other Example Course", observed_at=older
    )
    old_parent = repository.put_content_node(
        ContentId("synthetic", "old-parent"),
        course_id=course_id,
        handler_kind="folder",
        title="Old Parent",
        position=0,
        observed_at=older,
    )
    new_parent = repository.put_content_node(
        ContentId("synthetic", "new-parent"),
        course_id=course_id,
        handler_kind="folder",
        title="New Parent",
        position=1,
        observed_at=older,
    )
    child_id = ContentId("synthetic", "ordered-child")
    repository.put_content_node(
        child_id,
        course_id=course_id,
        parent_id=new_parent.remote_id,
        handler_kind="document",
        title="Newer Synthetic Content",
        position=2,
        availability=Availability.ACTIVE,
        sanitized_metadata={"module_label": "newer"},
        observed_at=newer,
    )
    result = repository.put_content_node(
        child_id,
        course_id=course_id,
        parent_id=old_parent.remote_id,
        handler_kind="folder",
        title="Older Synthetic Content",
        position=9,
        availability=Availability.UNAVAILABLE,
        sanitized_metadata={"module_label": "older"},
        observed_at=older,
    )

    assert result.parent_key == new_parent.key
    assert result.handler_kind == "document"
    assert result.title == "Newer Synthetic Content"
    assert result.position == 2
    assert result.availability is Availability.ACTIVE
    assert result.sanitized_metadata == {"module_label": "newer"}
    assert result.first_observed_at == older
    assert result.last_observed_at == newer

    # Even stale input that names another course cannot move the latest relationship.
    replayed = repository.put_content_node(
        child_id,
        course_id=other_course_id,
        handler_kind="folder",
        title="Stale Cross-Course Value",
        position=0,
        observed_at=older,
    )
    assert replayed.course_key == result.course_key
    assert replayed.parent_key == new_parent.key

    # Stale relationship values are not resolved because they cannot replace current state.
    replayed = repository.put_content_node(
        child_id,
        course_id=CourseId("synthetic", "missing-stale-course"),
        parent_id=ContentId("synthetic", "missing-stale-parent"),
        handler_kind="folder",
        title="Stale Missing Relationships",
        position=0,
        observed_at=older,
    )
    assert replayed.course_key == result.course_key
    assert replayed.parent_key == new_parent.key
