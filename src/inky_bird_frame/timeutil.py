"""Shared timestamp parsing for schema-versioned state files."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_utc_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp, requiring a timezone; None when invalid."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)
