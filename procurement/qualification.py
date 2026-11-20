"""Supplier qualification evidence for bid review.

A qualification document is a complete revision history of supplier
credentials.  A review instant selects, per ``credential_id``, the highest
revision recorded no later than that instant; that revision is the complete
visible state.  This module validates the document, builds the visible state,
and checks whether one credential version proves one document code for one
offer.

All timestamp comparisons use the actual instant represented by an
offset-aware ISO-8601 value.  Validity intervals are half-open:
``valid_from <= instant < valid_until``.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
STATUSES = frozenset({"active", "suspended", "revoked"})

_DOCUMENT_FIELDS = frozenset({"schema_version", "credentials"})
_RECORD_FIELDS = frozenset(
    {
        "credential_id",
        "revision",
        "vendor_id",
        "document_code",
        "status",
        "valid_from",
        "valid_until",
        "recorded_at",
    }
)

# Reasons reported when a declared, required document code is not proven.
REASON_NO_CREDENTIAL = "no_credential"
REASON_OUTSIDE_VALIDITY = "outside_validity"


class QualificationError(ValueError):
    """Qualification input is invalid.  No review result may be produced."""


# ---------------------------------------------------------------------------
# Strict JSON loading


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise QualificationError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_constant(name):
    raise QualificationError(f"non-finite JSON number {name} is not allowed")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise QualificationError(f"non-finite JSON number {text} is not allowed")
    return value


def parse_json_text(text, source):
    """Parse strict JSON, rejecting duplicate keys and non-finite numbers."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except QualificationError as exc:
        raise QualificationError(f"{source}: {exc}") from exc
    except (ValueError, TypeError) as exc:
        raise QualificationError(f"{source}: invalid JSON: {exc}") from exc


def load_json_file(path):
    """Read and strictly parse one JSON file."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise
    return parse_json_text(text, str(path))


def _reject_non_finite(value, field):
    if isinstance(value, float) and not math.isfinite(value):
        raise QualificationError(f"{field} contains a non-finite number")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_non_finite(item, f"{field}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_non_finite(item, f"{field}[{index}]")


# ---------------------------------------------------------------------------
# Validation helpers


def _non_empty_string(value, field):
    if not isinstance(value, str) or not value:
        raise QualificationError(f"{field} must be a non-empty string")
    return value


def _positive_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QualificationError(f"{field} must be a positive integer")
    return value


def parse_timestamp(value, field):
    """Parse an offset-aware ISO-8601 timestamp."""
    if not isinstance(value, str):
        raise QualificationError(
            f"{field} must be an ISO-8601 timestamp with a UTC offset"
        )
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise QualificationError(
            f"{field} must be a valid ISO-8601 timestamp with a UTC offset"
        ) from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise QualificationError(
            f"{field} must be an ISO-8601 timestamp with a UTC offset"
        )
    return result


def parse_as_of(value):
    """Return ``(instant, original_text)`` for a review instant."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise QualificationError(
                "qualification_as_of must be an ISO-8601 timestamp with a UTC offset"
            )
        return value, value.isoformat()
    return parse_timestamp(value, "qualification_as_of"), value


def _require_exact_fields(mapping, expected, context):
    missing = sorted(expected - set(mapping))
    if missing:
        raise QualificationError(
            f"{context} is missing required field(s) {', '.join(missing)}"
        )
    extra = sorted(set(mapping) - expected)
    if extra:
        raise QualificationError(
            f"{context} has unknown field(s) {', '.join(extra)}"
        )


# ---------------------------------------------------------------------------
# Document validation


