from __future__ import annotations

import json
import stat
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.birdbuddy import (
    _CONFIRMED_HISTORY,
    _FEED_QUERIES,
    ArchivedPostcardLink,
    ArchivedSpecies,
    BirdBuddyFeeder,
    BirdBuddyPostcard,
    ConfirmedSpeciesEvidence,
    FeederHistory,
    PostcardSpecies,
    _ConfirmedHistoryFetch,
    _fetch_confirmed_history,
    _fetch_postcard_variant,
    _fetch_postcards,
    _parse_confirmed_evidence,
    _parse_postcard,
    _reconcile_archived_postcards,
    _refresh_access_token,
    birdbuddy_status,
    login_birdbuddy,
    logout_birdbuddy,
    sync_birdbuddy_detections,
)
from inky_bird_frame.birds import ObservationWindow
from inky_bird_frame.errors import DataSourceError


def postcard(
    postcard_id: str,
    observed_at: datetime,
    species_id: str = "species-bluebird",
    common_name: str = "Eastern Bluebird",
    scientific_name: str = "Sialia sialis",
    media_ids: tuple[str, ...] = ("media-1",),
) -> BirdBuddyPostcard:
    return BirdBuddyPostcard(
        postcard_id,
        observed_at.astimezone(UTC).replace(microsecond=0).isoformat(),
        (PostcardSpecies(species_id, common_name, scientific_name),),
        media_ids=media_ids,
    )


def feed_node(
    *sightings: object,
    postcard_id: str = "postcard-1",
    observed_at: str = "2026-08-03T10:00:00+00:00",
    media_ids: tuple[str, ...] = ("media-1",),
) -> dict[str, object]:
    return {
        "__typename": "FeedItemNewPostcard",
        "id": postcard_id,
        "createdAt": observed_at,
        "inferenceConfidenceLevel": "HIGH_CONFIDENCE",
        "feeder": {"__typename": "FeederForMember", "id": "feeder-1"},
        "medias": [{"__typename": "MediaImage", "id": media_id} for media_id in media_ids],
        "sightingReportPreview": {"sightings": list(sightings)},
    }


def confirmed_node(
    *,
    origin: str,
    feeder_id: str,
    observed_at: str = "2026-08-03T10:00:00Z",
    media_id: str = "media-1",
) -> dict[str, object]:
    return {
        "origin": origin,
        "feeder": {"__typename": "FeederForMember", "id": feeder_id},
        "media": {"__typename": "MediaImage", "id": media_id, "createdAt": observed_at},
        "species": [
            {
                "__typename": "SpeciesBird",
                "id": "species-hummingbird",
                "name": "Ruby-throated Hummingbird",
                "scientificName": "Archilochus colubris",
            }
        ],
    }


