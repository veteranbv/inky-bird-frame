"""Read-only Bird Buddy authentication, postcard ingestion, and private history."""

from __future__ import annotations

import fcntl
import json
import stat
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from .birds import BirdBuddySpecies, ObservationWindow
from .errors import DataSourceError
from .http import post_json, write_json_atomic
from .timeutil import parse_utc_timestamp

BIRDBUDDY_GRAPHQL_URL: Final = "https://graphql.app-api.prod.aws.mybirdbuddy.com/graphql"
BIRDBUDDY_PAGE_SIZE: Final = 50
# A seven-day feed would need more than 700 visits per day to reach this guard.
BIRDBUDDY_MAX_FEED_PAGES: Final = 100
# Confirmed history is normally far smaller than this 5,000-record bound. The
# cap prevents an undocumented pagination change from creating an unbounded
# controller cycle while still failing visibly instead of truncating history.
BIRDBUDDY_MAX_CONFIRMED_PAGES: Final = 100
BIRDBUDDY_HISTORY_DAYS: Final = 366
AUTH_SCHEMA_VERSION: Final = 1
HISTORY_SCHEMA_VERSION: Final = 1
AUTHORIZED_ACCESS_ATTESTATION: Final = (
    "I confirm that Bird Buddy has authorized automated API access for this account."
)

_SIGN_IN = """
mutation InkyBirdBuddyEmailSignIn($input: EmailSignInInput!) {
  authEmailSignIn(emailSignInInput: $input) {
    __typename
    ... on Auth {
      accessToken
      refreshToken
      me {
        feeders {
          __typename
          ... on FeederForMember { id name }
          ... on FeederForOwner { id name }
        }
      }
    }
  }
}
""".strip()

_REFRESH_TOKEN = """
mutation InkyBirdBuddyRefreshToken($input: RefreshTokenInput!) {
  authRefreshToken(refreshTokenInput: $input) {
    accessToken
    refreshToken
  }
}
""".strip()

_FEED_RECOGNIZED = """
query InkyBirdBuddyFeed($first: Int!, $after: String) {
  me {
    feed(first: $first, after: $after, filter: {feedItemTypes: [NEW_POSTCARD]}) {
      edges {
        cursor
        node {
          __typename
          ... on FeedItemNewPostcard {
            id
            createdAt
            inferenceConfidenceLevel
            feeder {
              __typename
              ... on FeederForMember { id }
              ... on FeederForOwner { id }
              ... on FeederForPublic { id }
              ... on FeederForRemoteGuest { id }
            }
            medias { __typename id }
            sightingReportPreview {
              sightings {
                __typename
                ... on SightingRecognizedBird {
                  species {
                    __typename
                    ... on SpeciesBird { id name scientificName }
                  }
                }
              }
            }
          }
        }
      }
      pageInfo { endCursor hasNextPage }
    }
  }
}
""".strip()

_FEED_UNLOCKED = """
query InkyBirdBuddyFeed($first: Int!, $after: String) {
  me {
    feed(first: $first, after: $after, filter: {feedItemTypes: [NEW_POSTCARD]}) {
      edges {
        cursor
        node {
          __typename
          ... on FeedItemNewPostcard {
            id
            createdAt
            inferenceConfidenceLevel
            feeder {
              __typename
              ... on FeederForMember { id }
              ... on FeederForOwner { id }
              ... on FeederForPublic { id }
              ... on FeederForRemoteGuest { id }
            }
            medias { __typename id }
            sightingReportPreview {
              sightings {
                __typename
                ... on SightingRecognizedBirdUnlocked {
                  species {
                    __typename
                    ... on SpeciesBird { id name scientificName }
                  }
                }
              }
            }
          }
        }
      }
      pageInfo { endCursor hasNextPage }
    }
  }
}
""".strip()

_FEED_QUERIES: Final = (_FEED_RECOGNIZED, _FEED_UNLOCKED)
_FEED_SIGHTING_TYPES: Final = (
    "SightingRecognizedBird",
    "SightingRecognizedBirdUnlocked",
)

_CONFIRMED_HISTORY = """
query InkyBirdBuddyConfirmedHistory($first: Int!, $after: String) {
  me {
    mediasOwned(first: $first, after: $after) {
      edges {
        node {
          origin
          feeder {
            __typename
            ... on FeederForMember { id }
            ... on FeederForOwner { id }
            ... on FeederForPublic { id }
            ... on FeederForRemoteGuest { id }
          }
          media { __typename id createdAt }
          species {
            __typename
            ... on SpeciesBird { id name scientificName }
          }
        }
      }
      pageInfo { endCursor hasNextPage }
    }
  }
}
""".strip()

_CONFIRMED_FEEDER_SOURCE: Final = "selected_feeder"
_CONFIRMED_MANUAL_SOURCE: Final = "manual"
_CONFIRMED_SOURCES: Final = {
    _CONFIRMED_FEEDER_SOURCE,
    _CONFIRMED_MANUAL_SOURCE,
}


@dataclass(frozen=True)
class BirdBuddyFeeder:
    feeder_id: str
    name: str
    role: str


@dataclass(frozen=True)
class BirdBuddyAuthState:
    authorization_confirmed_at: str
    feeder: BirdBuddyFeeder
    refresh_token: str


@dataclass(frozen=True)
class PostcardSpecies:
    species_id: str
    common_name: str
    scientific_name: str


@dataclass(frozen=True)
class BirdBuddyPostcard:
    postcard_id: str
    observed_at: str
    species: tuple[PostcardSpecies, ...]
    media_ids: tuple[str, ...] = ()
    complete: bool = True


@dataclass(frozen=True)
class ConfirmedSpeciesEvidence:
    species: PostcardSpecies
    observed_at: str
    source: str


@dataclass(frozen=True)
class _ConfirmedMediaRecord:
    media_id: str
    observed_at: str
    source: str
    species: tuple[PostcardSpecies, ...]


@dataclass(frozen=True)
class ArchivedSpecies:
    species_id: str
    common_name: str
    scientific_name: str
    detection_count: int
    latest_detection_at: str


@dataclass(frozen=True)
class ArchivedPostcardLink:
    observed_at: str
    media_ids: tuple[str, ...]
    species_ids: tuple[str, ...]


@dataclass
class FeederHistory:
    history_started_at: str
    earliest_initial_feed_at: str | None
    last_successful_sync_at: str | None
    postcards: dict[str, BirdBuddyPostcard]
    archived_species: dict[str, ArchivedSpecies]
    confirmed_species: dict[tuple[str, str], ConfirmedSpeciesEvidence]
    archived_postcards: dict[tuple[str, ...], ArchivedPostcardLink] = field(default_factory=dict)
    archived_unlinked_latest: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BirdBuddySyncStats:
    pages: int
    postcards_processed: int
    accepted_detections: int
    ignored_postcards: int
    duplicate_postcards: int
    reclassified_postcards: int
    history_started_at: str
    earliest_initial_feed_at: str | None
    last_successful_sync_at: str
    confirmed_pages: int = 0
    confirmed_records_processed: int = 0
    confirmed_records_accepted: int = 0
    manual_records_accepted: int = 0
    confirmed_records_ignored: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "pages": self.pages,
            "postcards_processed": self.postcards_processed,
            "accepted_detections": self.accepted_detections,
            "ignored_postcards": self.ignored_postcards,
            "duplicate_postcards": self.duplicate_postcards,
            "reclassified_postcards": self.reclassified_postcards,
            "history_started_at": self.history_started_at,
            "earliest_initial_feed_at": self.earliest_initial_feed_at,
            "last_successful_sync_at": self.last_successful_sync_at,
            "confirmed_pages": self.confirmed_pages,
            "confirmed_records_processed": self.confirmed_records_processed,
            "confirmed_records_accepted": self.confirmed_records_accepted,
            "manual_records_accepted": self.manual_records_accepted,
            "confirmed_records_ignored": self.confirmed_records_ignored,
        }