def validate_qualification(document, vendor_ids):
    """Validate a qualification document and group revisions by credential.

    Returns ``{credential_id: [record, ...]}`` where each record is a dict
    carrying parsed datetimes plus the original timestamp text.  Revisions are
    ordered ascending by ``revision``.
    """
    if not isinstance(document, dict):
        raise QualificationError("qualification file must be a JSON object")
    _reject_non_finite(document, "qualification")

    missing = sorted(_DOCUMENT_FIELDS - set(document))
    if missing:
        raise QualificationError(
            "qualification file is missing required field(s) "
            + ", ".join(missing)
        )
    extra = sorted(set(document) - _DOCUMENT_FIELDS)
    if extra:
        raise QualificationError(
            "qualification file has unknown field(s) " + ", ".join(extra)
        )

    schema = document["schema_version"]
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA_VERSION:
        raise QualificationError(
            f"unknown qualification schema_version {schema!r}"
        )

    credentials = document["credentials"]
    if not isinstance(credentials, list):
        raise QualificationError("qualification credentials must be a list")

    vendor_ids = set(vendor_ids)
    grouped = {}
    for index, record in enumerate(credentials):
        index_context = f"qualification credential at index {index}"
        if not isinstance(record, dict):
            raise QualificationError(f"{index_context} must be an object")
        if "credential_id" not in record:
            raise QualificationError(f"{index_context} is missing credential_id")
        credential_id = _non_empty_string(
            record["credential_id"], f"{index_context} credential_id"
        )
        if "revision" not in record:
            raise QualificationError(
                f"qualification credential {credential_id!r} is missing revision"
            )
        revision = _positive_int(
            record["revision"],
            f"qualification credential {credential_id!r} revision",
        )
        context = f"qualification credential {credential_id!r} revision {revision}"

        _require_exact_fields(record, _RECORD_FIELDS, context)

        vendor_id = _non_empty_string(record["vendor_id"], f"{context} vendor_id")
        if vendor_id not in vendor_ids:
            raise QualificationError(
                f"{context} vendor_id {vendor_id!r} is not a declared vendor"
            )
        document_code = _non_empty_string(
            record["document_code"], f"{context} document_code"
        )
        status = record["status"]
        if not isinstance(status, str) or status not in STATUSES:
            raise QualificationError(
                f"{context} status {status!r} is unknown; expected "
                "active, suspended, or revoked"
            )
        valid_from_text = record["valid_from"]
        valid_until_text = record["valid_until"]
        recorded_at_text = record["recorded_at"]
        valid_from = parse_timestamp(valid_from_text, f"{context} valid_from")
        valid_until = parse_timestamp(valid_until_text, f"{context} valid_until")
        recorded_at = parse_timestamp(recorded_at_text, f"{context} recorded_at")
        if not valid_from < valid_until:
            raise QualificationError(
                f"{context} valid_from must be before valid_until"
            )

        grouped.setdefault(credential_id, []).append(
            {
                "credential_id": credential_id,
                "revision": revision,
                "vendor_id": vendor_id,
                "document_code": document_code,
                "status": status,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "recorded_at": recorded_at,
                "valid_from_text": valid_from_text,
                "valid_until_text": valid_until_text,
                "recorded_at_text": recorded_at_text,
            }
        )

    for credential_id, records in grouped.items():
        records.sort(key=lambda item: item["revision"])
        seen = set()
        for record in records:
            revision = record["revision"]
            if revision in seen:
                raise QualificationError(
                    f"duplicate revision {revision} for credential {credential_id!r}"
                )
            seen.add(revision)

        expected = 1
        for record in records:
            revision = record["revision"]
            if revision != expected:
                if expected == 1:
                    raise QualificationError(
                        f"qualification credential {credential_id!r} revision "
                        f"history must start at revision 1, found revision "
                        f"{revision}"
                    )
                raise QualificationError(
                    f"qualification credential {credential_id!r} is missing "
                    f"revision {expected} before revision {revision}"
                )
            expected += 1

        first = records[0]
        previous = None
        for record in records:
            revision = record["revision"]
            if record["vendor_id"] != first["vendor_id"]:
                raise QualificationError(
                    f"qualification credential {credential_id!r} revision "
                    f"{revision} changes vendor_id from "
                    f"{first['vendor_id']!r} to {record['vendor_id']!r}"
                )
            if record["document_code"] != first["document_code"]:
                raise QualificationError(
                    f"qualification credential {credential_id!r} revision "
                    f"{revision} changes document_code from "
                    f"{first['document_code']!r} to "
                    f"{record['document_code']!r}"
                )
            if previous is not None and not (
                record["recorded_at"] > previous["recorded_at"]
            ):
                raise QualificationError(
                    f"qualification credential {credential_id!r} revision "
                    f"{revision} recorded_at must be later than revision "
                    f"{previous['revision']}"
                )
            previous = record

    return grouped


# ---------------------------------------------------------------------------
# Visibility at the review instant


def visible_credentials(grouped, as_of):
    """Build ``{(vendor_id, document_code): [credential version, ...]}``.

    For each credential only the highest revision with
    ``recorded_at <= as_of`` is visible.  Revisions recorded after the review
    instant are wholly ignored.  A suspended or revoked visible revision never
    falls back to an earlier active revision.
    """
    index = {}
    for credential_id in sorted(grouped):
        visible = None
        for record in grouped[credential_id]:
            if record["recorded_at"] <= as_of:
                visible = record
            else:
                # Revisions are ordered by revision and recorded_at strictly
                # increases, so every later revision is also after as_of.
                break
        if visible is None:
            continue
        key = (visible["vendor_id"], visible["document_code"])
        index.setdefault(key, []).append(visible)

    for candidates in index.values():
        candidates.sort(key=lambda item: item["credential_id"])
    return index


def verify_document(index, vendor_id, document_code, submitted):
    """Check whether a credential proves one code for one offer.

    Returns ``(credential, reason)``.  On success ``reason`` is ``None`` and
    ``credential`` is the selected version: the lexicographically smallest
    ``credential_id`` among candidates that are active and whose half-open
    validity interval contains ``submitted``.  On failure ``credential`` is
    ``None`` and ``reason`` explains the miss; an unverified code is never
    attributed to a credential.
    """
    candidates = index.get((vendor_id, document_code), [])
    if not candidates:
        return None, REASON_NO_CREDENTIAL

    # Candidates are already ordered by credential_id, so the first match is
    # the stable, lexicographically smallest selection.
    for credential in candidates:
        if (
            credential["status"] == "active"
            and credential["valid_from"] <= submitted < credential["valid_until"]
        ):
            return credential, None

    # No active version proves the code at submission.  A suspended or revoked
    # version covering the instant is the directly observable cause; an active
    # version that merely misses the interval is the other cause.
    for credential in candidates:
        if (
            credential["status"] != "active"
            and credential["valid_from"] <= submitted < credential["valid_until"]
        ):
            return None, "credential_" + credential["status"]
    if any(credential["status"] == "active" for credential in candidates):
        return None, REASON_OUTSIDE_VALIDITY
    return None, "credential_" + candidates[0]["status"]


def evidence_entry(document_code, credential, reason):
    """Build one traceable ``qualification_evidence`` entry.

    A verified code names the selected credential version, its revision, and
    the time that revision was recorded.  An unverified code carries only the
    failure reason so it can never be mistaken for proof.
    """
    if reason is None:
        return {
            "document_code": document_code,
            "verified": True,
            "credential_id": credential["credential_id"],
            "revision": credential["revision"],
            "recorded_at": credential["recorded_at_text"],
        }
    return {"document_code": document_code, "verified": False, "reason": reason}
