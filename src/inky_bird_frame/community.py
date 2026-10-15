"""An explicit, bounded community-history preview, separate from personal history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import cast
from urllib.parse import urlencode
from uuid import uuid4

from .birds import DateRange, parse_inaturalist_species_counts
from .catalog import catalog_state_lock
from .errors import DataSourceError
from .http import get_json, read_private_json, write_bytes_atomic
from .journal import (
    MAX_JOURNAL_BYTES,
    CollectionKind,
    JournalCollection,
    JournalConflictError,
    JournalError,
    JournalMember,
    MemberOrigin,
    parse_collection,
    read_journal,
    save_imported_collection,
)

# The upstream species-count endpoint accepts at most 500 results per page.
# Refuse larger selections rather than silently save its first page as complete.
MAX_COMMUNITY_SPECIES = 500


class CommunityHistory:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / "community-preview.json"

    def status(self) -> dict[str, object]:
        with catalog_state_lock(self.state_dir):
            if not self.path.exists():
                return {"status": "empty"}
            try:
                raw = read_private_json(self.path, maximum=MAX_JOURNAL_BYTES)
                if not isinstance(raw, dict) or set(raw) != {"expires_at", "preview"}:
                    raise ValueError("shape")
                expires = datetime.fromisoformat(cast(str, raw["expires_at"]))
                preview = raw["preview"]
                if expires.tzinfo is None or not isinstance(preview, dict):
                    raise ValueError("shape")
                if (
                    set(preview) != {"revision", "collection", "preview_hash", "notice"}
                    or type(preview["revision"]) is not int
                ):
                    raise ValueError("shape")
                group = parse_collection(
                    preview["collection"], maximum_revision=preview["revision"] + 1
                )
                if group.kind != CollectionKind.COMMUNITY or any(
                    member.origin != MemberOrigin.COMMUNITY for member in group.members
                ):
                    raise ValueError("origin")
                digest = hashlib.sha256(
                    json.dumps(asdict(group), sort_keys=True).encode()
                ).hexdigest()
                if preview["preview_hash"] != digest:
                    raise ValueError("changed")
                if expires <= datetime.now(UTC):
                    self.path.unlink()
                    return {"status": "empty"}
                return {"status": "ready", **preview}
            except (DataSourceError, ValueError, TypeError) as exc:
                raise JournalError(
                    "Community preview cannot be read. Cancel it and search again."
                ) from exc

    def cancel(self) -> None:
        with catalog_state_lock(self.state_dir):
            self.path.unlink(missing_ok=True)

    def preview(self, choices: dict[str, object]) -> dict[str, object]:
        if set(choices) != {
            "label",
            "start_date",
            "end_date",
            "latitude",
            "longitude",
            "radius_km",
        }:
            raise JournalError("Choose a name, both dates and a point with radius")
        label = choices["label"]
        if (
            not isinstance(label, str)
            or not 1 <= len(label.strip()) <= 128
            or any(ord(c) < 32 for c in label)
        ):
            raise JournalError("Use a collection name with 1–128 visible characters")
        try:
            start, end = (
                date.fromisoformat(cast(str, choices[key])) for key in ("start_date", "end_date")
            )
            dates = DateRange(start, end)
            lat, lon = (float(cast(str, choices[key])) for key in ("latitude", "longitude"))
            radius = int(cast(str, choices["radius_km"]))
            if (
                not all(isfinite(value) for value in (lat, lon))
                or not -90 <= lat <= 90
                or not -180 <= lon <= 180
                or not 1 <= radius <= 500
            ):
                raise ValueError("point")
        except (ValueError, TypeError, OverflowError) as exc:
            raise JournalError(
                "Use valid inclusive dates, coordinates and a whole radius from 1 to 500 km"
            ) from exc
        params = urlencode(
            {
                "taxon_id": 3,
                "rank": "species",
                "verifiable": "true",
                "photos": "true",
                "lat": f"{lat:.6f}",
                "lng": f"{lon:.6f}",
                "radius": radius,
                "per_page": MAX_COMMUNITY_SPECIES,
                **dates.as_query_params(),
            }
        )
        try:
            raw = get_json(
                "https://api.inaturalist.org/v1/observations/species_counts?" + params,
                error_label="Community history",
            )
            if not isinstance(raw, dict) or type(raw.get("total_results")) is not int:
                raise DataSourceError("Incomplete results")
            total = cast(int, raw["total_results"])
            if total > MAX_COMMUNITY_SPECIES:
                raise JournalError(
                    "This search has more than 500 species. "
                    "Narrow its dates or radius; nothing was saved."
                )
            birds = parse_inaturalist_species_counts(raw)
            if total < 0 or len(birds) != total:
                raise DataSourceError("Incomplete species identities")
        except DataSourceError as exc:
            raise JournalError(
                "Community search did not return a complete usable result. "
                "Retry without changing your collections."
            ) from exc
        with catalog_state_lock(self.state_dir):
            revision = read_journal(self.state_dir).revision
            group = JournalCollection(
                uuid4().hex,
                label.strip(),
                tuple(
                    JournalMember(
                        bird.taxon_id,
                        bird.common_name,
                        bird.scientific_name,
                        MemberOrigin.COMMUNITY,
                    )
                    for bird in birds
                ),
                CollectionKind.COMMUNITY,
                start.isoformat(),
                end.isoformat(),
                revision + 1,
                revision + 1,
            )
            group = parse_collection(asdict(group), maximum_revision=revision + 1)
            document = asdict(group)
            digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
            preview = {
                "revision": revision,
                "collection": document,
                "preview_hash": digest,
                "notice": "Birds reported near this place. These are community reports, "
                "not your personal sightings. Saving requests no generation "
                "and changes no wall selection.",
            }
            encoded = json.dumps(
                {
                    "expires_at": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
                    "preview": preview,
                }
            ).encode()
            if len(encoded) > MAX_JOURNAL_BYTES:
                raise JournalError(
                    "This complete selection exceeds private preview storage. Narrow the search."
                )
            write_bytes_atomic(self.path, encoded, mode=0o600)
        return {
            "message": "Community reports checked. Review the complete selection before saving."
        }

    def save(self, preview_hash: str) -> dict[str, object]:
        preview = self.status()
        if preview.get("status") != "ready" or preview.get("preview_hash") != preview_hash:
            raise JournalConflictError("This community preview changed or expired. Search again.")
        group = parse_collection(
            preview["collection"], maximum_revision=cast(int, preview["revision"]) + 1
        )
        save_imported_collection(self.state_dir, revision=preview["revision"], collection=group)
        return {
            "collection_id": group.collection_id,
            "message": "Community collection saved. No plate requests or wall changes.",
        }
