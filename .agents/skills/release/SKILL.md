---
name: release
description: >-
  Use this skill when the user asks to cut, prepare, version, or publish a new
  release of sn2md-worker. Covers the bird codename convention, the version-bump
  draft PR, and publishing the GitHub Release that triggers GHCR builds.
---

# Release Workflow (`sn2md-worker`)

## 1. Determine the Version & Bird Codename

1. Inspect recent releases and commits since the last tag:
   ```sh
   gh release list --limit 10
   git log $(git describe --tags --abbrev=0)..origin/main --oneline
   ```
2. **Versioning (`0.x` semver) & Bird Codenames**:
   - **Minor releases (`v0.N.0 - <Bird>`)**: used for new features, behavioral changes, or default model/config changes. Each minor series gets a new North American bird codename:
     - `v0.1.x` — Chickadee
     - `v0.2.x` — Kingfisher
     - `v0.3.x` — Woodpecker
     - `v0.4.x` — Nuthatch
     - `v0.5.x` — Hummingbird
     - Future minor ideas: `Waxwing`, `Goldfinch`, `Warbler`, `Grosbeak`, `Kestrel`, `Wren`.
   - **Patch releases (`v0.N.P - <Same Bird>`)**: used for dependency updates, CI/CD fixes, and bug/doc fixes within the current `0.N` series (e.g., `v0.5.1 - Hummingbird`).

## 2. Open the Version-Bump Draft PR

`sn2md_worker.__version__` reads dynamically from installed package metadata (`importlib.metadata.version("sn2md-worker")`), so `pyproject.toml` (+ `uv.lock`) is the single version source of truth.

1. Create a release branch from up-to-date `main`:
   ```sh
   git checkout main && git pull origin main
   git checkout -b chore/release-X.Y.Z
   ```
2. Update `version = "X.Y.Z"` in `pyproject.toml`, sync `uv.lock`, and verify:
   ```sh
   uv sync
   uv run python -c 'import sn2md_worker; assert sn2md_worker.__version__ == "X.Y.Z"'
   uv run pytest
   ```
3. Commit, push, and open a **draft PR**:
   ```sh
   git add pyproject.toml uv.lock
   git commit -m "chore(release): bump version to X.Y.Z"
   git push origin chore/release-X.Y.Z
   gh pr create --draft --head chore/release-X.Y.Z \
     --title "chore(release): bump version to X.Y.Z" \
     --body "Bumps package version to \`X.Y.Z\` for the **vX.Y.Z - <Bird>** release."
   ```
4. Share the draft PR and preview the concise release notes with the user. Wait for the user to merge the PR.

## 3. Publish the GitHub Release

Once the version-bump PR is merged into `main`:

1. Pull `main` and delete the local release branch:
   ```sh
   git checkout main && git pull origin main
   git branch -D chore/release-X.Y.Z
   ```
2. Create the GitHub Release using the concise release template (which pushes tag `vX.Y.Z` and triggers `.github/workflows/release.yml` to publish `:X.Y.Z`, `:X.Y`, `:X`, `:latest` to GHCR and prune old versions via `scripts/ci/prune_ghcr.py`):
   ```sh
   gh release create vX.Y.Z --target main --title "vX.Y.Z - <Bird>" --notes "$(cat <<'EOF'
   <1-2 sentence high-level summary of the release.>

   ## What's in vX.Y.Z

   - **<Area>**: <concise bullet>
   - **<Area>**: <concise bullet>

   ## Upgrading from vA.B.C

   <State whether there are breaking or config changes.>

   - Pull `ghcr.io/macdonaldr93/sn2md-worker:vX.Y.Z` and restart the container

   See `docs/unraid-runbook.md` for the full deployment flow.
   EOF
   )"
   ```

## 4. Post-Release Validation

Before signing off on the release:

1. Watch or inspect the triggered `Publish container images` (`release.yml`) run on tag `vX.Y.Z`:
   ```sh
   gh run list --workflow release.yml --branch vX.Y.Z --limit 1
   gh api repos/macdonaldr93/sn2md-worker/commits/vX.Y.Z/check-runs \
     --jq '.check_runs[] | {name, status, conclusion, annotations_count: .output.annotations_count}'
   ```
2. Confirm both `Publish sn2md-worker` and `Publish obsidian-sync` succeeded with `annotations_count: 0`, and check the `Prune old and untagged GHCR versions` step log (`gh run view <run-id> --log | grep -E "total=.*kept_releases="`) to verify `kept_releases=5` and clean pruning.
