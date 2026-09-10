"""Shared SQL predicates for current deterministic event interpretations."""

from __future__ import annotations

import re

_SQL_ALIAS = re.compile(r"[a-z][a-z0-9_]*\Z")


def _alias(value: str) -> str:
    if _SQL_ALIAS.fullmatch(value) is None:
        raise ValueError("SQL alias is invalid")
    return value


def effective_extraction_sql(extraction_alias: str) -> str:
    """Return the latest-successful predicate for one extraction alias."""

    extraction = _alias(extraction_alias)
    return f"""{extraction}.status IN ('COMPLETE', 'PARTIAL')
        AND NOT EXISTS (
            SELECT 1 FROM extraction_record newer
            WHERE newer.input_kind = {extraction}.input_kind
              AND newer.extractor_name = {extraction}.extractor_name
              AND newer.status IN ('COMPLETE', 'PARTIAL')
              AND newer.extraction_record_key > {extraction}.extraction_record_key
              AND (
                  ({extraction}.input_kind = 'source_observation'
                   AND newer.source_observation_key = {extraction}.source_observation_key)
                  OR
                  ({extraction}.input_kind = 'resource_version'
                   AND newer.version_key = {extraction}.version_key)
              )
        )"""


def active_event_source_sql(source_alias: str) -> str:
    """Return whether a source has a current extraction or explicit manual selection."""

    source = _alias(source_alias)
    effective = effective_extraction_sql("extraction")
    return f"""(
        EXISTS (
            SELECT 1 FROM event_candidate active_candidate
            JOIN extraction_record extraction
              ON extraction.extraction_record_key = active_candidate.extraction_record_key
            WHERE active_candidate.candidate_key = {source}.candidate_key
              AND {effective}
        )
        OR EXISTS (
            SELECT 1 FROM manual_identity_resolution identity_resolution
            WHERE identity_resolution.event_source_key = {source}.event_source_key
              AND identity_resolution.active = 1
        )
        OR EXISTS (
            SELECT 1 FROM manual_field_resolution field_resolution
            JOIN claim selected_claim
              ON selected_claim.claim_key = field_resolution.selected_claim_key
            WHERE selected_claim.event_source_key = {source}.event_source_key
              AND field_resolution.active = 1
        )
    )"""


def active_claim_sql(claim_alias: str) -> str:
    """Return whether a source or local claim contributes to current projections."""

    claim = _alias(claim_alias)
    active_source = active_event_source_sql("active_source")
    return f"""(
        ({claim}.origin = 'SOURCE' AND (
            EXISTS (
                SELECT 1 FROM event_source active_source
                WHERE active_source.event_source_key = {claim}.event_source_key
                  AND {active_source}
            )
            OR EXISTS (
                SELECT 1 FROM manual_field_resolution selected_resolution
                WHERE selected_resolution.selected_claim_key = {claim}.claim_key
                  AND selected_resolution.active = 1
            )
        ))
        OR ({claim}.origin = 'LOCAL_DECISION' AND EXISTS (
            SELECT 1 FROM manual_field_resolution local_resolution
            WHERE local_resolution.local_claim_key = {claim}.claim_key
              AND local_resolution.active = 1
        ))
    )"""


__all__ = ["active_claim_sql", "active_event_source_sql", "effective_extraction_sql"]