@dataclass(frozen=True)
class BirdBuddySyncResult:
    species: list[BirdBuddySpecies]
    stats: BirdBuddySyncStats


@dataclass(frozen=True)
class _HistoryUpdate:
    history: FeederHistory
    duplicate_postcards: int
    reclassified_postcards: int


@dataclass(frozen=True)
class _ConfirmedHistoryFetch:
    evidence: list[ConfirmedSpeciesEvidence]
    pages: int
    records_processed: int
    records_accepted: int
    manual_records_accepted: int
    records_ignored: int
    species_by_media: dict[str, tuple[PostcardSpecies, ...]] = field(default_factory=dict)


def _auth_path(state_dir: Path) -> Path:
    return state_dir / "birdbuddy-auth.json"


def _history_path(state_dir: Path) -> Path:
    return state_dir / "birdbuddy-detections.json"


@contextmanager
def _auth_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "birdbuddy-auth.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _history_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "birdbuddy-detections.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _require_private_file(path: Path, label: str) -> None:
    if path.is_symlink():
        raise DataSourceError(f"Refusing symlinked {label} state: {path}")
    permissions = stat.S_IMODE(path.stat().st_mode)
    if permissions & 0o077:
        raise DataSourceError(f"{label} state must use mode 0600: {path}")


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    _require_private_file(path, label)
    try:
        value: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DataSourceError(f"Invalid {label} state: {path}") from exc
    if not isinstance(value, dict):
        raise DataSourceError(f"Invalid {label} state: {path}")
    return value


def _nonempty_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_feeder(value: object) -> BirdBuddyFeeder | None:
    if not isinstance(value, dict):
        return None
    feeder_id = _nonempty_string(value.get("id"))
    name = _nonempty_string(value.get("name"))
    typename = _nonempty_string(value.get("__typename"))
    if (
        feeder_id is None
        or name is None
        or typename
        not in {
            "FeederForMember",
            "FeederForOwner",
        }
    ):
        return None
    return BirdBuddyFeeder(
        feeder_id=feeder_id,
        name=name,
        role="member" if typename == "FeederForMember" else "owner",
    )


