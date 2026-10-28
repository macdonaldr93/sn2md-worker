# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Prune old and untagged GHCR container package versions while keeping recent releases.

Protects the top N semver/latest release versions and all multi-arch platform
and SBOM/provenance child manifests referenced by their OCI image indexes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass

_SEMVER_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+$")


class GhcrAccessError(RuntimeError):
    """Raised when querying or deleting GHCR package versions via `gh api` fails."""


class ManifestInspectError(RuntimeError):
    """Raised when inspecting a multi-arch OCI manifest via `docker buildx` fails."""


@dataclass(frozen=True)
class PackageVersion:
    id: int
    digest: str
    created_at: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class PrunePlan:
    kept_releases: tuple[PackageVersion, ...]
    protected_digests: frozenset[str]
    to_delete: tuple[PackageVersion, ...]


@dataclass(frozen=True)
class PruneOutcome:
    total_versions: int
    kept_releases: int
    protected_manifests: int
    deleted_version_ids: tuple[int, ...]
    skipped: bool = False


ListVersionsFn = Callable[[str, str], Sequence[PackageVersion]]
InspectManifestFn = Callable[[str, str], Sequence[str]]
DeleteVersionFn = Callable[[str, str, int], None]


def is_release_tag(tag: str) -> bool:
    """Return True if `tag` marks a release version (`latest` or `vX.Y.Z` / `X.Y.Z`)."""
    return tag == "latest" or bool(_SEMVER_TAG_RE.match(tag))


def parse_versions_pages(raw_json: str) -> list[PackageVersion]:
    """Parse `gh api --paginate --slurp` JSON pages into `PackageVersion` DTOs."""
    pages = json.loads(raw_json)
    versions: list[PackageVersion] = []
    for page in pages:
        for item in page:
            raw_tags = item.get("metadata", {}).get("container", {}).get("tags", [])
            versions.append(
                PackageVersion(
                    id=int(item["id"]),
                    digest=str(item["name"]),
                    created_at=str(item.get("created_at", "")),
                    tags=tuple(str(t) for t in raw_tags),
                )
            )
    return versions


def parse_child_digests(raw_manifest_json: str) -> list[str]:
    """Extract referenced child manifest digests from an OCI image index JSON payload."""
    manifest = json.loads(raw_manifest_json)
    manifests = manifest.get("manifests", [])
    if not isinstance(manifests, list):
        return []
    return [
        str(child["digest"]) for child in manifests if isinstance(child, dict) and "digest" in child
    ]


def plan_prune(
    versions: Sequence[PackageVersion],
    *,
    keep_releases: int,
    image: str,
    inspect_manifest: InspectManifestFn,
) -> PrunePlan:
    """Build a multi-arch-safe deletion plan keeping the newest `keep_releases` releases."""
    if keep_releases < 1:
        raise ValueError("keep_releases must be >= 1")

    sorted_versions = sorted(versions, key=lambda v: v.created_at, reverse=True)

    kept_releases: list[PackageVersion] = []
    for version in sorted_versions:
        if any(is_release_tag(t) for t in version.tags) and len(kept_releases) < keep_releases:
            kept_releases.append(version)

    protected_digests: set[str] = {v.digest for v in kept_releases}
    for version in kept_releases:
        child_digests = inspect_manifest(image, version.digest)
        protected_digests.update(child_digests)

    to_delete = tuple(v for v in sorted_versions if v.digest not in protected_digests)
    return PrunePlan(
        kept_releases=tuple(kept_releases),
        protected_digests=frozenset(protected_digests),
        to_delete=to_delete,
    )


