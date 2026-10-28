from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from scripts.ci.prune_ghcr import (
    GhcrAccessError,
    ManifestInspectError,
    PackageVersion,
    is_release_tag,
    main,
    parse_child_digests,
    parse_versions_pages,
    plan_prune,
    prune_package_versions,
)


def _make_version(
    version_id: int,
    *,
    digest: str | None = None,
    created_at: str = "2026-10-28T00:00:00Z",
    tags: tuple[str, ...] = (),
) -> PackageVersion:
    return PackageVersion(
        id=version_id,
        digest=digest or f"sha256:{version_id:064x}",
        created_at=created_at,
        tags=tags,
    )


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("latest", True),
        ("0.5.0", True),
        ("v0.5.0", True),
        ("1.2.3", True),
        ("0.5", False),
        ("0", False),
        ("main", False),
        ("sha-1942163", False),
        ("0.5.0-rc1", False),
    ],
)
def test_is_release_tag_matches_semver_and_latest_only(tag: str, expected: bool) -> None:
    assert is_release_tag(tag) is expected


def test_parse_versions_pages_flattens_paginated_gh_api_output() -> None:
    raw = json.dumps(
        [
            [
                {
                    "id": 10,
                    "name": "sha256:aaa",
                    "created_at": "2026-10-28T12:00:00Z",
                    "metadata": {"container": {"tags": ["0.5.0", "latest"]}},
                }
            ],
            [
                {
                    "id": 9,
                    "name": "sha256:bbb",
                    "created_at": "2026-10-27T12:00:00Z",
                    "metadata": {"container": {"tags": []}},
                }
            ],
        ]
    )

    versions = parse_versions_pages(raw)

    assert versions == [
        PackageVersion(
            id=10,
            digest="sha256:aaa",
            created_at="2026-10-28T12:00:00Z",
            tags=("0.5.0", "latest"),
        ),
        PackageVersion(
            id=9,
            digest="sha256:bbb",
            created_at="2026-10-27T12:00:00Z",
            tags=(),
        ),
    ]


def test_parse_child_digests_extracts_manifest_digests() -> None:
    raw = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "digest": "sha256:amd64",
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                },
                {
                    "digest": "sha256:arm64",
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                },
                {"digest": "sha256:attestation"},
            ],
        }
    )

    assert parse_child_digests(raw) == ["sha256:amd64", "sha256:arm64", "sha256:attestation"]


def test_plan_prune_rejects_keep_releases_below_one() -> None:
    with pytest.raises(ValueError, match="keep_releases must be >= 1"):
        plan_prune([], keep_releases=0, image="ghcr.io/o/p", inspect_manifest=lambda _i, _d: [])


class TestWhenPlanningPrune:
    def test_keeps_top_n_releases_and_protects_their_multi_arch_child_manifests(self) -> None:
        # GIVEN three releases (keep_releases=2), a sha-* build, and untagged child manifests
        r3 = _make_version(
            30, digest="sha256:r3", created_at="2026-10-28T03:00:00Z", tags=("0.3.0", "latest")
        )
        r3_amd64 = _make_version(31, digest="sha256:r3-amd64", created_at="2026-10-28T03:00:00Z")
        r3_arm64 = _make_version(32, digest="sha256:r3-arm64", created_at="2026-10-28T03:00:00Z")
        r2 = _make_version(
            20, digest="sha256:r2", created_at="2026-10-28T02:00:00Z", tags=("0.2.0", "0.2")
        )
        r2_amd64 = _make_version(21, digest="sha256:r2-amd64", created_at="2026-10-28T02:00:00Z")
        r1 = _make_version(
            10, digest="sha256:r1", created_at="2026-10-28T01:00:00Z", tags=("0.1.0",)
        )
        r1_amd64 = _make_version(11, digest="sha256:r1-amd64", created_at="2026-10-28T01:00:00Z")
        sha_only = _make_version(
            40,
            digest="sha256:sha-only",
            created_at="2026-10-28T04:00:00Z",
            tags=("main", "sha-abcdef1"),
        )
        orphan_untagged = _make_version(
            50, digest="sha256:orphan", created_at="2026-10-28T04:00:00Z"
        )

        child_map = {
            "sha256:r3": ["sha256:r3-amd64", "sha256:r3-arm64"],
            "sha256:r2": ["sha256:r2-amd64"],
            "sha256:r1": ["sha256:r1-amd64"],
        }

        # WHEN
        plan = plan_prune(
            [r1, r1_amd64, r2, r2_amd64, r3, r3_amd64, r3_arm64, sha_only, orphan_untagged],
            keep_releases=2,
            image="ghcr.io/owner/sn2md-worker",
            inspect_manifest=lambda _image, digest: child_map[digest],
        )

        # THEN r3 and r2 (plus their child manifests) are protected; r1, r1_amd64, sha_only,
        # and orphan_untagged are scheduled for deletion
        assert plan.kept_releases == (r3, r2)
        assert plan.protected_digests == frozenset(
            {"sha256:r3", "sha256:r3-amd64", "sha256:r3-arm64", "sha256:r2", "sha256:r2-amd64"}
        )
        assert set(plan.to_delete) == {r1, r1_amd64, sha_only, orphan_untagged}


