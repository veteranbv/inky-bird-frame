# Names, upgrades and compatibility

`inky-bird` is the preferred name for the repository, Python distribution,
command and controller image. An existing installation does not need to move
its data or replace its service identities to use the updated application.

| Surface | Preferred for new installations | Existing installations |
| --- | --- | --- |
| Repository | `veteranbv/inky-bird` | Old GitHub URLs redirect after the rename; keep the old repository name unused. |
| Python distribution | `inky-bird` | Remove the old distribution before installing the new one; see below. |
| Command | `inky-bird` | `inky-bird-frame` remains a supported alias, including JSON output. |
| Controller image | `ghcr.io/veteranbv/inky-bird` | `ghcr.io/veteranbv/inky-bird-frame` receives the same builds and tags. No sunset is scheduled. |
| Native runtime, config and services | `inky-bird` / `com.inky-bird.*` | Setup retains the detected `inky-bird-frame` family and its paths. |
| Compose project and volumes | Stable `inky-bird-frame` project | Unchanged. The image name does not select a new data volume. |

The Python import namespace stays `inky_bird_frame`. Status API fields,
`INKY_*` environment variables, Home Assistant entity/unique IDs, the AppArmor
profile and container license paths stay compatible. Do not reset owner access,
provider credentials, collections, retry records or budgets for a name change.

## Native installations

Clean installs use `~/Services/inky-bird`, `~/Library/Application Support/Inky Bird`
on macOS or `~/.config/inky-bird` on Linux, and the `inky-bird` service family.
Existing installs keep their detected configuration/runtime/service family.
Explicit custom paths remain supported.

Setup, doctor and uninstall use the same read-only identity resolver. A support
root (even with a lost config), managed environment, installed unit/plist or
loaded service/timer identifies an installation. If both families exist,
setup and uninstall refuse to proceed;
doctor reports the conflict. Resolve it with the operator rather than removing
state or enabling another scheduler. A user LaunchAgent installer still refuses
to compete with operator-managed system LaunchDaemons under either name.

Uninstall removes only the selected family's jobs, including its dynamic
scheduler. It leaves configuration, artwork, history, authentication and the
runtime in place. Keeping old names on an existing install is supported, not a
partially completed migration.

## Python environments

The two distribution names own the same `inky_bird_frame/` files. **Do not
install them side by side**, and do not uninstall the old distribution after
installing the new one: that can remove files from the working package.

The locked native controller installer uses `uv sync`, which reconciles its
environment to the selected project. The display installer removes the old
distribution before installing the renamed one. For every display runtime upgrade,
it requires the timer and service to be inactive before it changes any files.
Use the existing service family, preserve the last panel image, and record the
original enabled/active state for restoration.

For an operator-managed pip/wheel deployment:

1. Stage the exact new wheel, its dependencies and a recoverable old runtime.
   Verify their source, hashes and compatibility before stopping anything.
2. Quiesce the role's writers and take a completed encrypted state backup.
   Prove an isolated restore of that backup. A source checkout is not a backup.
3. Remove `inky-bird-frame` from that exact virtual environment **first**. Install
   the staged `inky-bird` wheel into the same environment. Do not upgrade
   unrelated hardware packages or relocate state as part of this operation.
4. Check installed metadata, both commands, imports, doctor and the role's real
   behavior. Resume the recorded schedule and verify a natural successful
   display refresh with controller-received telemetry.
5. If validation fails, stop the affected role and restore its matched runtime
   and state preimages. If rolling back by wheel, remove `inky-bird` before
   reinstalling the old wheel. Never erase journals or fabricate receipts.

The naming change does not alter the catalog wire format or private-state
schema. Runtime replacement still needs its ordinary recovery checks. A label
change alone is not a reason to reset an otherwise valid soak interval.

## Containers and release bundles

One trusted build publishes both image names. Publication checks that every
tag resolves to the build's same multi-platform index digest, with both Linux
architectures and provenance/SBOM attestation manifests. An old image name does
not mean old code. Compare an immutable digest or source revision, not a label.

New releases provide `inky-bird-docker.tar.gz`. The old
`inky-bird-frame-docker.tar.gz` filename remains a byte-identical download alias;
both contain the canonical `inky-bird-docker/` directory. Use the supplied
checksum and the documented `--strip-components=1` extraction. Keep existing
`.env`, private configuration and Compose volumes when updating the image.

## Repository cutover

The GitHub rename is a separate owner operation, after compatibility changes
pass the normal exact-head review and required checks. It does not deploy an
application or create a release.

1. Record the repository ID, rules, required-check producers, hooks, Pages,
   reusable actions, release assets and package access.
2. Verify trusted main publication under both image names. Do not publish an
   unmerged release to test a name change.
3. Check the personal gallery's catalog-fetch URLs and build integration.
   A working deployed page does not prove its next build can fetch the catalog.
4. Rename GitHub, then verify old/new Git reads, issue and release links, raw
   index/manifest/image reads, package access and trusted check producers.
   Prove the next normal publication can update both packages.
5. Update local remotes and dependent source links deliberately. A catalog
   publisher's configured repository and checkout remote must match. Leave
   their old spellings together while relying on verified redirects, or update
   both in a coordinated operation; never change one alone.

Keep the old repository name unused so GitHub's redirects remain available.
See [GitHub's rename documentation](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository).
Pause if a required check, publisher, gallery read or old installation breaks.
A bounded rename-back preserves the repository identity; reverse any separate
configuration edits from their own preimages. Do not create another repository
at either name as a rollback technique.