def prune_package_versions(
    *,
    owner: str,
    package: str,
    image: str,
    keep_releases: int = 5,
    list_versions: ListVersionsFn | None = None,
    inspect_manifest: InspectManifestFn | None = None,
    delete_version: DeleteVersionFn | None = None,
) -> PruneOutcome:
    """List GHCR versions, protect the newest `keep_releases` + child manifests, and prune rest."""
    list_fn = list_versions or _gh_list_versions
    inspect_fn = inspect_manifest or _docker_inspect_child_digests
    delete_fn = delete_version or _gh_delete_version

    try:
        versions = list(list_fn(owner, package))
    except GhcrAccessError as exc:
        print(f"::warning::Could not list GHCR versions for {package}: {exc}")
        return PruneOutcome(
            total_versions=0,
            kept_releases=0,
            protected_manifests=0,
            deleted_version_ids=(),
            skipped=True,
        )

    try:
        plan = plan_prune(
            versions,
            keep_releases=keep_releases,
            image=image.lower(),
            inspect_manifest=inspect_fn,
        )
    except ManifestInspectError as exc:
        print(f"::warning::{exc}; skipping cleanup for {package}")
        return PruneOutcome(
            total_versions=len(versions),
            kept_releases=0,
            protected_manifests=0,
            deleted_version_ids=(),
            skipped=True,
        )

    print(
        f"{package}: total={len(versions)}, kept_releases={len(plan.kept_releases)}, "
        f"protected_manifests={len(plan.protected_digests)}, deleting={len(plan.to_delete)}"
    )

    deleted_ids: list[int] = []
    for version in plan.to_delete:
        try:
            delete_fn(owner, package, version.id)
        except GhcrAccessError as exc:
            print(
                f"::warning::Could not delete version {version.id} ({list(version.tags)}): {exc} "
                "(ensure repository has Admin access in Package Settings -> Manage Actions access)"
            )
            break
        deleted_ids.append(version.id)
        print(f"  deleted version {version.id} ({version.digest[:19]} tags={list(version.tags)})")

    return PruneOutcome(
        total_versions=len(versions),
        kept_releases=len(plan.kept_releases),
        protected_manifests=len(plan.protected_digests),
        deleted_version_ids=tuple(deleted_ids),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prune old and untagged GHCR container package versions."
    )
    parser.add_argument("--owner", required=True, help="GitHub repository/package owner")
    parser.add_argument("--package", required=True, help="GHCR package name (e.g. sn2md-worker)")
    parser.add_argument(
        "--image",
        required=True,
        help="Full OCI image name (e.g. ghcr.io/owner/sn2md-worker)",
    )
    parser.add_argument(
        "--keep-releases",
        type=int,
        default=5,
        help="Number of most recent release versions to retain (default: 5)",
    )
    args = parser.parse_args(argv)

    prune_package_versions(
        owner=args.owner,
        package=args.package,
        image=args.image,
        keep_releases=args.keep_releases,
    )
    return 0


def _gh_list_versions(owner: str, package: str) -> list[PackageVersion]:
    encoded_pkg = urllib.parse.quote(package, safe="")
    res = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"/users/{owner}/packages/container/{encoded_pkg}/versions?per_page=100",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise GhcrAccessError(res.stderr.strip() or f"gh api exited with {res.returncode}")
    return parse_versions_pages(res.stdout)


def _docker_inspect_child_digests(image: str, digest: str) -> list[str]:
    res = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", f"{image}@{digest}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise ManifestInspectError(
            f"Could not inspect {image}@{digest}: {res.stderr.strip() or res.returncode}"
        )
    try:
        return parse_child_digests(res.stdout)
    except json.JSONDecodeError as exc:
        raise ManifestInspectError(f"Invalid manifest JSON for {image}@{digest}: {exc}") from exc


def _gh_delete_version(owner: str, package: str, version_id: int) -> None:
    encoded_pkg = urllib.parse.quote(package, safe="")
    res = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "DELETE",
            f"/users/{owner}/packages/container/{encoded_pkg}/versions/{version_id}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise GhcrAccessError(res.stderr.strip() or f"gh api DELETE exited with {res.returncode}")


if __name__ == "__main__":
    sys.exit(main())