class TestWhenPruningPackageVersions:
    def test_deletes_unprotected_versions_and_reports_outcome(self) -> None:
        # GIVEN
        kept = _make_version(
            1, digest="sha256:kept", created_at="2026-10-28T02:00:00Z", tags=("0.5.0", "latest")
        )
        child = _make_version(2, digest="sha256:child", created_at="2026-10-28T02:00:00Z")
        stale = _make_version(
            3, digest="sha256:stale", created_at="2026-10-28T01:00:00Z", tags=("sha-1234567",)
        )
        deleted: list[int] = []

        # WHEN
        outcome = prune_package_versions(
            owner="macdonaldr93",
            package="sn2md-worker",
            image="ghcr.io/macdonaldr93/sn2md-worker",
            keep_releases=5,
            list_versions=lambda _o, _p: [kept, child, stale],
            inspect_manifest=lambda _img, _d: ["sha256:child"],
            delete_version=lambda _o, _p, vid: deleted.append(vid),
        )

        # THEN
        assert outcome.skipped is False
        assert outcome.kept_releases == 1
        assert outcome.protected_manifests == 2
        assert outcome.deleted_version_ids == (3,)
        assert deleted == [3]

    def test_skips_cleanly_when_listing_versions_fails(self) -> None:
        # GIVEN
        delete_mock = MagicMock()

        def fail_list(_owner: str, _pkg: str) -> list[PackageVersion]:
            raise GhcrAccessError("HTTP 403 Forbidden")

        # WHEN
        outcome = prune_package_versions(
            owner="macdonaldr93",
            package="sn2md-worker",
            image="ghcr.io/macdonaldr93/sn2md-worker",
            list_versions=fail_list,
            inspect_manifest=lambda _i, _d: [],
            delete_version=delete_mock,
        )

        # THEN
        assert outcome.skipped is True
        assert outcome.deleted_version_ids == ()
        delete_mock.assert_not_called()

    def test_aborts_cleanup_before_deleting_if_manifest_inspection_fails(self) -> None:
        # GIVEN a kept release whose manifest inspection fails
        kept = _make_version(1, digest="sha256:kept", tags=("0.5.0",))
        untagged = _make_version(2, digest="sha256:child")
        delete_mock = MagicMock()

        def fail_inspect(_image: str, _digest: str) -> list[str]:
            raise ManifestInspectError("imagetools failed")

        # WHEN
        outcome = prune_package_versions(
            owner="macdonaldr93",
            package="sn2md-worker",
            image="ghcr.io/macdonaldr93/sn2md-worker",
            list_versions=lambda _o, _p: [kept, untagged],
            inspect_manifest=fail_inspect,
            delete_version=delete_mock,
        )

        # THEN nothing is deleted so multi-arch child manifests are never broken
        assert outcome.skipped is True
        assert outcome.deleted_version_ids == ()
        delete_mock.assert_not_called()

    def test_stops_deleting_remaining_versions_when_delete_permission_is_denied(self) -> None:
        # GIVEN two stale versions where the first delete fails with 403
        kept = _make_version(
            1, digest="sha256:kept", created_at="2026-10-28T03:00:00Z", tags=("0.5.0",)
        )
        stale1 = _make_version(
            2, digest="sha256:s1", created_at="2026-10-28T02:00:00Z", tags=("sha-1",)
        )
        stale2 = _make_version(
            3, digest="sha256:s2", created_at="2026-10-28T01:00:00Z", tags=("sha-2",)
        )
        attempted: list[int] = []

        def fail_delete(_o: str, _p: str, vid: int) -> None:
            attempted.append(vid)
            raise GhcrAccessError("Admin access required")

        # WHEN
        outcome = prune_package_versions(
            owner="macdonaldr93",
            package="sn2md-worker",
            image="ghcr.io/macdonaldr93/sn2md-worker",
            list_versions=lambda _o, _p: [kept, stale1, stale2],
            inspect_manifest=lambda _i, _d: [],
            delete_version=fail_delete,
        )

        # THEN it stops after the first failed delete rather than spamming failed calls
        assert attempted == [2]
        assert outcome.deleted_version_ids == ()


class TestWhenRunningCliMain:
    def test_parses_cli_args_and_invokes_prune(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GIVEN
        captured: dict[str, object] = {}

        def fake_prune(**kwargs: object) -> object:
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr("scripts.ci.prune_ghcr.prune_package_versions", fake_prune)

        # WHEN
        rc = main(
            [
                "--owner",
                "macdonaldr93",
                "--package",
                "sn2md-worker/obsidian-sync",
                "--image",
                "ghcr.io/macdonaldr93/sn2md-worker/obsidian-sync",
                "--keep-releases",
                "3",
            ]
        )

        # THEN
        assert rc == 0
        assert captured == {
            "owner": "macdonaldr93",
            "package": "sn2md-worker/obsidian-sync",
            "image": "ghcr.io/macdonaldr93/sn2md-worker/obsidian-sync",
            "keep_releases": 3,
        }