class BirdBuddyTests(unittest.TestCase):
    def setUp(self) -> None:
        confirmed = patch(
            "inky_bird_frame.birdbuddy._fetch_confirmed_history",
            return_value=_ConfirmedHistoryFetch([], 1, 0, 0, 0, 0),
        )
        confirmed.start()
        self.addCleanup(confirmed.stop)

    def test_feed_operation_is_read_only_and_metadata_only(self) -> None:
        self.assertEqual(len(_FEED_QUERIES), 2)
        combined = "\n".join(_FEED_QUERIES)
        self.assertNotIn("mutation", combined.casefold())
        self.assertEqual(combined.count("species {"), 2)
        self.assertEqual(combined.count("medias { __typename id }"), 2)
        for forbidden in (
            "contentUrl",
            "sightingCreateFromPostcard",
            "finishPostcard",
            "reanalyze",
            "feederUpdate",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertNotIn("mutation", _CONFIRMED_HISTORY.casefold())
        self.assertIn("mediasOwned", _CONFIRMED_HISTORY)
        self.assertIn("media { __typename id createdAt }", _CONFIRMED_HISTORY)
        for feeder_type in (
            "FeederForMember",
            "FeederForOwner",
            "FeederForPublic",
            "FeederForRemoteGuest",
        ):
            self.assertIn(f"... on {feeder_type} {{ id }}", _CONFIRMED_HISTORY)
        for forbidden in ("contentUrl", "thumbnailUrl", "locationCity", "ownerName"):
            self.assertNotIn(forbidden, _CONFIRMED_HISTORY)

    def test_login_requires_authorized_access_confirmation_before_network(self) -> None:
        with (
            TemporaryDirectory() as temporary,
            patch("inky_bird_frame.birdbuddy._authenticate") as authenticate,
            self.assertRaisesRegex(DataSourceError, "confirmation is required"),
        ):
            login_birdbuddy(
                Path(temporary),
                email="birder@example.test",
                password="private-password",
                authorization_confirmed=False,
            )

        authenticate.assert_not_called()

    def test_login_persists_only_private_rotating_auth_state(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        with (
            TemporaryDirectory() as temporary,
            patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access-secret", "refresh-secret", [feeder]),
            ),
        ):
            state_dir = Path(temporary)
            result = login_birdbuddy(
                state_dir,
                email="birder@example.test",
                password="private-password",
                authorization_confirmed=True,
                now=now,
            )
            path = state_dir / "birdbuddy-auth.json"
            payload = path.read_text()
            permissions = stat.S_IMODE(path.stat().st_mode)

        self.assertTrue(result["authenticated"])
        self.assertEqual(permissions, 0o600)
        self.assertIn("refresh-secret", payload)
        self.assertNotIn("access-secret", payload)
        self.assertNotIn("private-password", payload)
        self.assertNotIn("birder@example.test", payload)

    def test_login_requires_explicit_selection_for_multiple_feeders(self) -> None:
        feeders = [
            BirdBuddyFeeder("feeder-1", "Front feeder", "member"),
            BirdBuddyFeeder("feeder-2", "Back feeder", "member"),
        ]
        with (
            TemporaryDirectory() as temporary,
            patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", feeders),
            ),
        ):
            state_dir = Path(temporary)
            with self.assertRaisesRegex(DataSourceError, "multiple feeders"):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                )

            self.assertFalse((state_dir / "birdbuddy-auth.json").exists())

    def test_parse_postcard_accepts_only_high_confidence_recognized_birds(self) -> None:
        recognized_sighting = {
            "__typename": "SightingRecognizedBird",
            "species": {
                "__typename": "SpeciesBird",
                "id": "species-bluebird",
                "name": "Eastern Bluebird",
                "scientificName": "Sialia sialis",
            },
        }
        node = feed_node(
            recognized_sighting,
            {"__typename": "SightingCantDecideWhichBird"},
        )

        parsed = _parse_postcard(node, "feeder-1")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.postcard_id, "postcard-1")
        self.assertEqual([item.species_id for item in parsed.species], ["species-bluebird"])
        low_confidence = _parse_postcard(
            {**node, "inferenceConfidenceLevel": "LOW_CONFIDENCE"}, "feeder-1"
        )
        self.assertIsNotNone(low_confidence)
        assert low_confidence is not None
        self.assertEqual(low_confidence.species, ())
        self.assertIsNone(_parse_postcard(node, "another-feeder"))

        missing_media_type = feed_node(recognized_sighting)
        missing_media_type["medias"] = [{"__typename": None, "id": "media-1"}]
        parsed_missing_media_type = _parse_postcard(missing_media_type, "feeder-1")
        self.assertIsNotNone(parsed_missing_media_type)
        assert parsed_missing_media_type is not None
        self.assertFalse(parsed_missing_media_type.complete)

        missing_feeder_type = feed_node(recognized_sighting)
        missing_feeder_type["feeder"] = {"__typename": None, "id": "feeder-1"}
        self.assertIsNone(_parse_postcard(missing_feeder_type, "feeder-1"))

        unlocked = {
            **node,
            "id": "postcard-2",
            "sightingReportPreview": {
                "sightings": [
                    {
                        "__typename": "SightingRecognizedBirdUnlocked",
                        "species": {
                            "__typename": "SpeciesBird",
                            "id": "species-cardinal",
                            "name": "Northern Cardinal",
                            "scientificName": "Cardinalis cardinalis",
                        },
                    }
                ]
            },
        }
        parsed_unlocked = _parse_postcard(unlocked, "feeder-1")
        self.assertIsNotNone(parsed_unlocked)
        assert parsed_unlocked is not None
        self.assertEqual(parsed_unlocked.species[0].species_id, "species-cardinal")

        no_bird = {
            **node,
            "sightingReportPreview": {"sightings": [{"__typename": "SightingNoBirdRecognized"}]},
        }
        parsed_no_bird = _parse_postcard(no_bird, "feeder-1")
        self.assertIsNotNone(parsed_no_bird)
        assert parsed_no_bird is not None
        self.assertEqual(parsed_no_bird.species, ())

        for incomplete_preview in (
            None,
            {},
            {"sightings": None},
            {"sightings": [None]},
            {"sightings": [{"__typename": None}]},
            {
                "sightings": [
                    {
                        "__typename": "SightingRecognizedBird",
                        "species": None,
                    }
                ]
            },
        ):
            incomplete = {**node, "sightingReportPreview": incomplete_preview}
            parsed_incomplete = _parse_postcard(incomplete, "feeder-1")
            self.assertIsNotNone(parsed_incomplete)
            assert parsed_incomplete is not None
            self.assertFalse(parsed_incomplete.complete)

        partially_incomplete = {
            **node,
            "sightingReportPreview": {
                "sightings": [
                    recognized_sighting,
                    {
                        "__typename": "SightingRecognizedBird",
                        "species": None,
                    },
                ]
            },
        }
        parsed_partial = _parse_postcard(partially_incomplete, "feeder-1")
        self.assertIsNotNone(parsed_partial)
        assert parsed_partial is not None
        self.assertFalse(parsed_partial.complete)

        conflicting_identity = feed_node(
            recognized_sighting,
            {
                "__typename": "SightingRecognizedBird",
                "species": {
                    "__typename": "SpeciesBird",
                    "id": "species-bluebird",
                    "name": "Conflicting Bluebird",
                    "scientificName": "Sialia conflictus",
                },
            },
        )
        parsed_conflict = _parse_postcard(conflicting_identity, "feeder-1")
        self.assertIsNotNone(parsed_conflict)
        assert parsed_conflict is not None
        self.assertFalse(parsed_conflict.complete)

        low_confidence_incomplete = {
            **node,
            "inferenceConfidenceLevel": "LOW_CONFIDENCE",
            "sightingReportPreview": None,
        }
        parsed_low_confidence = _parse_postcard(low_confidence_incomplete, "feeder-1")
        self.assertIsNotNone(parsed_low_confidence)
        assert parsed_low_confidence is not None
        self.assertFalse(parsed_low_confidence.complete)

    def test_feed_rejects_repeated_pagination_cursor(self) -> None:
        page = {
            "me": {
                "feed": {
                    "edges": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": "same-cursor"},
                }
            }
        }
        with (
            patch("inky_bird_frame.birdbuddy._graphql_request", return_value=page),
            self.assertRaisesRegex(DataSourceError, "repeated pagination cursor"),
        ):
            _fetch_postcards("access-secret", "feeder-1")

    def test_feed_rejects_conflicting_duplicate_postcards_across_pages(self) -> None:
        sighting = {
            "__typename": "SightingRecognizedBird",
            "species": {
                "__typename": "SpeciesBird",
                "id": "species-bluebird",
                "name": "Eastern Bluebird",
                "scientificName": "Sialia sialis",
            },
        }
        first = {
            "me": {
                "feed": {
                    "edges": [{"node": feed_node(sighting)}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                }
            }
        }
        second = {
            "me": {
                "feed": {
                    "edges": [
                        {
                            "node": feed_node(
                                sighting,
                                observed_at="2026-08-03T11:00:00+00:00",
                            )
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        with (
            patch(
                "inky_bird_frame.birdbuddy._graphql_request",
                side_effect=[first, second],
            ),
            self.assertRaisesRegex(DataSourceError, "conflicting duplicate postcards"),
        ):
            _fetch_postcard_variant(
                "access-secret",
                "feeder-1",
                _FEED_QUERIES[0],
                "SightingRecognizedBird",
            )

    def test_first_feed_page_omits_null_cursor(self) -> None:
        page = {
            "me": {
                "feed": {
                    "edges": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        with patch(
            "inky_bird_frame.birdbuddy._graphql_request",
            return_value=page,
        ) as request:
            _fetch_postcard_variant("access-secret", "feeder-1", _FEED_QUERIES[0])

        self.assertEqual(request.call_args.args[1], {"first": 50})

    def test_feed_merges_recognized_variants_by_postcard_and_species(self) -> None:
        observed_at = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
        recognized = postcard("postcard-1", observed_at)
        unlocked = postcard(
            "postcard-1",
            observed_at,
            "species-cardinal",
            "Northern Cardinal",
            "Cardinalis cardinalis",
        )
        with patch(
            "inky_bird_frame.birdbuddy._fetch_postcard_variant",
            side_effect=[
                ([recognized], 1, 1, 0),
                ([unlocked], 1, 1, 0),
            ],
        ):
            postcards, pages, processed, ignored = _fetch_postcards("access-secret", "feeder-1")

        self.assertEqual(pages, 2)
        self.assertEqual(processed, 1)
        self.assertEqual(ignored, 0)
        self.assertEqual(len(postcards), 1)
        self.assertEqual(
            [item.species_id for item in postcards[0].species],
            ["species-bluebird", "species-cardinal"],
        )

    def test_feed_combines_complementary_union_variants_before_validation(self) -> None:
        observed_at = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
        recognized = postcard("postcard-1", observed_at)
        unlocked = postcard(
            "postcard-1",
            observed_at,
            "species-cardinal",
            "Northern Cardinal",
            "Cardinalis cardinalis",
        )
        recognized_node = feed_node(
            {
                "__typename": "SightingRecognizedBird",
                "species": {
                    "__typename": "SpeciesBird",
                    "id": recognized.species[0].species_id,
                    "name": recognized.species[0].common_name,
                    "scientificName": recognized.species[0].scientific_name,
                },
            },
            {"__typename": "SightingRecognizedBirdUnlocked"},
            observed_at=observed_at.isoformat(),
        )
        unlocked_node = feed_node(
            {"__typename": "SightingRecognizedBird"},
            {
                "__typename": "SightingRecognizedBirdUnlocked",
                "species": {
                    "__typename": "SpeciesBird",
                    "id": unlocked.species[0].species_id,
                    "name": unlocked.species[0].common_name,
                    "scientificName": unlocked.species[0].scientific_name,
                },
            },
            observed_at=observed_at.isoformat(),
        )

        parsed_recognized = _parse_postcard(
            recognized_node,
            "feeder-1",
            "SightingRecognizedBird",
        )
        parsed_unlocked = _parse_postcard(
            unlocked_node,
            "feeder-1",
            "SightingRecognizedBirdUnlocked",
        )

        self.assertIsNotNone(parsed_recognized)
        self.assertIsNotNone(parsed_unlocked)
        assert parsed_recognized is not None
        assert parsed_unlocked is not None
        self.assertTrue(parsed_recognized.complete)
        self.assertTrue(parsed_unlocked.complete)

        with patch(
            "inky_bird_frame.birdbuddy._fetch_postcard_variant",
            side_effect=[
                ([parsed_recognized], 1, 1, 0),
                ([parsed_unlocked], 1, 1, 0),
            ],
        ):
            postcards, _, _, ignored = _fetch_postcards("access-secret", "feeder-1")

        self.assertEqual(ignored, 0)
        self.assertEqual(
            [item.species_id for item in postcards[0].species],
            ["species-bluebird", "species-cardinal"],
        )

    def test_feed_rejects_combined_postcard_when_selected_variant_is_incomplete(
        self,
    ) -> None:
        observed_at = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
        incomplete = BirdBuddyPostcard(
            "postcard-1",
            observed_at.isoformat(),
            (),
            complete=False,
        )
        unlocked = postcard(
            "postcard-1",
            observed_at,
            "species-cardinal",
            "Northern Cardinal",
            "Cardinalis cardinalis",
        )
        with patch(
            "inky_bird_frame.birdbuddy._fetch_postcard_variant",
            side_effect=[
                ([incomplete], 1, 1, 1),
                ([unlocked], 1, 1, 0),
            ],
        ):
            postcards, _, processed, ignored = _fetch_postcards("access-secret", "feeder-1")

        self.assertEqual(postcards, [])
        self.assertEqual(processed, 1)
        self.assertEqual(ignored, 1)

    def test_feed_ignores_postcard_missing_from_one_query_variant(self) -> None:
        observed_at = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
        recognized = postcard("postcard-1", observed_at)
        with patch(
            "inky_bird_frame.birdbuddy._fetch_postcard_variant",
            side_effect=[
                ([recognized], 1, 1, 0),
                ([], 1, 0, 0),
            ],
        ):
            postcards, _, processed, ignored = _fetch_postcards("access-secret", "feeder-1")

        self.assertEqual(postcards, [])
        self.assertEqual(processed, 1)
        self.assertEqual(ignored, 1)

    def test_confirmed_history_filters_by_provenance_and_selected_feeder(self) -> None:
        selected = _parse_confirmed_evidence(
            confirmed_node(origin="POSTCARD", feeder_id="feeder-1"),
            "feeder-1",
            include_manual_sightings=False,
        )
        other_feeder = _parse_confirmed_evidence(
            confirmed_node(origin="POSTCARD", feeder_id="feeder-2"),
            "feeder-1",
            include_manual_sightings=True,
        )
        manual_disabled = _parse_confirmed_evidence(
            confirmed_node(origin="CUSTOM_ID", feeder_id="manual"),
            "feeder-1",
            include_manual_sightings=False,
        )
        manual_enabled = _parse_confirmed_evidence(
            confirmed_node(origin="CUSTOM_ID", feeder_id="manual"),
            "feeder-1",
            include_manual_sightings=True,
        )
        watching = _parse_confirmed_evidence(
            confirmed_node(origin="WATCHING", feeder_id="feeder-1"),
            "feeder-1",
            include_manual_sightings=True,
        )
        malformed = confirmed_node(origin="POSTCARD", feeder_id="feeder-1")
        malformed["species"] = [{"__typename": "SpeciesBird", "id": None}]
        conflicting_species = confirmed_node(origin="POSTCARD", feeder_id="feeder-1")
        conflicting_species_values = conflicting_species["species"]
        assert isinstance(conflicting_species_values, list)
        conflicting_species_values.append(
            {
                "__typename": "SpeciesBird",
                "id": "species-hummingbird",
                "name": "Conflicting Hummingbird",
                "scientificName": "Archilochus conflictus",
            }
        )
        missing_species_type = confirmed_node(origin="POSTCARD", feeder_id="feeder-1")
        missing_species_type["species"] = [{}]
        missing_media_type = confirmed_node(origin="POSTCARD", feeder_id="feeder-1")
        assert isinstance(missing_media_type["media"], dict)
        missing_media_type["media"]["__typename"] = None
        missing_feeder = confirmed_node(origin="POSTCARD", feeder_id="feeder-1")
        missing_feeder["feeder"] = None
        missing_feeder_type = confirmed_node(origin="POSTCARD", feeder_id="feeder-1")
        assert isinstance(missing_feeder_type["feeder"], dict)
        missing_feeder_type["feeder"]["__typename"] = None
        unsupported_origin = confirmed_node(origin="UNKNOWN", feeder_id="feeder-1")

        assert selected is not None
        assert manual_enabled is not None
        self.assertEqual(selected.source, "selected_feeder")
        self.assertEqual(selected.observed_at, "2026-08-03T10:00:00+00:00")
        self.assertEqual(selected.media_id, "media-1")
        self.assertEqual(selected.species[0].scientific_name, "Archilochus colubris")
        self.assertIsNone(other_feeder)
        self.assertIsNone(manual_disabled)
        self.assertEqual(manual_enabled.source, "manual")
        self.assertIsNone(watching)
        with self.assertRaisesRegex(DataSourceError, "record was incomplete"):
            _parse_confirmed_evidence(
                malformed,
                "feeder-1",
                include_manual_sightings=False,
            )
        with self.assertRaisesRegex(DataSourceError, "conflicting species identities"):
            _parse_confirmed_evidence(
                conflicting_species,
                "feeder-1",
                include_manual_sightings=False,
            )
        with self.assertRaisesRegex(DataSourceError, "record was incomplete"):
            _parse_confirmed_evidence(
                missing_species_type,
                "feeder-1",
                include_manual_sightings=False,
            )
        with self.assertRaisesRegex(DataSourceError, "record was incomplete"):
            _parse_confirmed_evidence(
                missing_media_type,
                "feeder-1",
                include_manual_sightings=False,
            )
        with self.assertRaisesRegex(DataSourceError, "record was incomplete"):
            _parse_confirmed_evidence(
                missing_feeder,
                "feeder-1",
                include_manual_sightings=False,
            )
        with self.assertRaisesRegex(DataSourceError, "record was incomplete"):
            _parse_confirmed_evidence(
                missing_feeder_type,
                "feeder-1",
                include_manual_sightings=False,
            )
        with self.assertRaisesRegex(DataSourceError, "unsupported origin"):
            _parse_confirmed_evidence(
                unsupported_origin,
                "feeder-1",
                include_manual_sightings=False,
            )

    def test_confirmed_history_paginates_and_keeps_newest_species_evidence(self) -> None:
        first = {
            "me": {
                "mediasOwned": {
                    "edges": [
                        {
                            "node": confirmed_node(
                                origin="CUSTOM_ID",
                                feeder_id="manual",
                                observed_at="2026-08-03T09:00:00Z",
                            )
                        }
                    ],
                    "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                }
            }
        }
        second = {
            "me": {
                "mediasOwned": {
                    "edges": [
                        {
                            "node": confirmed_node(
                                origin="CUSTOM_ID",
                                feeder_id="manual",
                                observed_at="2026-08-03T10:00:00Z",
                                media_id="media-2",
                            )
                        },
                        {
                            "node": confirmed_node(
                                origin="POSTCARD",
                                feeder_id="another-feeder",
                            )
                        },
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        with patch(
            "inky_bird_frame.birdbuddy._graphql_request",
            side_effect=[first, second],
        ):
            result = _fetch_confirmed_history(
                "access-secret",
                "feeder-1",
                include_manual_sightings=True,
            )

        self.assertEqual(result.pages, 2)
        self.assertEqual(result.records_processed, 3)
        self.assertEqual(result.records_accepted, 2)
        self.assertEqual(result.manual_records_accepted, 2)
        self.assertEqual(result.records_ignored, 1)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].observed_at, "2026-08-03T10:00:00+00:00")

    def test_confirmed_history_links_selected_feeder_species_by_media(self) -> None:
        species_free = confirmed_node(
            origin="POSTCARD",
            feeder_id="feeder-1",
            media_id="media-2",
        )
        species_free["species"] = []
        page = {
            "me": {
                "mediasOwned": {
                    "edges": [
                        {
                            "node": confirmed_node(
                                origin="POSTCARD",
                                feeder_id="feeder-1",
                                media_id="media-1",
                            )
                        },
                        {"node": species_free},
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        with patch("inky_bird_frame.birdbuddy._graphql_request", return_value=page):
            result = _fetch_confirmed_history(
                "access-secret",
                "feeder-1",
                include_manual_sightings=False,
            )

        self.assertEqual(
            result.species_by_media["media-1"][0].scientific_name,
            "Archilochus colubris",
        )
        self.assertEqual(result.species_by_media["media-2"], ())
        self.assertEqual(result.records_accepted, 2)

    def test_confirmed_history_rejects_timestamp_conflicts_for_one_media(self) -> None:
        page = {
            "me": {
                "mediasOwned": {
                    "edges": [
                        {
                            "node": confirmed_node(
                                origin="POSTCARD",
                                feeder_id="feeder-1",
                                observed_at="2026-08-03T10:00:00Z",
                            )
                        },
                        {
                            "node": confirmed_node(
                                origin="POSTCARD",
                                feeder_id="feeder-1",
                                observed_at="2026-08-03T11:00:00Z",
                            )
                        },
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        with (
            patch("inky_bird_frame.birdbuddy._graphql_request", return_value=page),
            self.assertRaisesRegex(DataSourceError, "conflicting media records"),
        ):
            _fetch_confirmed_history(
                "access-secret",
                "feeder-1",
                include_manual_sightings=False,
            )

    def test_confirmed_history_rejects_opted_in_manual_media_conflicts(self) -> None:
        page = {
            "me": {
                "mediasOwned": {
                    "edges": [
                        {
                            "node": confirmed_node(
                                origin="CUSTOM_ID",
                                feeder_id="manual",
                                observed_at="2026-08-03T10:00:00Z",
                            )
                        },
                        {
                            "node": confirmed_node(
                                origin="CUSTOM_ID",
                                feeder_id="manual",
                                observed_at="2026-08-03T11:00:00Z",
                            )
                        },
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        with patch("inky_bird_frame.birdbuddy._graphql_request", return_value=page):
            ignored = _fetch_confirmed_history(
                "access-secret",
                "feeder-1",
                include_manual_sightings=False,
            )
        self.assertEqual(ignored.records_accepted, 0)

        with (
            patch("inky_bird_frame.birdbuddy._graphql_request", return_value=page),
            self.assertRaisesRegex(DataSourceError, "conflicting media records"),
        ):
            _fetch_confirmed_history(
                "access-secret",
                "feeder-1",
                include_manual_sightings=True,
            )

    def test_confirmed_history_rejects_malformed_selected_feeder_record(self) -> None:
        malformed = confirmed_node(origin="POSTCARD", feeder_id="feeder-1")
        malformed["species"] = [{"__typename": "SpeciesBird", "id": None}]
        page = {
            "me": {
                "mediasOwned": {
                    "edges": [{"node": malformed}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        with (
            patch("inky_bird_frame.birdbuddy._graphql_request", return_value=page),
            self.assertRaisesRegex(DataSourceError, "record was incomplete"),
        ):
            _fetch_confirmed_history(
                "access-secret",
                "feeder-1",
                include_manual_sightings=False,
            )

    def test_confirmed_history_rejects_repeated_pagination_cursor(self) -> None:
        page = {
            "me": {
                "mediasOwned": {
                    "edges": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": "same"},
                }
            }
        }
        with (
            patch("inky_bird_frame.birdbuddy._graphql_request", return_value=page),
            self.assertRaisesRegex(DataSourceError, "repeated pagination cursor"),
        ):
            _fetch_confirmed_history(
                "access-secret",
                "feeder-1",
                include_manual_sightings=False,
            )

    def test_revoked_refresh_token_has_actionable_redacted_error(self) -> None:
        with (
            patch(
                "inky_bird_frame.birdbuddy._graphql_request",
                side_effect=DataSourceError("HTTP 401 from Bird Buddy API"),
            ),
            self.assertRaisesRegex(DataSourceError, "run birdbuddy login again") as raised,
        ):
            _refresh_access_token("private-refresh-token")

        self.assertNotIn("private-refresh-token", str(raised.exception))

    def test_sync_rotates_token_and_handles_empty_feed(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("first-access", "first-refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("second-access", "second-refresh"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([], 1, 0, 0),
                ),
            ):
                result = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.LAST_30_DAYS,
                    limit=20,
                    now=now,
                )
            auth_payload = (state_dir / "birdbuddy-auth.json").read_text()
            history_path = state_dir / "birdbuddy-detections.json"

            self.assertEqual(result.species, [])
            self.assertEqual(result.stats.pages, 1)
            self.assertIn("second-refresh", auth_payload)
            self.assertNotIn("second-access", auth_payload)
            self.assertEqual(stat.S_IMODE(history_path.stat().st_mode), 0o600)

    def test_malformed_confirmed_snapshot_preserves_detection_history(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        confirmed = _ConfirmedHistoryFetch(
            [
                ConfirmedSpeciesEvidence(
                    PostcardSpecies(
                        "species-hummingbird",
                        "Ruby-throated Hummingbird",
                        "Archilochus colubris",
                    ),
                    "2026-08-03T10:00:00+00:00",
                    "selected_feeder",
                )
            ],
            1,
            1,
            1,
            0,
            0,
        )
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("access", "replacement"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([], 1, 0, 0),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_confirmed_history",
                    side_effect=[
                        confirmed,
                        DataSourceError("Bird Buddy confirmed history record was incomplete"),
                    ],
                ),
            ):
                sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    now=now,
                )
                history_path = state_dir / "birdbuddy-detections.json"
                before = history_path.read_bytes()
                with self.assertRaisesRegex(DataSourceError, "record was incomplete"):
                    sync_birdbuddy_detections(
                        state_dir,
                        window=ObservationWindow.ALL_TIME,
                        limit=20,
                        now=now + timedelta(minutes=5),
                    )

            self.assertEqual(history_path.read_bytes(), before)

    def test_manual_confirmed_history_is_opt_in_and_persisted_privately(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        hummingbird = ConfirmedSpeciesEvidence(
            PostcardSpecies(
                "species-hummingbird",
                "Ruby-throated Hummingbird",
                "Archilochus colubris",
            ),
            "2026-08-03T10:00:00+00:00",
            "manual",
        )
        confirmed = _ConfirmedHistoryFetch([hummingbird], 1, 1, 1, 1, 0)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("access", "replacement"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([], 2, 0, 0),
                ),
            ):
                with patch(
                    "inky_bird_frame.birdbuddy._fetch_confirmed_history",
                    return_value=confirmed,
                ):
                    included = sync_birdbuddy_detections(
                        state_dir,
                        window=ObservationWindow.LAST_DAY,
                        limit=20,
                        now=now,
                        include_manual_sightings=True,
                    )
                excluded = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.LAST_DAY,
                    limit=20,
                    now=now,
                    include_manual_sightings=False,
                )
            included_status = birdbuddy_status(state_dir, include_manual_sightings=True)
            excluded_status = birdbuddy_status(state_dir)
            history_path = state_dir / "birdbuddy-detections.json"
            history_payload = history_path.read_text()
            history_permissions = stat.S_IMODE(history_path.stat().st_mode)

        self.assertEqual(len(included.species), 1)
        self.assertEqual(included.species[0].scientific_name, "Archilochus colubris")
        self.assertEqual(included.species[0].detection_count, 1)
        self.assertEqual(included.stats.manual_records_accepted, 1)
        self.assertEqual(excluded.species, [])
        self.assertEqual(history_permissions, 0o600)
        self.assertIn('"source": "manual"', history_payload)
        self.assertNotIn("access", history_payload)
        included_history = included_status["history"]
        excluded_history = excluded_status["history"]
        assert isinstance(included_history, dict)
        assert isinstance(excluded_history, dict)
        self.assertEqual(included_history["latest_detection_at"], "2026-08-03T10:00:00+00:00")
        self.assertIsNone(excluded_history["latest_detection_at"])

    def test_confirmed_history_does_not_inflate_exact_postcard_count(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        exact = postcard("postcard-1", now - timedelta(hours=2))
        confirmed = ConfirmedSpeciesEvidence(
            exact.species[0],
            (now - timedelta(hours=1)).isoformat(),
            "selected_feeder",
        )
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("access", "replacement"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([exact], 2, 1, 0),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_confirmed_history",
                    return_value=_ConfirmedHistoryFetch([confirmed], 1, 1, 1, 0, 0),
                ),
            ):
                result = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.LAST_DAY,
                    limit=20,
                    now=now,
                )

        self.assertEqual(result.species[0].detection_count, 1)
        self.assertEqual(
            result.species[0].latest_detection_at,
            (now - timedelta(hours=1)).isoformat(),
        )

    def test_confirmed_media_reclassifies_linked_cached_postcard(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        preview = postcard(
            "postcard-1",
            now - timedelta(hours=2),
            media_ids=("media-1",),
        )
        corrected_species = PostcardSpecies(
            "species-cardinal",
            "Northern Cardinal",
            "Cardinalis cardinalis",
        )
        confirmed_evidence = ConfirmedSpeciesEvidence(
            corrected_species,
            (now - timedelta(hours=1)).isoformat(),
            "selected_feeder",
        )
        confirmed = _ConfirmedHistoryFetch(
            [confirmed_evidence],
            1,
            1,
            1,
            0,
            0,
            {"media-1": (corrected_species,)},
        )
        rejected = _ConfirmedHistoryFetch(
            [],
            1,
            1,
            1,
            0,
            0,
            {"media-1": ()},
        )
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("access", "replacement"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([preview], 2, 1, 0),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_confirmed_history",
                    side_effect=[confirmed, rejected],
                ),
            ):
                corrected = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.LAST_DAY,
                    limit=20,
                    now=now,
                )
                removed = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.LAST_DAY,
                    limit=20,
                    now=now + timedelta(minutes=5),
                )
            status = birdbuddy_status(state_dir)
            history = json.loads((state_dir / "birdbuddy-detections.json").read_text())

        self.assertEqual(
            [item.scientific_name for item in corrected.species],
            ["Cardinalis cardinalis"],
        )
        self.assertEqual(corrected.species[0].detection_count, 1)
        self.assertEqual(corrected.stats.reclassified_postcards, 1)
        self.assertEqual(removed.species, [])
        self.assertEqual(removed.stats.reclassified_postcards, 1)
        status_history = status["history"]
        assert isinstance(status_history, dict)
        self.assertIsNone(status_history["latest_detection_at"])
        stored = history["feeders"]["feeder-1"]["postcards"][0]
        self.assertEqual(stored["media_ids"], ["media-1"])
        self.assertEqual(stored["species"], [])

    def test_confirmed_media_recovers_same_sync_feed_tombstone_as_exact_postcard(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        exact = postcard(
            "postcard-exact",
            now - timedelta(hours=2),
            media_ids=("media-exact",),
        )
        tombstone = BirdBuddyPostcard(
            "postcard-confirmed",
            (now - timedelta(hours=1)).isoformat(),
            (),
            media_ids=("media-confirmed",),
        )
        evidence = ConfirmedSpeciesEvidence(
            exact.species[0],
            tombstone.observed_at,
            "selected_feeder",
        )
        confirmed = _ConfirmedHistoryFetch(
            [evidence],
            1,
            1,
            1,
            0,
            0,
            {"media-confirmed": exact.species},
        )
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("access", "replacement"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([exact, tombstone], 2, 2, 1),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_confirmed_history",
                    return_value=confirmed,
                ),
            ):
                result = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    now=now,
                )
            history = json.loads((state_dir / "birdbuddy-detections.json").read_text())

        self.assertEqual(result.species[0].detection_count, 2)
        stored = {item["id"]: item for item in history["feeders"]["feeder-1"]["postcards"]}
        self.assertEqual(
            stored["postcard-confirmed"]["species"][0]["scientific_name"],
            "Sialia sialis",
        )

    def test_sync_discards_legacy_postcard_without_media_linkage(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        legacy = postcard("postcard-1", now - timedelta(hours=2))
        legacy_payload = {
            "schema_version": 1,
            "feeders": {
                "feeder-1": {
                    "history_started_at": now.isoformat(),
                    "earliest_initial_feed_at": legacy.observed_at,
                    "last_successful_sync_at": now.isoformat(),
                    "postcards": [
                        {
                            "id": legacy.postcard_id,
                            "observed_at": legacy.observed_at,
                            "species": [
                                {
                                    "id": legacy.species[0].species_id,
                                    "common_name": legacy.species[0].common_name,
                                    "scientific_name": legacy.species[0].scientific_name,
                                }
                            ],
                        }
                    ],
                    "archived_species": {},
                    "confirmed_species": [],
                }
            },
        }
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            history_path = state_dir / "birdbuddy-detections.json"
            history_path.write_text(json.dumps(legacy_payload))
            history_path.chmod(0o600)
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("access", "replacement"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([], 2, 0, 0),
                ),
            ):
                result = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    now=now + timedelta(minutes=5),
                )
            migrated = json.loads(history_path.read_text())

        self.assertEqual(result.species, [])
        self.assertEqual(migrated["feeders"]["feeder-1"]["postcards"], [])

    def test_sync_preserves_inactive_feeder_legacy_postcards(self) -> None:
        selected_feeder = BirdBuddyFeeder("feeder-2", "Patio feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

        def legacy_history(postcard_id: str) -> dict[str, object]:
            observed_at = (now - timedelta(hours=2)).isoformat()
            return {
                "history_started_at": now.isoformat(),
                "earliest_initial_feed_at": observed_at,
                "last_successful_sync_at": now.isoformat(),
                "postcards": [
                    {
                        "id": postcard_id,
                        "observed_at": observed_at,
                        "species": [
                            {
                                "id": "species-bluebird",
                                "common_name": "Eastern Bluebird",
                                "scientific_name": "Sialia sialis",
                            }
                        ],
                    }
                ],
                "archived_species": {},
                "confirmed_species": [],
            }

        payload = {
            "schema_version": 1,
            "feeders": {
                "feeder-1": legacy_history("postcard-1"),
                "feeder-2": legacy_history("postcard-2"),
            },
        }
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [selected_feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            history_path = state_dir / "birdbuddy-detections.json"
            history_path.write_text(json.dumps(payload))
            history_path.chmod(0o600)
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("access", "replacement"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([], 2, 0, 0),
                ),
            ):
                sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    now=now + timedelta(minutes=5),
                )
            migrated = json.loads(history_path.read_text())

        self.assertEqual(
            migrated["feeders"]["feeder-1"]["postcards"][0]["id"],
            "postcard-1",
        )
        self.assertEqual(migrated["feeders"]["feeder-2"]["postcards"], [])

    def test_confirmed_media_reclassifies_archived_postcard_totals(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        observed_at = now - timedelta(days=367)
        original = postcard("postcard-1", observed_at, media_ids=("media-1",))
        corrected_species = PostcardSpecies(
            "species-cardinal",
            "Northern Cardinal",
            "Cardinalis cardinalis",
        )
        original_confirmed = _ConfirmedHistoryFetch(
            [
                ConfirmedSpeciesEvidence(
                    original.species[0],
                    observed_at.isoformat(),
                    "selected_feeder",
                )
            ],
            1,
            1,
            1,
            0,
            0,
            {"media-1": original.species},
        )
        corrected_confirmed = _ConfirmedHistoryFetch(
            [
                ConfirmedSpeciesEvidence(
                    corrected_species,
                    observed_at.isoformat(),
                    "selected_feeder",
                )
            ],
            1,
            1,
            1,
            0,
            0,
            {"media-1": (corrected_species,)},
        )
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("access", "replacement"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    side_effect=[([original], 2, 1, 0), ([], 2, 0, 0)],
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_confirmed_history",
                    side_effect=[original_confirmed, corrected_confirmed],
                ),
            ):
                archived = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    now=now,
                )
                corrected = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    now=now + timedelta(minutes=5),
                )
            history = json.loads((state_dir / "birdbuddy-detections.json").read_text())

        self.assertEqual(
            [item.scientific_name for item in archived.species],
            ["Sialia sialis"],
        )
        self.assertEqual(
            [item.scientific_name for item in corrected.species],
            ["Cardinalis cardinalis"],
        )
        self.assertEqual(corrected.species[0].detection_count, 1)
        self.assertEqual(corrected.stats.reclassified_postcards, 1)
        archived_state = history["feeders"]["feeder-1"]
        self.assertNotIn("species-bluebird", archived_state["archived_species"])
        self.assertEqual(
            archived_state["archived_postcards"][0]["species_ids"],
            ["species-cardinal"],
        )

    def test_archived_reclassification_recomputes_latest_detection(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        older = postcard(
            "postcard-older",
            now - timedelta(days=368),
            media_ids=("media-older",),
        )
        newer = postcard(
            "postcard-newer",
            now - timedelta(days=367),
            media_ids=("media-newer",),
        )
        corrected_species = PostcardSpecies(
            "species-cardinal",
            "Northern Cardinal",
            "Cardinalis cardinalis",
        )
        original_confirmed = _ConfirmedHistoryFetch(
            [],
            1,
            1,
            1,
            0,
            0,
            {"media-newer": newer.species},
        )
        corrected_confirmed = _ConfirmedHistoryFetch(
            [],
            1,
            1,
            1,
            0,
            0,
            {"media-newer": (corrected_species,)},
        )
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("access", "replacement"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    side_effect=[([older, newer], 2, 2, 0), ([], 2, 0, 0)],
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_confirmed_history",
                    side_effect=[original_confirmed, corrected_confirmed],
                ),
            ):
                before = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    now=now,
                )
                after = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    now=now + timedelta(minutes=5),
                )

        before_by_name = {item.scientific_name: item for item in before.species}
        after_by_name = {item.scientific_name: item for item in after.species}
        self.assertEqual(before_by_name["Sialia sialis"].detection_count, 2)
        self.assertEqual(
            before_by_name["Sialia sialis"].latest_detection_at,
            newer.observed_at,
        )
        self.assertEqual(after_by_name["Sialia sialis"].detection_count, 1)
        self.assertEqual(
            after_by_name["Sialia sialis"].latest_detection_at,
            older.observed_at,
        )
        self.assertEqual(after_by_name["Cardinalis cardinalis"].detection_count, 1)

    def test_archived_identity_correction_survives_confirmed_record_removal(self) -> None:
        observed_at = "2025-08-01T10:00:00+00:00"
        media_ids = ("media-1",)
        original = ArchivedSpecies(
            "species-bluebird",
            "Eastern Bluebird",
            "Sialia sialis",
            1,
            observed_at,
        )
        history = FeederHistory(
            "2025-01-01T00:00:00+00:00",
            None,
            None,
            {},
            {original.species_id: original},
            {},
            {media_ids: ArchivedPostcardLink(observed_at, media_ids, (original.species_id,))},
        )
        corrected = PostcardSpecies(
            original.species_id,
            "Eastern Bluebird (updated)",
            "Sialia sialis",
        )

        changed = _reconcile_archived_postcards(
            history,
            {"media-1": (corrected,)},
        )
        _reconcile_archived_postcards(history, {})

        self.assertEqual(changed, 1)
        self.assertEqual(
            history.archived_species[original.species_id].common_name,
            "Eastern Bluebird (updated)",
        )
        self.assertEqual(history.archived_postcards, {})

    def test_complete_confirmed_snapshot_removes_stale_manual_evidence(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        hummingbird = ConfirmedSpeciesEvidence(
            PostcardSpecies(
                "species-hummingbird",
                "Ruby-throated Hummingbird",
                "Archilochus colubris",
            ),
            "2026-08-03T10:00:00+00:00",
            "manual",
        )
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("access", "replacement"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([], 2, 0, 0),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_confirmed_history",
                    side_effect=[
                        _ConfirmedHistoryFetch([hummingbird], 1, 1, 1, 1, 0),
                        _ConfirmedHistoryFetch([], 1, 0, 0, 0, 0),
                    ],
                ),
            ):
                first = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    now=now,
                    include_manual_sightings=True,
                )
                removed = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    now=now + timedelta(minutes=15),
                    include_manual_sightings=True,
                )
            history = json.loads((state_dir / "birdbuddy-detections.json").read_text())

        self.assertEqual(len(first.species), 1)
        self.assertEqual(removed.species, [])
        self.assertEqual(history["feeders"]["feeder-1"]["confirmed_species"], [])

    def test_preview_sync_does_not_persist_detection_history(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("access", "replacement"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([postcard("postcard-1", now)], 1, 1, 0),
                ),
            ):
                result = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.LAST_DAY,
                    limit=20,
                    now=now,
                    persist_history=False,
                )

            self.assertEqual(len(result.species), 1)
            self.assertFalse((state_dir / "birdbuddy-detections.json").exists())

    def test_sync_is_idempotent_and_replaces_reclassification(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        original = postcard("postcard-1", now - timedelta(hours=1))
        corrected = postcard(
            "postcard-1",
            now - timedelta(hours=1),
            "species-cardinal",
            "Northern Cardinal",
            "Cardinalis cardinalis",
        )
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with patch(
                "inky_bird_frame.birdbuddy._refresh_access_token",
                return_value=("access", "replacement"),
            ):
                with patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([original], 1, 1, 0),
                ):
                    first = sync_birdbuddy_detections(
                        state_dir,
                        window=ObservationWindow.ALL_TIME,
                        limit=20,
                        now=now,
                    )
                with patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([original], 1, 1, 0),
                ):
                    duplicate = sync_birdbuddy_detections(
                        state_dir,
                        window=ObservationWindow.ALL_TIME,
                        limit=20,
                        now=now,
                    )
                with patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([corrected], 1, 1, 0),
                ):
                    reclassified = sync_birdbuddy_detections(
                        state_dir,
                        window=ObservationWindow.ALL_TIME,
                        limit=20,
                        now=now,
                    )

        self.assertEqual(first.species[0].detection_count, 1)
        self.assertEqual(duplicate.stats.duplicate_postcards, 1)
        self.assertEqual(reclassified.stats.reclassified_postcards, 1)
        self.assertEqual(
            [item.scientific_name for item in reclassified.species], ["Cardinalis cardinalis"]
        )

    def test_sync_removes_detection_when_classification_is_withdrawn(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        original = postcard("postcard-1", now - timedelta(hours=1))
        withdrawn = BirdBuddyPostcard(original.postcard_id, original.observed_at, ())
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with patch(
                "inky_bird_frame.birdbuddy._refresh_access_token",
                return_value=("access", "replacement"),
            ):
                with patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([original], 2, 1, 0),
                ):
                    first = sync_birdbuddy_detections(
                        state_dir,
                        window=ObservationWindow.ALL_TIME,
                        limit=20,
                        now=now,
                    )
                with patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([withdrawn], 2, 1, 1),
                ):
                    removed = sync_birdbuddy_detections(
                        state_dir,
                        window=ObservationWindow.ALL_TIME,
                        limit=20,
                        now=now,
                    )
            history = json.loads((state_dir / "birdbuddy-detections.json").read_text())

        self.assertEqual(len(first.species), 1)
        self.assertEqual(removed.species, [])
        self.assertEqual(removed.stats.reclassified_postcards, 1)
        self.assertEqual(history["feeders"]["feeder-1"]["postcards"], [])

    def test_sync_preserves_detection_when_postcard_preview_is_incomplete(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        original = postcard("postcard-1", now - timedelta(hours=1))
        incomplete_page = {
            "me": {
                "feed": {
                    "edges": [
                        {
                            "node": {
                                "__typename": "FeedItemNewPostcard",
                                "id": original.postcard_id,
                                "createdAt": original.observed_at,
                                "inferenceConfidenceLevel": "HIGH_CONFIDENCE",
                                "feeder": {
                                    "__typename": "FeederForMember",
                                    "id": feeder.feeder_id,
                                },
                                "medias": [{"__typename": "MediaImage", "id": "media-1"}],
                                "sightingReportPreview": None,
                            }
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with patch(
                "inky_bird_frame.birdbuddy._refresh_access_token",
                return_value=("access", "replacement"),
            ):
                with patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([original], 2, 1, 0),
                ):
                    sync_birdbuddy_detections(
                        state_dir,
                        window=ObservationWindow.ALL_TIME,
                        limit=20,
                        now=now,
                    )
                with patch(
                    "inky_bird_frame.birdbuddy._graphql_request",
                    return_value=incomplete_page,
                ):
                    preserved = sync_birdbuddy_detections(
                        state_dir,
                        window=ObservationWindow.ALL_TIME,
                        limit=20,
                        now=now,
                    )

        self.assertEqual(len(preserved.species), 1)
        self.assertEqual(preserved.species[0].scientific_name, "Sialia sialis")
        self.assertEqual(preserved.stats.reclassified_postcards, 0)
        self.assertEqual(preserved.stats.ignored_postcards, 1)

    def test_all_time_retains_pruned_detection_while_last_year_excludes_it(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
        old = postcard("postcard-old", now - timedelta(days=400))
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                    now=now,
                )
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("access", "replacement"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([old], 1, 1, 0),
                ),
            ):
                all_time = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.ALL_TIME,
                    limit=20,
                    now=now,
                )
                status = birdbuddy_status(state_dir)
            with (
                patch(
                    "inky_bird_frame.birdbuddy._refresh_access_token",
                    return_value=("access", "replacement-2"),
                ),
                patch(
                    "inky_bird_frame.birdbuddy._fetch_postcards",
                    return_value=([], 1, 0, 0),
                ),
            ):
                last_year = sync_birdbuddy_detections(
                    state_dir,
                    window=ObservationWindow.LAST_YEAR,
                    limit=20,
                    now=now,
                )

        self.assertEqual(all_time.species[0].detection_count, 1)
        history = status["history"]
        assert isinstance(history, dict)
        self.assertEqual(history["latest_detection_at"], old.observed_at)
        self.assertEqual(last_year.species, [])

    def test_status_and_logout_never_return_token_and_preserve_history(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access-secret", "refresh-secret", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                )
            (state_dir / "birdbuddy-detections.json").write_text(
                json.dumps({"schema_version": 1, "feeders": {}})
            )
            (state_dir / "birdbuddy-detections.json").chmod(0o600)

            status = birdbuddy_status(state_dir)
            logout = logout_birdbuddy(state_dir)

        self.assertNotIn("secret", json.dumps(status))
        self.assertTrue(logout["history_preserved"])
        self.assertFalse((state_dir / "birdbuddy-auth.json").exists())

    def test_insecure_auth_state_is_rejected(self) -> None:
        feeder = BirdBuddyFeeder("feeder-1", "Garden feeder", "member")
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            with patch(
                "inky_bird_frame.birdbuddy._authenticate",
                return_value=("access", "refresh", [feeder]),
            ):
                login_birdbuddy(
                    state_dir,
                    email="birder@example.test",
                    password="private-password",
                    authorization_confirmed=True,
                )
            (state_dir / "birdbuddy-auth.json").chmod(0o644)

            with self.assertRaisesRegex(DataSourceError, "mode 0600"):
                birdbuddy_status(state_dir)

    def test_broken_auth_symlink_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            (state_dir / "birdbuddy-auth.json").symlink_to(state_dir / "missing-auth.json")

            with self.assertRaisesRegex(DataSourceError, "Refusing symlinked"):
                birdbuddy_status(state_dir)


if __name__ == "__main__":
    unittest.main()