def _graphql_data(payload: object, operation: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise DataSourceError(f"Bird Buddy {operation} response was not an object")
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        raise DataSourceError(f"Bird Buddy {operation} failed")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise DataSourceError(f"Bird Buddy {operation} response did not include data")
    return data


def _graphql_request(
    query: str,
    variables: dict[str, object],
    operation: str,
    *,
    access_token: str | None = None,
) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else None
    payload = post_json(
        BIRDBUDDY_GRAPHQL_URL,
        {"query": query, "variables": variables},
        headers=headers,
        error_label="Bird Buddy API",
    )
    return _graphql_data(payload, operation)


def _authenticate(email: str, password: str) -> tuple[str, str, list[BirdBuddyFeeder]]:
    data = _graphql_request(
        _SIGN_IN,
        {"input": {"email": email, "password": password}},
        "authentication",
    )
    result = data.get("authEmailSignIn")
    if not isinstance(result, dict) or result.get("__typename") != "Auth":
        raise DataSourceError("Bird Buddy authentication failed")
    access_token = _nonempty_string(result.get("accessToken"))
    refresh_token = _nonempty_string(result.get("refreshToken"))
    me = result.get("me")
    feeder_values = me.get("feeders") if isinstance(me, dict) else None
    if access_token is None or refresh_token is None or not isinstance(feeder_values, list):
        raise DataSourceError("Bird Buddy authentication response was incomplete")
    feeders = [feeder for value in feeder_values if (feeder := _parse_feeder(value)) is not None]
    if not feeders:
        raise DataSourceError("Bird Buddy account has no accessible feeders")
    return access_token, refresh_token, feeders


def _select_feeder(feeders: list[BirdBuddyFeeder], feeder_id: str | None) -> BirdBuddyFeeder:
    if feeder_id is None:
        if len(feeders) == 1:
            return feeders[0]
        raise DataSourceError(
            "Bird Buddy account has multiple feeders; rerun login with --feeder-id using one of: "
            + ", ".join(f"{feeder.name} ({feeder.feeder_id})" for feeder in feeders)
        )
    selected = next((feeder for feeder in feeders if feeder.feeder_id == feeder_id), None)
    if selected is None:
        raise DataSourceError("Selected Bird Buddy feeder is not accessible to this account")
    return selected


def _auth_payload(state: BirdBuddyAuthState) -> dict[str, object]:
    return {
        "schema_version": AUTH_SCHEMA_VERSION,
        "authorization_confirmed_at": state.authorization_confirmed_at,
        "feeder": {
            "id": state.feeder.feeder_id,
            "name": state.feeder.name,
            "role": state.feeder.role,
        },
        "refresh_token": state.refresh_token,
    }


def _read_auth_state(state_dir: Path) -> BirdBuddyAuthState:
    path = _auth_path(state_dir)
    if path.is_symlink():
        raise DataSourceError(f"Refusing symlinked Bird Buddy authentication state: {path}")
    if not path.exists():
        raise DataSourceError("Bird Buddy is not authenticated; run birdbuddy login")
    value = _read_json_object(path, "Bird Buddy authentication")
    if value.get("schema_version") != AUTH_SCHEMA_VERSION:
        raise DataSourceError("Unsupported Bird Buddy authentication state schema")
    authorization_confirmed_at = _nonempty_string(value.get("authorization_confirmed_at"))
    refresh_token = _nonempty_string(value.get("refresh_token"))
    feeder_value = value.get("feeder")
    if isinstance(feeder_value, dict):
        feeder = BirdBuddyFeeder(
            feeder_id=_nonempty_string(feeder_value.get("id")) or "",
            name=_nonempty_string(feeder_value.get("name")) or "",
            role=_nonempty_string(feeder_value.get("role")) or "",
        )
    else:
        feeder = BirdBuddyFeeder("", "", "")
    if (
        authorization_confirmed_at is None
        or parse_utc_timestamp(authorization_confirmed_at) is None
        or refresh_token is None
        or not feeder.feeder_id
        or not feeder.name
        or feeder.role not in {"member", "owner"}
    ):
        raise DataSourceError("Invalid Bird Buddy authentication state")
    return BirdBuddyAuthState(authorization_confirmed_at, feeder, refresh_token)


def login_birdbuddy(
    state_dir: Path,
    *,
    email: str,
    password: str,
    authorization_confirmed: bool,
    feeder_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    if not authorization_confirmed:
        raise DataSourceError("Bird Buddy authorized-access confirmation is required")
    if not email.strip() or not password:
        raise DataSourceError("Bird Buddy email and password are required")
    _, refresh_token, feeders = _authenticate(email.strip(), password)
    feeder = _select_feeder(feeders, feeder_id)
    confirmed_at = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0).isoformat()
    state = BirdBuddyAuthState(confirmed_at, feeder, refresh_token)
    with _auth_lock(state_dir):
        write_json_atomic(_auth_path(state_dir), _auth_payload(state), mode=0o600)
    return {
        "authenticated": True,
        "authorization_confirmed_at": confirmed_at,
        "feeder": {
            "id": feeder.feeder_id,
            "name": feeder.name,
            "role": feeder.role,
        },
        "least_privilege_guest": feeder.role == "member",
    }


def logout_birdbuddy(state_dir: Path) -> dict[str, object]:
    with _auth_lock(state_dir):
        path = _auth_path(state_dir)
        if path.is_symlink():
            raise DataSourceError(f"Refusing symlinked Bird Buddy authentication state: {path}")
        removed = path.exists()
        if removed:
            _require_private_file(path, "Bird Buddy authentication")
            path.unlink()
    return {
        "authenticated": False,
        "local_authentication_removed": removed,
        "history_preserved": _history_path(state_dir).exists(),
        "remote_token_revoked": False,
    }


def _refresh_access_token(refresh_token: str) -> tuple[str, str]:
    try:
        data = _graphql_request(
            _REFRESH_TOKEN,
            {"input": {"token": refresh_token}},
            "token refresh",
        )
    except DataSourceError as exc:
        if str(exc) in {
            "Bird Buddy token refresh failed",
            "HTTP 401 from Bird Buddy API",
            "HTTP 403 from Bird Buddy API",
        }:
            raise DataSourceError(
                "Bird Buddy session is invalid; run birdbuddy login again"
            ) from exc
        raise
    tokens = data.get("authRefreshToken")
    if not isinstance(tokens, dict):
        raise DataSourceError("Bird Buddy session is invalid; run birdbuddy login again")
    access_token = _nonempty_string(tokens.get("accessToken"))
    replacement = _nonempty_string(tokens.get("refreshToken"))
    if access_token is None or replacement is None:
        raise DataSourceError("Bird Buddy session is invalid; run birdbuddy login again")
    return access_token, replacement


def _refreshed_access_token(state_dir: Path) -> tuple[str, BirdBuddyFeeder]:
    with _auth_lock(state_dir):
        state = _read_auth_state(state_dir)
        access_token, replacement = _refresh_access_token(state.refresh_token)
        write_json_atomic(
            _auth_path(state_dir),
            _auth_payload(
                BirdBuddyAuthState(
                    state.authorization_confirmed_at,
                    state.feeder,
                    replacement,
                )
            ),
            mode=0o600,
        )
    return access_token, state.feeder


def _feeder_id(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    return _nonempty_string(value.get("id"))


def _parse_postcard(
    value: object,
    selected_feeder_id: str,
    selected_sighting_type: str | None = None,
) -> BirdBuddyPostcard | None:
    if not isinstance(value, dict) or value.get("__typename") != "FeedItemNewPostcard":
        return None
    postcard_id = _nonempty_string(value.get("id"))
    observed = parse_utc_timestamp(value.get("createdAt"))
    if (
        postcard_id is None
        or observed is None
        or _feeder_id(value.get("feeder")) != selected_feeder_id
    ):
        return None
    observed_at = observed.replace(microsecond=0).isoformat()
    media_values = value.get("medias")
    media_ids: set[str] = set()
    media_complete = isinstance(media_values, list)
    if isinstance(media_values, list):
        for media in media_values:
            media_id = _nonempty_string(media.get("id")) if isinstance(media, dict) else None
            if media_id is None:
                media_complete = False
                continue
            media_ids.add(media_id)
        if not media_ids:
            media_complete = False
    sorted_media_ids = tuple(sorted(media_ids))
    preview = value.get("sightingReportPreview")
    sightings = preview.get("sightings") if isinstance(preview, dict) else None
    if not isinstance(sightings, list):
        # A missing preview is an incomplete response, not an explicit
        # withdrawal. Ignore it so a transient schema/backend response cannot
        # delete a previously retained classification during reconciliation.
        return BirdBuddyPostcard(
            postcard_id,
            observed_at,
            (),
            media_ids=sorted_media_ids,
            complete=False,
        )
    # An empty species tuple is a reconciliation tombstone. It lets a complete
    # feed refresh remove a previously accepted classification without storing
    # low-confidence or non-bird results in detection history.
    if value.get("inferenceConfidenceLevel") != "HIGH_CONFIDENCE":
        return BirdBuddyPostcard(
            postcard_id,
            observed_at,
            (),
            media_ids=sorted_media_ids,
            complete=media_complete,
        )
    species: dict[str, PostcardSpecies] = {}
    incomplete_recognized_sighting = False
    for sighting in sightings:
        if not isinstance(sighting, dict):
            incomplete_recognized_sighting = True
            continue
        typename = sighting.get("__typename")
        if typename not in {
            "SightingRecognizedBird",
            "SightingRecognizedBirdUnlocked",
        }:
            continue
        # Each private-API query can select fields for only one union member.
        # The complementary member therefore has a typename but no species in
        # this response; its own query variant validates it independently.
        if selected_sighting_type is not None and typename != selected_sighting_type:
            continue
        species_value = sighting.get("species")
        if not isinstance(species_value, dict) or species_value.get("__typename") != "SpeciesBird":
            incomplete_recognized_sighting = True
            continue
        species_id = _nonempty_string(species_value.get("id"))
        common_name = _nonempty_string(species_value.get("name"))
        scientific_name = _nonempty_string(species_value.get("scientificName"))
        if species_id is None or common_name is None or scientific_name is None:
            incomplete_recognized_sighting = True
            continue
        species[species_id] = PostcardSpecies(species_id, common_name, scientific_name)
    if not species:
        return BirdBuddyPostcard(
            postcard_id,
            observed_at,
            (),
            media_ids=sorted_media_ids,
            complete=media_complete and not incomplete_recognized_sighting,
        )
    return BirdBuddyPostcard(
        postcard_id,
        observed_at,
        tuple(sorted(species.values(), key=lambda item: item.species_id)),
        media_ids=sorted_media_ids,
        complete=media_complete and not incomplete_recognized_sighting,
    )


def _fetch_postcard_variant(
    access_token: str,
    feeder_id: str,
    query: str,
    sighting_type: str | None = None,
) -> tuple[list[BirdBuddyPostcard], int, int, int]:
    postcards: dict[str, BirdBuddyPostcard] = {}
    after: str | None = None
    seen_cursors: set[str] = set()
    pages = 0
    processed = 0
    ignored = 0
    while True:
        if pages >= BIRDBUDDY_MAX_FEED_PAGES:
            raise DataSourceError("Bird Buddy feed exceeded the safe pagination limit")
        variables: dict[str, object] = {"first": BIRDBUDDY_PAGE_SIZE}
        if after is not None:
            variables["after"] = after
        data = _graphql_request(
            query,
            variables,
            "feed query",
            access_token=access_token,
        )
        me = data.get("me")
        feed = me.get("feed") if isinstance(me, dict) else None
        edges = feed.get("edges") if isinstance(feed, dict) else None
        page_info = feed.get("pageInfo") if isinstance(feed, dict) else None
        if not isinstance(edges, list) or not isinstance(page_info, dict):
            raise DataSourceError("Bird Buddy feed response was incomplete")
        pages += 1
        for edge in edges:
            processed += 1
            node = edge.get("node") if isinstance(edge, dict) else None
            postcard = _parse_postcard(node, feeder_id, sighting_type)
            if postcard is None:
                ignored += 1
            else:
                postcards[postcard.postcard_id] = postcard
                if not postcard.complete or not postcard.species:
                    ignored += 1
        has_next = page_info.get("hasNextPage")
        if has_next is False:
            break
        if has_next is not True:
            raise DataSourceError("Bird Buddy feed response had invalid pagination state")
        cursor = _nonempty_string(page_info.get("endCursor"))
        if cursor is None or cursor in seen_cursors:
            raise DataSourceError("Bird Buddy feed returned a repeated pagination cursor")
        seen_cursors.add(cursor)
        after = cursor
    return list(postcards.values()), pages, processed, ignored


def _merge_postcard_variants(
    postcards: dict[str, BirdBuddyPostcard], incoming: BirdBuddyPostcard
) -> None:
    existing = postcards.get(incoming.postcard_id)
    if existing is None:
        postcards[incoming.postcard_id] = incoming
        return
    if existing.observed_at != incoming.observed_at:
        raise DataSourceError("Bird Buddy returned inconsistent postcard timestamps")
    species = {item.species_id: item for item in existing.species}
    species.update({item.species_id: item for item in incoming.species})
    media_ids_match = existing.media_ids == incoming.media_ids
    postcards[incoming.postcard_id] = BirdBuddyPostcard(
        existing.postcard_id,
        existing.observed_at,
        tuple(sorted(species.values(), key=lambda item: item.species_id)),
        media_ids=tuple(sorted(set(existing.media_ids) | set(incoming.media_ids))),
        complete=existing.complete and incoming.complete and media_ids_match,
    )


def _fetch_postcards(
    access_token: str,
    feeder_id: str,
) -> tuple[list[BirdBuddyPostcard], int, int, int]:
    postcards: dict[str, BirdBuddyPostcard] = {}
    variant_counts: dict[str, int] = {}
    total_pages = 0
    processed_per_variant: list[int] = []
    for query, sighting_type in zip(
        _FEED_QUERIES,
        _FEED_SIGHTING_TYPES,
        strict=True,
    ):
        incoming, pages, processed, _ = _fetch_postcard_variant(
            access_token,
            feeder_id,
            query,
            sighting_type,
        )
        total_pages += pages
        processed_per_variant.append(processed)
        for postcard in incoming:
            _merge_postcard_variants(postcards, postcard)
            variant_counts[postcard.postcard_id] = variant_counts.get(postcard.postcard_id, 0) + 1
    processed = max(processed_per_variant, default=0)
    complete_postcards = [
        postcard
        for postcard in postcards.values()
        if postcard.complete and variant_counts[postcard.postcard_id] == len(_FEED_QUERIES)
    ]
    accepted_postcards = sum(bool(postcard.species) for postcard in complete_postcards)
    ignored = max(0, processed - accepted_postcards)
    return complete_postcards, total_pages, processed, ignored


def _parse_confirmed_evidence(
    value: object,
    selected_feeder_id: str,
    *,
    include_manual_sightings: bool,
) -> _ConfirmedMediaRecord | None:
    if not isinstance(value, dict):
        raise DataSourceError("Bird Buddy confirmed history record was incomplete")
    origin = _nonempty_string(value.get("origin"))
    feeder_id = _feeder_id(value.get("feeder"))
    if origin == "POSTCARD":
        if feeder_id is None:
            raise DataSourceError("Bird Buddy confirmed history record was incomplete")
        if feeder_id != selected_feeder_id:
            return None
        source = _CONFIRMED_FEEDER_SOURCE
    elif origin == "CUSTOM_ID" and include_manual_sightings:
        source = _CONFIRMED_MANUAL_SOURCE
    elif origin in {"CUSTOM_ID", "WATCHING"}:
        return None
    else:
        raise DataSourceError("Bird Buddy confirmed history record had an unsupported origin")
    media = value.get("media")
    created_at = media.get("createdAt") if isinstance(media, dict) else None
    media_id = _nonempty_string(media.get("id")) if isinstance(media, dict) else None
    observed = parse_utc_timestamp(created_at)
    species_values = value.get("species")
    if media_id is None or observed is None or not isinstance(species_values, list):
        raise DataSourceError("Bird Buddy confirmed history record was incomplete")
    observed_at = observed.astimezone(UTC).replace(microsecond=0).isoformat()
    species: dict[str, PostcardSpecies] = {}
    for item in species_values:
        if not isinstance(item, dict):
            raise DataSourceError("Bird Buddy confirmed history record was incomplete")
        if item.get("__typename") != "SpeciesBird":
            continue
        species_id = _nonempty_string(item.get("id"))
        common_name = _nonempty_string(item.get("name"))
        scientific_name = _nonempty_string(item.get("scientificName"))
        if species_id is None or common_name is None or scientific_name is None:
            raise DataSourceError("Bird Buddy confirmed history record was incomplete")
        species[species_id] = PostcardSpecies(species_id, common_name, scientific_name)
    return _ConfirmedMediaRecord(
        media_id,
        observed_at,
        source,
        tuple(sorted(species.values(), key=lambda candidate: candidate.species_id)),
    )


def _fetch_confirmed_history(
    access_token: str,
    feeder_id: str,
    *,
    include_manual_sightings: bool,
) -> _ConfirmedHistoryFetch:
    evidence: dict[tuple[str, str], ConfirmedSpeciesEvidence] = {}
    species_by_media: dict[str, dict[str, PostcardSpecies]] = {}
    after: str | None = None
    seen_cursors: set[str] = set()
    pages = 0
    processed = 0
    accepted_records = 0
    accepted_manual_records = 0
    while True:
        if pages >= BIRDBUDDY_MAX_CONFIRMED_PAGES:
            raise DataSourceError("Bird Buddy confirmed history exceeded the safe pagination limit")
        variables: dict[str, object] = {"first": BIRDBUDDY_PAGE_SIZE}
        if after is not None:
            variables["after"] = after
        data = _graphql_request(
            _CONFIRMED_HISTORY,
            variables,
            "confirmed history query",
            access_token=access_token,
        )
        me = data.get("me")
        connection = me.get("mediasOwned") if isinstance(me, dict) else None
        edges = connection.get("edges") if isinstance(connection, dict) else None
        page_info = connection.get("pageInfo") if isinstance(connection, dict) else None
        if not isinstance(edges, list) or not isinstance(page_info, dict):
            raise DataSourceError("Bird Buddy confirmed history response was incomplete")
        pages += 1
        for edge in edges:
            processed += 1
            node = edge.get("node") if isinstance(edge, dict) else None
            parsed = _parse_confirmed_evidence(
                node,
                feeder_id,
                include_manual_sightings=include_manual_sightings,
            )
            if parsed is None:
                continue
            accepted_records += 1
            if parsed.source == _CONFIRMED_MANUAL_SOURCE:
                accepted_manual_records += 1
            if parsed.source == _CONFIRMED_FEEDER_SOURCE:
                parsed_species = {item.species_id: item for item in parsed.species}
                existing_species = species_by_media.get(parsed.media_id)
                if existing_species is not None and existing_species != parsed_species:
                    raise DataSourceError(
                        "Bird Buddy confirmed history returned conflicting media records"
                    )
                species_by_media[parsed.media_id] = parsed_species
            for species in parsed.species:
                item = ConfirmedSpeciesEvidence(
                    species,
                    parsed.observed_at,
                    parsed.source,
                )
                key = (item.source, item.species.species_id)
                existing = evidence.get(key)
                if (
                    existing is None
                    or _newest_timestamp(existing.observed_at, item.observed_at) == item.observed_at
                ):
                    evidence[key] = item
        has_next = page_info.get("hasNextPage")
        if has_next is False:
            break
        if has_next is not True:
            raise DataSourceError(
                "Bird Buddy confirmed history response had invalid pagination state"
            )
        cursor = _nonempty_string(page_info.get("endCursor"))
        if cursor is None or cursor in seen_cursors:
            raise DataSourceError(
                "Bird Buddy confirmed history returned a repeated pagination cursor"
            )
        seen_cursors.add(cursor)
        after = cursor
    return _ConfirmedHistoryFetch(
        list(evidence.values()),
        pages,
        processed,
        accepted_records,
        accepted_manual_records,
        processed - accepted_records,
        {
            media_id: tuple(sorted(items.values(), key=lambda item: item.species_id))
            for media_id, items in species_by_media.items()
        },
    )


def _parse_postcard_state(value: object) -> BirdBuddyPostcard:
    if not isinstance(value, dict):
        raise DataSourceError("Invalid Bird Buddy detection history")
    postcard_id = _nonempty_string(value.get("id"))
    observed_at = _nonempty_string(value.get("observed_at"))
    species_values = value.get("species")
    media_id_values = value.get("media_ids", [])
    if (
        postcard_id is None
        or observed_at is None
        or parse_utc_timestamp(observed_at) is None
        or not isinstance(species_values, list)
        or not isinstance(media_id_values, list)
    ):
        raise DataSourceError("Invalid Bird Buddy detection history")
    media_ids = tuple(
        media_id
        for item in media_id_values
        for media_id in [_nonempty_string(item)]
        if media_id is not None
    )
    if len(media_ids) != len(media_id_values) or len(set(media_ids)) != len(media_ids):
        raise DataSourceError("Invalid Bird Buddy detection history")
    species: list[PostcardSpecies] = []
    for item in species_values:
        if not isinstance(item, dict):
            raise DataSourceError("Invalid Bird Buddy detection history")
        species_id = _nonempty_string(item.get("id"))
        common_name = _nonempty_string(item.get("common_name"))
        scientific_name = _nonempty_string(item.get("scientific_name"))
        if species_id is None or common_name is None or scientific_name is None:
            raise DataSourceError("Invalid Bird Buddy detection history")
        species.append(PostcardSpecies(species_id, common_name, scientific_name))
    return BirdBuddyPostcard(
        postcard_id,
        observed_at,
        tuple(species),
        media_ids=tuple(sorted(media_ids)),
    )


def _parse_archived_species(value: object, species_id: str) -> ArchivedSpecies:
    if not isinstance(value, dict):
        raise DataSourceError("Invalid Bird Buddy detection history")
    common_name = _nonempty_string(value.get("common_name"))
    scientific_name = _nonempty_string(value.get("scientific_name"))
    latest = _nonempty_string(value.get("latest_detection_at"))
    count = value.get("detection_count")
    if (
        common_name is None
        or scientific_name is None
        or latest is None
        or parse_utc_timestamp(latest) is None
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count <= 0
    ):
        raise DataSourceError("Invalid Bird Buddy detection history")
    return ArchivedSpecies(species_id, common_name, scientific_name, count, latest)


def _parse_archived_postcard(value: object) -> ArchivedPostcardLink:
    if not isinstance(value, dict):
        raise DataSourceError("Invalid Bird Buddy detection history")
    observed_at = _nonempty_string(value.get("observed_at"))
    media_values = value.get("media_ids")
    species_values = value.get("species_ids")
    if (
        observed_at is None
        or parse_utc_timestamp(observed_at) is None
        or not isinstance(media_values, list)
        or not isinstance(species_values, list)
    ):
        raise DataSourceError("Invalid Bird Buddy detection history")
    media_ids = tuple(
        media_id
        for item in media_values
        for media_id in [_nonempty_string(item)]
        if media_id is not None
    )
    species_ids = tuple(
        species_id
        for item in species_values
        for species_id in [_nonempty_string(item)]
        if species_id is not None
    )
    if (
        not media_ids
        or len(media_ids) != len(media_values)
        or len(set(media_ids)) != len(media_ids)
        or len(species_ids) != len(species_values)
        or len(set(species_ids)) != len(species_ids)
    ):
        raise DataSourceError("Invalid Bird Buddy detection history")
    return ArchivedPostcardLink(
        observed_at,
        tuple(sorted(media_ids)),
        tuple(sorted(species_ids)),
    )


def _parse_confirmed_species(value: object) -> ConfirmedSpeciesEvidence:
    if not isinstance(value, dict):
        raise DataSourceError("Invalid Bird Buddy detection history")
    species_id = _nonempty_string(value.get("id"))
    common_name = _nonempty_string(value.get("common_name"))
    scientific_name = _nonempty_string(value.get("scientific_name"))
    observed_at = _nonempty_string(value.get("observed_at"))
    source = _nonempty_string(value.get("source"))
    if (
        species_id is None
        or common_name is None
        or scientific_name is None
        or observed_at is None
        or parse_utc_timestamp(observed_at) is None
        or source not in _CONFIRMED_SOURCES
    ):
        raise DataSourceError("Invalid Bird Buddy detection history")
    return ConfirmedSpeciesEvidence(
        PostcardSpecies(species_id, common_name, scientific_name),
        observed_at,
        source,
    )


def _parse_feeder_history(value: object) -> FeederHistory:
    if not isinstance(value, dict):
        raise DataSourceError("Invalid Bird Buddy detection history")
    history_started_at = _nonempty_string(value.get("history_started_at"))
    earliest = value.get("earliest_initial_feed_at")
    last_sync = value.get("last_successful_sync_at")
    postcard_values = value.get("postcards")
    archived_values = value.get("archived_species")
    archived_postcard_values = value.get("archived_postcards", [])
    archived_unlinked_values = value.get("archived_unlinked_latest")
    confirmed_values = value.get("confirmed_species", [])
    if (
        history_started_at is None
        or parse_utc_timestamp(history_started_at) is None
        or (earliest is not None and parse_utc_timestamp(earliest) is None)
        or (last_sync is not None and parse_utc_timestamp(last_sync) is None)
        or not isinstance(postcard_values, list)
        or not isinstance(archived_values, dict)
        or not isinstance(archived_postcard_values, list)
        or (archived_unlinked_values is not None and not isinstance(archived_unlinked_values, dict))
        or not isinstance(confirmed_values, list)
    ):
        raise DataSourceError("Invalid Bird Buddy detection history")
    parsed_postcards = [_parse_postcard_state(item) for item in postcard_values]
    postcards = {postcard.postcard_id: postcard for postcard in parsed_postcards}
    archived = {
        species_id: _parse_archived_species(item, species_id)
        for species_id, item in archived_values.items()
        if isinstance(species_id, str)
    }
    if len(archived) != len(archived_values):
        raise DataSourceError("Invalid Bird Buddy detection history")
    archived_postcard_items = [_parse_archived_postcard(item) for item in archived_postcard_values]
    archived_postcards = {item.media_ids: item for item in archived_postcard_items}
    if len(archived_postcards) != len(archived_postcard_items):
        raise DataSourceError("Invalid Bird Buddy detection history")
    linked_counts: dict[str, int] = {}
    for item in archived_postcards.values():
        for species_id in item.species_ids:
            linked_counts[species_id] = linked_counts.get(species_id, 0) + 1
    if any(
        species_id not in archived or count > archived[species_id].detection_count
        for species_id, count in linked_counts.items()
    ):
        raise DataSourceError("Invalid Bird Buddy detection history")
    if archived_unlinked_values is None:
        archived_unlinked_latest = {
            species_id: item.latest_detection_at
            for species_id, item in archived.items()
            if linked_counts.get(species_id, 0) < item.detection_count
        }
    else:
        archived_unlinked_latest = {
            species_id: timestamp
            for species_id, value in archived_unlinked_values.items()
            if isinstance(species_id, str)
            for timestamp in [_nonempty_string(value)]
            if timestamp is not None and parse_utc_timestamp(timestamp) is not None
        }
        expected_unlinked = {
            species_id
            for species_id, item in archived.items()
            if linked_counts.get(species_id, 0) < item.detection_count
        }
        if (
            len(archived_unlinked_latest) != len(archived_unlinked_values)
            or set(archived_unlinked_latest) != expected_unlinked
        ):
            raise DataSourceError("Invalid Bird Buddy detection history")
    confirmed_items = [_parse_confirmed_species(item) for item in confirmed_values]
    confirmed = {(item.source, item.species.species_id): item for item in confirmed_items}
    if len(confirmed) != len(confirmed_items):
        raise DataSourceError("Invalid Bird Buddy detection history")
    return FeederHistory(
        history_started_at,
        earliest if isinstance(earliest, str) else None,
        last_sync if isinstance(last_sync, str) else None,
        postcards,
        archived,
        confirmed,
        archived_postcards,
        archived_unlinked_latest,
    )


def _read_history(state_dir: Path) -> dict[str, FeederHistory]:
    path = _history_path(state_dir)
    if path.is_symlink():
        raise DataSourceError(f"Refusing symlinked Bird Buddy detection state: {path}")
    if not path.exists():
        return {}
    value = _read_json_object(path, "Bird Buddy detection")
    if value.get("schema_version") != HISTORY_SCHEMA_VERSION:
        raise DataSourceError("Unsupported Bird Buddy detection history schema")
    feeders = value.get("feeders")
    if not isinstance(feeders, dict):
        raise DataSourceError("Invalid Bird Buddy detection history")
    histories = {
        feeder_id: _parse_feeder_history(history)
        for feeder_id, history in feeders.items()
        if isinstance(feeder_id, str) and feeder_id
    }
    if len(histories) != len(feeders):
        raise DataSourceError("Invalid Bird Buddy detection history")
    return histories


def _postcard_payload(postcard: BirdBuddyPostcard) -> dict[str, object]:
    return {
        "id": postcard.postcard_id,
        "observed_at": postcard.observed_at,
        "media_ids": list(postcard.media_ids),
        "species": [
            {
                "id": species.species_id,
                "common_name": species.common_name,
                "scientific_name": species.scientific_name,
            }
            for species in postcard.species
        ],
    }


def _history_payload(histories: dict[str, FeederHistory]) -> dict[str, object]:
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "feeders": {
            feeder_id: {
                "history_started_at": history.history_started_at,
                "earliest_initial_feed_at": history.earliest_initial_feed_at,
                "last_successful_sync_at": history.last_successful_sync_at,
                "postcards": [
                    _postcard_payload(postcard)
                    for postcard in sorted(
                        history.postcards.values(),
                        key=lambda item: (item.observed_at, item.postcard_id),
                    )
                ],
                "archived_species": {
                    species_id: {
                        "common_name": species.common_name,
                        "scientific_name": species.scientific_name,
                        "detection_count": species.detection_count,
                        "latest_detection_at": species.latest_detection_at,
                    }
                    for species_id, species in sorted(history.archived_species.items())
                },
                "archived_postcards": [
                    {
                        "observed_at": item.observed_at,
                        "media_ids": list(item.media_ids),
                        "species_ids": list(item.species_ids),
                    }
                    for item in sorted(
                        history.archived_postcards.values(),
                        key=lambda candidate: (
                            candidate.observed_at,
                            candidate.media_ids,
                        ),
                    )
                ],
                "archived_unlinked_latest": dict(sorted(history.archived_unlinked_latest.items())),
                "confirmed_species": [
                    {
                        "id": item.species.species_id,
                        "common_name": item.species.common_name,
                        "scientific_name": item.species.scientific_name,
                        "observed_at": item.observed_at,
                        "source": item.source,
                    }
                    for _, item in sorted(history.confirmed_species.items())
                ],
            }
            for feeder_id, history in sorted(histories.items())
        },
    }


def _newest_timestamp(first: str, second: str) -> str:
    first_at = parse_utc_timestamp(first)
    second_at = parse_utc_timestamp(second)
    if first_at is None or second_at is None:
        raise DataSourceError("Invalid Bird Buddy detection timestamp")
    return first if first_at >= second_at else second


def _archive_postcard(
    history: FeederHistory,
    postcard: BirdBuddyPostcard,
    *,
    linked: bool,
) -> None:
    for species in postcard.species:
        existing = history.archived_species.get(species.species_id)
        history.archived_species[species.species_id] = ArchivedSpecies(
            species.species_id,
            species.common_name,
            species.scientific_name,
            (existing.detection_count if existing is not None else 0) + 1,
            (
                _newest_timestamp(existing.latest_detection_at, postcard.observed_at)
                if existing is not None
                else postcard.observed_at
            ),
        )
        if not linked:
            unlinked_latest = history.archived_unlinked_latest.get(species.species_id)
            history.archived_unlinked_latest[species.species_id] = (
                postcard.observed_at
                if unlinked_latest is None
                else _newest_timestamp(unlinked_latest, postcard.observed_at)
            )


def _window_cutoff(window: ObservationWindow, now: datetime) -> datetime | None:
    days = {
        ObservationWindow.LAST_DAY: 1,
        ObservationWindow.LAST_WEEK: 7,
        ObservationWindow.LAST_30_DAYS: 30,
        ObservationWindow.LAST_YEAR: 365,
    }
    return None if window is ObservationWindow.ALL_TIME else now - timedelta(days=days[window])


def _aggregate_species(
    history: FeederHistory,
    window: ObservationWindow,
    now: datetime,
    limit: int,
    *,
    include_manual_sightings: bool,
) -> list[BirdBuddySpecies]:
    counts: dict[str, int] = {}
    identities: dict[str, PostcardSpecies] = {}
    latest: dict[str, str] = {}
    cutoff = _window_cutoff(window, now)
    if cutoff is None:
        for archived in history.archived_species.values():
            counts[archived.species_id] = archived.detection_count
            identities[archived.species_id] = PostcardSpecies(
                archived.species_id, archived.common_name, archived.scientific_name
            )
            latest[archived.species_id] = archived.latest_detection_at
    for postcard in history.postcards.values():
        observed = parse_utc_timestamp(postcard.observed_at)
        if observed is None:
            raise DataSourceError("Invalid Bird Buddy detection timestamp")
        if cutoff is not None and observed < cutoff:
            continue
        for species in postcard.species:
            counts[species.species_id] = counts.get(species.species_id, 0) + 1
            identities[species.species_id] = species
            current_latest = latest.get(species.species_id)
            latest[species.species_id] = (
                postcard.observed_at
                if current_latest is None
                else _newest_timestamp(current_latest, postcard.observed_at)
            )
    for evidence in history.confirmed_species.values():
        if evidence.source == _CONFIRMED_MANUAL_SOURCE and not include_manual_sightings:
            continue
        observed = parse_utc_timestamp(evidence.observed_at)
        if observed is None:
            raise DataSourceError("Invalid Bird Buddy confirmed-history timestamp")
        if cutoff is not None and observed < cutoff:
            continue
        species_id = evidence.species.species_id
        # Confirmed media closes the transient-feed gap, but one postcard can
        # contain several media records. It therefore supplies conservative
        # presence evidence and a newest timestamp, never a fabricated visit
        # count. Exact postcard history remains authoritative when available.
        counts.setdefault(species_id, 1)
        identities[species_id] = evidence.species
        current_latest = latest.get(species_id)
        latest[species_id] = (
            evidence.observed_at
            if current_latest is None
            else _newest_timestamp(current_latest, evidence.observed_at)
        )
    result = [
        BirdBuddySpecies(
            species_id,
            identities[species_id].common_name,
            identities[species_id].scientific_name,
            count,
            latest[species_id],
        )
        for species_id, count in counts.items()
    ]
    result.sort(
        key=lambda item: (-item.detection_count, item.common_name.casefold(), item.species_id)
    )
    return result[:limit]


def _latest_history_detection(
    history: FeederHistory, *, include_manual_sightings: bool
) -> str | None:
    latest: str | None = None
    timestamps = [postcard.observed_at for postcard in history.postcards.values()]
    timestamps.extend(species.latest_detection_at for species in history.archived_species.values())
    timestamps.extend(
        item.observed_at
        for item in history.confirmed_species.values()
        if item.source != _CONFIRMED_MANUAL_SOURCE or include_manual_sightings
    )
    for timestamp in timestamps:
        latest = timestamp if latest is None else _newest_timestamp(latest, timestamp)
    return latest


def _confirmed_species_for_media(
    media_ids: tuple[str, ...],
    species_by_media: dict[str, tuple[PostcardSpecies, ...]],
) -> tuple[PostcardSpecies, ...] | None:
    if not media_ids or any(media_id not in species_by_media for media_id in media_ids):
        return None
    species = {
        item.species_id: item for media_id in media_ids for item in species_by_media[media_id]
    }
    return tuple(sorted(species.values(), key=lambda item: item.species_id))


def _archived_latest_timestamp(history: FeederHistory, species_id: str) -> str:
    candidates = [
        item.observed_at
        for item in history.archived_postcards.values()
        if species_id in item.species_ids
    ]
    unlinked_latest = history.archived_unlinked_latest.get(species_id)
    if unlinked_latest is not None:
        candidates.append(unlinked_latest)
    if not candidates:
        raise DataSourceError("Invalid Bird Buddy archived correction history")
    latest = candidates[0]
    for candidate in candidates[1:]:
        latest = _newest_timestamp(latest, candidate)
    return latest


def _reconcile_archived_postcards(
    history: FeederHistory,
    species_by_media: dict[str, tuple[PostcardSpecies, ...]],
) -> int:
    reclassified = 0
    for media_ids, archived_postcard in list(history.archived_postcards.items()):
        confirmed_species = _confirmed_species_for_media(media_ids, species_by_media)
        if confirmed_species is None:
            for species_id in archived_postcard.species_ids:
                current = history.archived_unlinked_latest.get(species_id)
                history.archived_unlinked_latest[species_id] = (
                    archived_postcard.observed_at
                    if current is None
                    else _newest_timestamp(current, archived_postcard.observed_at)
                )
            del history.archived_postcards[media_ids]
            continue
        confirmed_by_id = {item.species_id: item for item in confirmed_species}
        old_ids = set(archived_postcard.species_ids)
        new_ids = set(confirmed_by_id)
        if old_ids == new_ids:
            continue
        history.archived_postcards[media_ids] = ArchivedPostcardLink(
            archived_postcard.observed_at,
            media_ids,
            tuple(sorted(new_ids)),
        )
        for species_id in old_ids - new_ids:
            archived = history.archived_species.get(species_id)
            if archived is None:
                raise DataSourceError("Invalid Bird Buddy archived correction history")
            if archived.detection_count == 1:
                del history.archived_species[species_id]
                history.archived_unlinked_latest.pop(species_id, None)
            else:
                history.archived_species[species_id] = ArchivedSpecies(
                    archived.species_id,
                    archived.common_name,
                    archived.scientific_name,
                    archived.detection_count - 1,
                    _archived_latest_timestamp(history, species_id),
                )
        for species_id in new_ids:
            species = confirmed_by_id[species_id]
            archived = history.archived_species.get(species_id)
            added = species_id not in old_ids
            if not added and archived is None:
                raise DataSourceError("Invalid Bird Buddy archived correction history")
            history.archived_species[species_id] = ArchivedSpecies(
                species_id,
                species.common_name,
                species.scientific_name,
                (archived.detection_count if archived is not None else 0) + (1 if added else 0),
                (
                    _newest_timestamp(
                        archived.latest_detection_at,
                        archived_postcard.observed_at,
                    )
                    if archived is not None
                    else archived_postcard.observed_at
                ),
            )
        reclassified += 1
    return reclassified


def _update_history(
    state_dir: Path,
    feeder_id: str,
    incoming: list[BirdBuddyPostcard],
    confirmed: list[ConfirmedSpeciesEvidence],
    current: datetime,
    *,
    persist: bool,
    replace_manual_evidence: bool,
    confirmed_species_by_media: dict[str, tuple[PostcardSpecies, ...]],
) -> _HistoryUpdate:
    current_iso = current.isoformat()
    lock = _history_lock(state_dir) if persist else nullcontext()
    with lock:
        histories = _read_history(state_dir)
        history = histories.get(
            feeder_id,
            FeederHistory(current_iso, None, None, {}, {}, {}),
        )
        # History written before media linkage cannot safely reconcile a later
        # confirmation. Migrate only the selected feeder after both remote
        # snapshots succeeded; inactive feeder history must remain untouched.
        for postcard_id, postcard in list(history.postcards.items()):
            if not postcard.media_ids:
                del history.postcards[postcard_id]
        duplicates = 0
        reclassified_postcard_ids: set[str] = set()
        for postcard in incoming:
            existing = history.postcards.get(postcard.postcard_id)
            if not postcard.species:
                if existing is not None:
                    del history.postcards[postcard.postcard_id]
                    reclassified_postcard_ids.add(postcard.postcard_id)
                continue
            if (
                existing is not None
                and existing.observed_at == postcard.observed_at
                and existing.species == postcard.species
            ):
                duplicates += 1
            elif existing is not None:
                reclassified_postcard_ids.add(postcard.postcard_id)
            history.postcards[postcard.postcard_id] = postcard
        for postcard_id, postcard in list(history.postcards.items()):
            confirmed_species = _confirmed_species_for_media(
                postcard.media_ids,
                confirmed_species_by_media,
            )
            if confirmed_species is None or confirmed_species == postcard.species:
                continue
            history.postcards[postcard_id] = BirdBuddyPostcard(
                postcard.postcard_id,
                postcard.observed_at,
                confirmed_species,
                media_ids=postcard.media_ids,
            )
            reclassified_postcard_ids.add(postcard_id)
        archived_reclassifications = _reconcile_archived_postcards(
            history,
            confirmed_species_by_media,
        )
        replaced_sources = {_CONFIRMED_FEEDER_SOURCE}
        if replace_manual_evidence:
            replaced_sources.add(_CONFIRMED_MANUAL_SOURCE)
        for key in list(history.confirmed_species):
            if key[0] in replaced_sources:
                del history.confirmed_species[key]
        for evidence in confirmed:
            key = (evidence.source, evidence.species.species_id)
            existing_evidence = history.confirmed_species.get(key)
            if (
                existing_evidence is None
                or _newest_timestamp(existing_evidence.observed_at, evidence.observed_at)
                == evidence.observed_at
            ):
                history.confirmed_species[key] = evidence
        if history.earliest_initial_feed_at is None and incoming:
            history.earliest_initial_feed_at = min(item.observed_at for item in incoming)
        prune_before = current - timedelta(days=BIRDBUDDY_HISTORY_DAYS)
        for postcard_id, postcard in list(history.postcards.items()):
            observed = parse_utc_timestamp(postcard.observed_at)
            if observed is None:
                raise DataSourceError("Invalid Bird Buddy detection timestamp")
            if observed < prune_before:
                confirmed_species = _confirmed_species_for_media(
                    postcard.media_ids,
                    confirmed_species_by_media,
                )
                linked = bool(postcard.media_ids) and confirmed_species is not None
                _archive_postcard(history, postcard, linked=linked)
                if linked:
                    history.archived_postcards[postcard.media_ids] = ArchivedPostcardLink(
                        postcard.observed_at,
                        postcard.media_ids,
                        tuple(item.species_id for item in postcard.species),
                    )
                del history.postcards[postcard_id]
        history.last_successful_sync_at = current_iso
        histories[feeder_id] = history
        if persist:
            write_json_atomic(_history_path(state_dir), _history_payload(histories), mode=0o600)
    return _HistoryUpdate(
        history,
        duplicates,
        len(reclassified_postcard_ids) + archived_reclassifications,
    )


def sync_birdbuddy_detections(
    state_dir: Path,
    *,
    window: ObservationWindow,
    limit: int,
    now: datetime | None = None,
    persist_history: bool = True,
    include_manual_sightings: bool = False,
) -> BirdBuddySyncResult:
    if limit <= 0:
        raise ValueError("Bird Buddy species limit must be greater than zero")
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    access_token, feeder = _refreshed_access_token(state_dir)
    incoming, pages, processed, ignored = _fetch_postcards(access_token, feeder.feeder_id)
    confirmed = _fetch_confirmed_history(
        access_token,
        feeder.feeder_id,
        include_manual_sightings=include_manual_sightings,
    )
    current_iso = current.isoformat()
    update = _update_history(
        state_dir,
        feeder.feeder_id,
        incoming,
        confirmed.evidence,
        current,
        persist=persist_history,
        replace_manual_evidence=include_manual_sightings,
        confirmed_species_by_media=confirmed.species_by_media,
    )
    species = _aggregate_species(
        update.history,
        window,
        current,
        limit,
        include_manual_sightings=include_manual_sightings,
    )
    stats = BirdBuddySyncStats(
        pages=pages,
        postcards_processed=processed,
        accepted_detections=sum(len(item.species) for item in incoming),
        ignored_postcards=ignored,
        duplicate_postcards=update.duplicate_postcards,
        reclassified_postcards=update.reclassified_postcards,
        history_started_at=update.history.history_started_at,
        earliest_initial_feed_at=update.history.earliest_initial_feed_at,
        last_successful_sync_at=current_iso,
        confirmed_pages=confirmed.pages,
        confirmed_records_processed=confirmed.records_processed,
        confirmed_records_accepted=confirmed.records_accepted,
        manual_records_accepted=confirmed.manual_records_accepted,
        confirmed_records_ignored=confirmed.records_ignored,
    )
    return BirdBuddySyncResult(species, stats)


def birdbuddy_status(
    state_dir: Path, *, include_manual_sightings: bool = False
) -> dict[str, object]:
    path = _auth_path(state_dir)
    if path.is_symlink():
        raise DataSourceError(f"Refusing symlinked Bird Buddy authentication state: {path}")
    if not path.exists():
        history_present = _history_path(state_dir).exists()
        if history_present:
            _read_history(state_dir)
        return {
            "authenticated": False,
            "authorization_confirmed": False,
            "history_present": history_present,
        }
    state = _read_auth_state(state_dir)
    histories = _read_history(state_dir)
    history = histories.get(state.feeder.feeder_id)
    return {
        "authenticated": True,
        "authorization_confirmed": True,
        "authorization_confirmed_at": state.authorization_confirmed_at,
        "feeder": {
            "id": state.feeder.feeder_id,
            "name": state.feeder.name,
            "role": state.feeder.role,
        },
        "least_privilege_guest": state.feeder.role == "member",
        "history": (
            {
                "history_started_at": history.history_started_at,
                "earliest_initial_feed_at": history.earliest_initial_feed_at,
                "last_successful_sync_at": history.last_successful_sync_at,
                "latest_detection_at": _latest_history_detection(
                    history,
                    include_manual_sightings=include_manual_sightings,
                ),
                "retained_postcards": len(history.postcards),
                "archived_species": len(history.archived_species),
                "confirmed_feeder_species": sum(
                    item.source == _CONFIRMED_FEEDER_SOURCE
                    for item in history.confirmed_species.values()
                ),
                "confirmed_manual_species": sum(
                    item.source == _CONFIRMED_MANUAL_SOURCE
                    for item in history.confirmed_species.values()
                ),
                "manual_sightings_included": include_manual_sightings,
            }
            if history is not None
            else None
        ),
    }
