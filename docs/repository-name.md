# Repository-name migration

The planned GitHub name is `veteranbv/inky-bird`. The repository has not yet
been renamed. This checklist separates that URL change from an installation
migration. No package, command, service, data directory or credential needs to
be renamed.

## Compatibility contract

- Keep publishing `ghcr.io/veteranbv/inky-bird-frame`. Existing Compose files
  and version-pinned bundles continue to use that image. A repository rename
  must not silently freeze their updates.
- Keep the Python package and `inky-bird-frame` command, release bundle asset
  names, native service labels, AppArmor profile and existing data paths.
- Keep status API fields and existing Home Assistant entity IDs unchanged.
- Leave the old repository name unused after the rename so GitHub's redirects
  remain available. See [GitHub's rename documentation](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository).

## Cutover order

1. Merge and verify the compatibility-image workflow before renaming GitHub.
   Use the normal exact-head review and required checks. Do not publish an
   unmerged release to test a name change.
2. Inventory repository rules, required-check producers, webhooks, Pages,
   reusable actions and package access. Record the repository ID and existing
   release assets. The repo-name change must preserve those protections.
3. Check the personal gallery's catalog-fetch URLs and build integration.
   Its existing deployed pages remain available. Do not assume a redirect
   proves the next site build can fetch the catalog and artwork.
4. Rename the GitHub repository, then verify old and new Git operations, issue
   and release links, raw catalog/index and image reads, package access and
   the review/CI check producers. Verify the first normal trusted publication
   can still push to the compatibility image package with its existing access.
   Do not tag a release as part of this step.
5. Update canonical repository links in documentation and package metadata.
   Keep compatibility image, command, service and bundle identifiers intact.
6. A catalog publisher's configured repository and checkout remote must match.
   Leave both old spellings together while relying on verified redirects, or
   coordinate their later update as one operation. Do not change one alone.
   Verify a normal publication without changing the running application.

Pause if a required check, publisher, gallery read or old installation breaks.
Retain the original repository name and settings for a bounded rename-back;
do not create another repository at either name. A rename-back does not undo
other configuration edits, so keep and reverse those edits separately.
