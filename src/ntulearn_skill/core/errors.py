"""Privacy-safe error and warning mapping for core operations."""

from __future__ import annotations

import sqlite3
from typing import Any

from ntulearn_skill.client import (
    AuthenticationRequired,
    SessionExpired,
    SourceError,
    UnsupportedCapability,
    safe_source_error_category,
)
from ntulearn_skill.core.models import Coverage
from ntulearn_skill.core.results import (
    ErrorCategory,
    ResultEnvelope,
    SafeError,
)
from ntulearn_skill.storage import (
    MigrationError,
    StorageError,
)
from ntulearn_skill.sync.models import SyncRunResult


def safe_error(operation: str, error: Exception, scope: str) -> SafeError:
    category = ErrorCategory.STORAGE_FAILURE
    code = "operation_failed"
    retryable = False
    message = "The local operation could not be completed."
    source_category = (
        safe_source_error_category(getattr(error, "category", None))
        if isinstance(error, SourceError)
        else None
    )
    if isinstance(error, AuthenticationRequired) or source_category == "authentication_required":
        category, code, retryable, message = (
            ErrorCategory.AUTHENTICATION_REQUIRED,
            "authentication_required",
            True,
            "An authorized read session is required.",
        )
    elif isinstance(error, SessionExpired) or source_category == "session_expired":
        category, code, retryable, message = (
            ErrorCategory.SESSION_EXPIRED,
            "session_expired",
            True,
            "The authorized read session expired.",
        )
    elif isinstance(error, UnsupportedCapability) or source_category == "unsupported_capability":
        category, code, message = (
            ErrorCategory.CAPABILITY_UNSUPPORTED,
            "capability_unsupported",
            "The source does not support this read capability.",
        )
    elif isinstance(error, SourceError):
        category, code, retryable, message = (
            ErrorCategory.SOURCE_UNAVAILABLE,
            source_category or "source_unavailable",
            True,
            "The source read could not be completed.",
        )
    elif isinstance(error, MigrationError):
        category, code, message = (
            ErrorCategory.MIGRATION_FAILURE,
            "migration_failed",
            "The local database migration could not be completed.",
        )
    elif isinstance(error, StorageError) or isinstance(error, sqlite3.Error):
        category, code, message = (
            ErrorCategory.STORAGE_FAILURE,
            "storage_failed",
            "The local data operation could not be completed.",
        )
    elif isinstance(error, LookupError):
        category, code, message = (
            ErrorCategory.RESOURCE_UNAVAILABLE,
            "local_item_not_found",
            "The requested local item was not found.",
        )
    elif isinstance(error, (TypeError, ValueError)):
        category, code, message = (
            ErrorCategory.INVALID_REQUEST,
            "invalid_request",
            "The request is invalid.",
        )
    elif "sync_engine_unavailable" in str(error):
        category, code, retryable, message = (
            ErrorCategory.CONFIGURATION_REQUIRED,
            "sync_engine_unavailable",
            True,
            "A configured read-only sync engine is required.",
        )
    return SafeError(category, code, message, operation, scope, retryable, Coverage.FAILED)


def category_error(operation: str, code: str, scope: str) -> SafeError:
    mapping = {
        "authentication_required": (
            ErrorCategory.AUTHENTICATION_REQUIRED,
            True,
            "An authorized read session is required.",
        ),
        "session_expired": (
            ErrorCategory.SESSION_EXPIRED,
            True,
            "The authorized read session expired.",
        ),
        "unsupported_capability": (
            ErrorCategory.CAPABILITY_UNSUPPORTED,
            False,
            "The source does not support this read capability.",
        ),
        "source_unavailable": (
            ErrorCategory.SOURCE_UNAVAILABLE,
            True,
            "The source read could not be completed.",
        ),
    }
    for source_code in (
        "source_error",
        "source_access_denied",
        "read_policy_violation",
        "source_protocol_error",
        "pagination_limit_reached",
        "pagination_cycle",
        "course_not_observed",
        "content_node_limit_reached",
        "assessment_limit_reached",
        "resource_storage_error",
        "sync_engine_error",
    ):
        mapping[source_code] = (
            ErrorCategory.SOURCE_UNAVAILABLE,
            True,
            "The source read could not be completed.",
        )
    safe_code = code if code in mapping else "source_unavailable"
    category, retryable, message = mapping.get(
        safe_code,
        (ErrorCategory.SOURCE_UNAVAILABLE, True, "The source read could not be completed."),
    )
    return SafeError(category, safe_code, message, operation, scope, retryable, Coverage.FAILED)


def warning_message(code: str) -> str:
    return {
        "no_complete_observation": "No complete observation exists for this exact scope.",
        "max_age_unconfigured": "No maximum age is configured for this scope.",
        "no_max_age_guarantee": (
            "The complete local observation has no configured age limit; "
            "CURRENT does not guarantee a recent source read."
        ),
        "latest_attempt_incomplete": "The latest observation of this scope was incomplete.",
        "current_coverage_unestablished": "Current complete coverage is not established.",
    }.get(code, "Freshness could not be fully established for this scope.")


def not_found(operation: str, scope: str) -> ResultEnvelope[Any]:
    return ResultEnvelope(
        operation,
        errors=(
            SafeError(
                ErrorCategory.RESOURCE_UNAVAILABLE,
                "local_item_not_found",
                "The requested local item was not found.",
                operation,
                scope,
                False,
                Coverage.UNKNOWN,
            ),
        ),
        completeness=Coverage.UNKNOWN,
    )


def failure(operation: str, error: Exception, scope: str) -> ResultEnvelope[Any]:
    return ResultEnvelope(
        operation,
        errors=(safe_error(operation, error, scope),),
        completeness=Coverage.FAILED,
    )


def refresh_result_errors(operation: str, result: SyncRunResult) -> tuple[SafeError, ...]:
    failures = [
        (scope.failure_category, scope.data_kind)
        for scope in result.scopes
        if scope.failure_category is not None
    ]
    if result.error_category is not None:
        failures.append((result.error_category, "refresh"))
    return tuple(
        category_error(operation, code, scope)
        for code, scope in dict.fromkeys(failures)
        if code is not None
    )
