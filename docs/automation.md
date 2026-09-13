# Actions and repository configuration

This repository publishes AALookup releases and runs its production deployment
automation. The application source remains in the private `lonelam/aalookup`
repository. Each workflow accepts an immutable source commit SHA and checks out
that exact revision with a read-only credential.

## Choose the operation

All five workflows are manual-only. Merging application or Hub code into `main`
does not build, publish, approve, or deploy a release.

| Workflow | Use it for | Result |
| --- | --- | --- |
| `pre-release.yml` — Build preview | Test an exact pushed source SHA before reserving a stable tag | GitHub-only preview and TestFlight build |
| `promote-release.yml` — Promote tested release | Reuse the exact accepted preview artifacts | Receipt and isolated draft; operator publication completes the stable candidate |
| `release.yml` — Build acceptance candidate | Build a stable source tag directly, or use `operation=ios` / `operation=verify` | Stable acceptance candidate, TestFlight-only build, or macOS signing verification |
| `deploy.yml` — Deploy server and website | Roll out a reviewed server and website revision | Production deployment with protocol and health checks |
| `deploy-review-pages.yml` — Publish public review pages | Maintain an approved set of public static pages | Plan or apply only that manifest; independent of client releases |

For a client release, prefer **preview → acceptance → promotion of the same
bytes → distribution approval**. Direct stable builds remain available when no
preview is being reused. Do not run both paths for the same stable version.

1. Push the source revision with consistent package versions and its changelog.
2. Build an explicit preview label such as `v1.0.1-rc.1`; retain the successful
   run ID, release ID, provenance hash, and platform acceptance results.
3. After acceptance, create the private stable tag at that tested source SHA.
   For promotion, create the public stable tag at the reviewed Hub promotion
   commit, and follow the receipt-based publication procedure below.
4. Approve the exact stable candidate in the website's release gate. Publication
   alone never advances downloads or updates.
5. If this launch also changes the website or server, deploy the reviewed source
   with `--client-release` and verify the public feeds. Server-only deployment
   remains independent of client publication.

The published `v1.0.0` release is complete and immutable. Future changes use a
new version; never move its tags, replace its assets, or rerun its old publishers.

## Run the workflows

Start from the Actions page or with GitHub CLI after the source revision has
been pushed. Commands below are templates for a new release:

```sh
# Deploy the private repository's pushed HEAD.
npm --prefix ../aalookup run app:deploy

# Or deploy any pushed private commit by its full SHA.
npm --prefix ../aalookup run app:deploy -- <40-character-source-sha>

# Build a tagged revision as a GitHub acceptance candidate (not GitHub Latest).
version=v1.0.1
source_sha="$(git -C ../aalookup rev-parse "${version}^{commit}")"
gh workflow run release.yml --repo lonelam/aalookup-hub \
  -f version="$version" \
  -f source_sha="$source_sha"

# Send another TestFlight build of an already-released version, without
# rebuilding or republishing anything else. Each run stamps the built bundles
# with its own run number, so App Store Connect accepts the upload as a new
# build of the same version — which is what a rejected or superseded build in
# review needs.
gh workflow run release.yml --repo lonelam/aalookup-hub \
  -f operation=ios \
  -f version="$version" \
  -f source_sha="$source_sha"

# Build, sign, notarize, and verify macOS artifacts without publishing them.
gh workflow run release.yml --repo lonelam/aalookup-hub \
  -f operation=verify \
  -f version="$version" \
  -f source_sha="$source_sha"

# After approving this exact client version and source in the website release
# gate, deploy its associated website/server revision with the approval check.
npm --prefix ../aalookup run app:deploy -- --client-release "$version" "$source_sha"

# Build and publish a GitHub-only prerelease from any pushed private commit.
source_sha="$(git -C ../aalookup rev-parse HEAD)"
gh workflow run pre-release.yml --repo lonelam/aalookup-hub \
  -f source_sha="$source_sha"

# Or choose an explicit prerelease label whose base matches the source version.
gh workflow run pre-release.yml --repo lonelam/aalookup-hub \
  -f source_sha="$source_sha" \
  -f version=v1.0.1-rc.1
```

## Candidate publication

`release.yml` publishes a stable `vX.Y.Z` tag as a public GitHub prerelease with
`make_latest=false`, only after macOS, Windows, Android and iOS builds succeed.
It does not refresh the production mirror or dispatch a website deployment.
TestFlight upload remains part of the existing iOS testing flow; it does not
submit an App Store release.

The candidate includes `release-provenance.json` with `schemaVersion: 1`,
`sourceCommit`, `workflowCommit`, `releaseTag`, and an `assets` array sorted by
name. Each of the 15 platform artifacts has its exact `name`, `sha256` and `size`
computed after signing/notarization and before upload. The provenance file is
not self-listed. Only the reviewed installers and metadata are uploaded; private
source files are never release assets. Existing public assets cannot be replaced.

Both build workflows call `scripts/publish_candidate.py`. A read-only target
check runs during source resolution, before the platform builds. It rejects an
already published version, a foreign draft, or a public tag pointing at another
Hub commit. `operation=ios` and `operation=verify` skip this publication check so
an existing stable version can still receive a TestFlight build or signing check.
Source SHAs are required for every operation.

The shared publisher owns the artifact manifest and provenance generation. It
uploads through a numeric release ID, verifies every asset's exact size and
SHA-256, and checks the source, workflow, tag, and draft again before publication.
Retrying an incomplete draft may reuse only identical existing assets and upload
missing ones. It never deletes or overwrites an asset; rebuilt bytes that differ
require a new preview version or explicit operator recovery. The final publish
request keeps `prerelease=true` and `make_latest=false`; fresh reads must confirm
`immutable=true` and unchanged asset IDs, sizes, hashes, and tag identity.

A failed API call or identity mismatch stops the operation and retains the draft
for inspection. There are no automatic mutation retries. The early check saves
build time; it does not replace the final checks. Repository release immutability
must remain enabled. Historical failed runs and receipts are audit evidence, not
release steps to replay. A cancellation response alone does not prove a stalled
run has terminated; preserve the published tag and asset protections.

Maintained builds require the private release scripts and changelog renderer.
The pre-1.0 inline iOS fallback and empty-changelog fallback have been retired.

Both release workflows include the Windows NSIS installer and its signature,
plus `AALookup-windows-x86_64-update.tar.gz` and its separate signature. New
desktop clients prepare this complete runtime before their next cold launch;
older Windows clients continue using the installer channel. The source build
enforces a single executable with its VC runtime linked statically. macOS keeps
the universal signed bundle and its signed update helper. Installer artifacts
remain available for manual installation.

The macOS jobs normalize the updater tar headers before signing the final
archive again. Tauri can include Unix file-type bits in the mode field; the
staged updater accepts permission bits only. The trusted Hub packaging script
removes only matching type bits after checking paths, entry types, links,
permissions, limits, and the update manifest. It verifies every bundle file's
hash and metadata across the rewrite, preserving the Apple signatures and
notarization. Privileged permissions or mismatched type bits fail the job.
The old archive signature is removed, and the final archive is signed with the
existing Tauri key before Apple verification, upload, and provenance generation.

`pre-release.yml` invokes the shared publisher with `--prerelease` to
record its preview tag and exact 15 artifacts. The stable release path rejects
prerelease labels, and the preview path requires one. A preview's provenance
does not make it eligible for the website's stable candidate gate.

## Promote a tested preview without rebuilding

`promote-release.yml` prepares a stable acceptance candidate from an already
tested, successful `pre-release.yml` run. It does not compile, sign, approve,
deploy, or upload another TestFlight build. Supply the exact private source SHA,
preview run ID, preview release ID, explicit preview tag, reviewed SHA-256 of
its `release-provenance.json`, and stable `version`. The preview must contain all
15 platform artifacts and provenance, and its recorded attempt must have all six
successful jobs, including all four platforms and publication.

Before dispatch, the operator verifies that repository release immutability is
enabled, the public stable tag points to the reviewed promotion workflow commit,
and the private stable tag resolves to the tested source. The stable release must
be absent. Retiring an earlier rejected draft or fencing an older publisher is a
separate reviewed operation; this tool never edits old releases or Git tags.
The promotion workflow shares `release-${version}` concurrency with release builds.

The CI preparation job checks both tag identities, source versions and changelog,
the successful preview run, release ownership and exact provenance, then downloads
all 15 artifacts and verifies their byte sizes and SHA-256 values. It writes a
new stable provenance describing the same payload bytes. Its `workflowCommit`
identifies the promotion workflow; the original build workflow, preview run,
release, provenance hash and all artifact IDs remain explicit in the receipt
and release notes. The source provenance is preserved alongside the receipt.

CI creates a new numeric release ID under a unique
`vX.Y.Z-promotion.<run-id>.<attempt>` draft tag and uploads only through that ID's
API upload URL. It verifies all 16 uploaded asset IDs, sizes and digests before
emitting a `PREPARED` receipt artifact. It never exposes this draft under the
stable tag and never publishes it. CI has only `contents: write` and
`actions: read`; the repository immutability setting requires administration
read access and is checked by the operator, without adding an administrator
credential to Actions.

After the preparation run completes successfully, download its receipt artifact,
record the exact `promotion-receipt.json` SHA-256, and use the same reviewed Hub
checkout to finish publication:

```sh
python3 scripts/promote_release.py publish \
  --receipt /private/path/promotion-receipt.json \
  --receipt-sha256 <reviewed-receipt-sha256> \
  --source-directory /path/to/exact-tested-private-checkout \
  --directory /private/path/new-publication-evidence
```

The publish command captures the existing `gh auth token` in memory. It verifies
the successful preparation workflow and attempt, downloads its original receipt
artifact again, checks its API SHA-256, size, ZIP members and CRC, and requires the
local receipt to match those exact CI bytes. It rechecks the source checkout and
private stable tag, preview provenance and original artifacts, the independent
draft's ownership and complete asset IDs/digests, the public stable tag, and the
live repository immutability setting. An existing stable release always refuses
publication.

Only then does one PATCH of the new numeric ID set the stable `tag_name` and
`draft=false` together, retaining `prerelease=true` and `make_latest=false`.
The response and fresh reads must confirm `immutable=true`, the stable tag's
exact Hub commit, and the unchanged complete artifact set. API conflicts or
drift stop the operation; it never deletes, clobbers, automatically cleans up,
or retries a publication mutation. A failed run retains its draft and receipt
for inspection. Repository immutability protects the published assets and tag;
GitHub's API documentation does not promise a wider multi-resource transaction.

`PUBLISHED_IMMUTABLE_CANDIDATE` still means an unapproved acceptance candidate.
The existing website release approval and distribution gate remains mandatory.

The website release gate approves the exact source and artifact set before its
download feed and updater feed advance. A GitHub publication, metadata refresh,
or ordinary website deployment does not approve a candidate.
For a website rollout associated with a client release, pass
`--client-release vX.Y.Z` to `app:deploy` (or set the same `client_release_tag`
workflow input). Immediately before deployment, the trusted helper checks the
public `/api/v1/releases/latest` response against that version and the exact
`source_sha`. Legacy manifests without `sourceCommit` fail this check. General
server operations omit that optional input and remain independent of client
releases; this is not a blanket restriction on all website content deployment.

## Server and website deployment

The source SHA is deliberately separate from this repository's `GITHUB_SHA`.
The latter identifies the public workflow revision, not the application being
built. Deployment selection never depends on the private `deploy` branch. The
workflow checks out its SSH deployment helper from this repository, so an
older source revision does not need to contain current Actions tooling.
`scripts/deploy_production.py` is that helper's caller: it validates the inputs,
copies the archive to the production host, and invokes the root-owned installer
there as `aalookup-deploy --protocol 10 <sha>`. Nothing on this side touches
production state. The installer itself lives in the private source repository at
`server/aalookup-deploy` and is installed on the host out of band, so this
workflow can ship a bad binary — which the installer will roll back — but never
a bad deployment procedure. Both are Python; the protocol number is what makes a
mismatch between them fail closed instead of half-running.
The server quality gate runs the source repository's isolated PostgreSQL 17
launcher, including matching PostgreSQL client tools and automatic fixture cleanup.
Protocol 10 explicitly builds and packages `aalookup-server`, `aalookup-backup`,
`aalookup-database`, `aalookup-membership-transition`, and `aalookup-event-log`
from the same source revision and `x86_64-unknown-linux-musl` release output.
Each must be a regular, nonempty, statically linked binary. The workflow copies
each ELF into the deployment directory and removes only its debug information,
preserving the original Cargo build output. It logs each original and shipped
byte size, each shipped SHA-256, and the final archive SHA-256. Before upload,
the package must satisfy the host's existing 100 MiB compressed and 512 MiB
expanded limits. The shared event-log reader is installed at
`/usr/local/libexec/aalookup-event-log` for authorized SSH log import. This requires
the matching analytics implementation and protocol-10 helper in the private source
repository. Payment operations now use the existing daemon through Admin
APIs. The retired `aalookup-billing` and the offline `aalookup-billing-catalog`
are excluded even if they exist in the local Cargo output directory. An older
source SHA lacking a required binary cannot produce this package; never mix
binaries from different revisions or retry with a historical protocol.
The installed helper supports PostgreSQL-only production after SQLite retirement
and checks unchanged issuer identity and authority when rolling back binaries.
It never restores live PostgreSQL data during deployment.

Protocol 10 is the installed production contract after the 1.0.0 launch. The
caller runs `aalookup-deploy --check --protocol 10` before uploading a server
archive or reviewed pages. A rejected check cleans up local credentials and
prevents upload and deployment. Merging this repository does not install the
root-owned helper. For a future protocol change, coordinate caller and helper
updates out of band, pause dispatches while they differ, and verify the matching
protocol before resuming. Do not replay the completed protocol-7 bootstrap or
mix intermediate helpers and binary packages. Before a schema migration, stop
independently running maintenance writers as required by the application runbook.

During protocol-10 deployment of the five matching binaries, the host transaction
also backs up and removes an installed legacy billing tool. Early rollback
restores the old tool and prior companions, or removes tools first installed by
the failed attempt.
Successful retirement removes the obsolete tool's recovery copy. A later website
failure retains the healthy server and matching companions. These host behaviors
are covered by the application helper tests; this Hub tests only the transport
and package boundaries.

Run the trusted caller and workflow packaging tests without SSH, Docker, or Cargo (including candidate provenance and associated-rollout gates):

```sh
python3 -m unittest discover -s tests
```

The workflow runs this gate before invoking the deployment caller. Packaging
tests execute the actual workflow shell with fixture binaries, mock only ELF
inspection, and check archive membership, permissions, missing operators and
dynamic-link rejection, and exclusion of retired/offline tools. Caller tests
mock SSH and verify protocol 10 before upload, strict host identity, failed-check
and failed-deployment cleanup, and no fallback protocol. Candidate tests exercise
real fixture bytes with a stateful GitHub boundary, including interrupted uploads,
asset drift, publication refusal, and immutable completion. These local tests do
not contact production or publish a release.

`pre-release.yml` accepts an exact source SHA without requiring a private source
tag. If `version` is omitted, it derives `v<source-base-version>-pre.<12-character-sha>`.
The resulting GitHub release is marked as a prerelease and is deliberately not
marked latest or sent to the production release mirror, so it is not offered by
the website or desktop updater. The Android preview still contains the
check-only updater path, but prerelease artifacts are not mirrored as a
production update; preview installers must be downloaded and installed manually.

## Repository secrets

Create `AALOOKUP_SOURCE_TOKEN` as a repository secret. It is a long-lived
fine-grained personal access token restricted to `lonelam/aalookup` with only
**Contents: read** permission. The checkout action consumes it directly and
does not persist it in the checked-out repository.

Create these repository secrets for release builds:

- `AALOOKUP_CLIENT_TOKEN`
- `ASC_API_KEY` (raw App Store Connect API `.p8` private-key contents)
- `ASC_API_KEY_ID`
- `ASC_API_ISSUER_ID`
- `TAURI_SIGNING_PRIVATE_KEY`
- `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` (optional for a passwordless key)
- `ANDROID_KEYSTORE` (base64-encoded Android upload/release keystore)
- `ANDROID_KEYSTORE_PASSWORD`
- `AALOOKUP_ANDROID_SIGNING_CERT_SHA256` (optional; the workflow derives the
  SHA-256 certificate digest from `ANDROID_KEYSTORE` when omitted; use the
  lowercase 64-hex form without colons when setting it explicitly)
- `APPLE_CERTIFICATE` (base64-encoded `.p12` containing the Developer ID
  Application certificate and private key)
- `APPLE_CERTIFICATE_PASSWORD` (empty when the `.p12` is passwordless)
- `APPLE_ID` (Apple account email used for notarization)
- `APPLE_PASSWORD` (an Apple app-specific password, never the account password)
- `GLITCHTIP_AUTH_TOKEN` (optional; a GlitchTip auth token with `org:read`,
  `project:read`, `project:write` and `project:releases`. Every client job and
  the deployment pass it to the private repository's release scripts, which use
  it to upload the source maps and debug files that turn a shipped stack trace
  back into source. Without it a release still builds and says so in its log,
  but its crashes can never be symbolicated afterwards — a map belongs to the
  exact bundle that produced it)

The workflow pins `APPLE_SIGNING_IDENTITY` to
`Developer ID Application: Zenan Lai (5CP5A63Q2H)` and `APPLE_TEAM_ID` to
`5CP5A63Q2H`; these identifiers are public signing metadata rather than secrets.

The Android release and prerelease jobs run the private source repository's
`scripts/release-android.sh`. They build the arm64 direct-download APK with
the official updater installer gate, sign the exact renamed APK with
`TAURI_SIGNING_PRIVATE_KEY`, and publish its adjacent `.apk.sig`. The
certificate digest is checked against the optional secret above (or the
keystore-derived value), so an APK signed by a different key cannot enter the
release asset set. The server's updater manifest and the website's download
manifest both offer the approved direct APK.

The iOS release and prerelease jobs require the App Store Connect API key
secrets above. `ASC_API_KEY` is the unencoded `.p8` contents; the key ID must
match the `AuthKey_<ID>.p8` filename and the issuer ID must belong to the same
App Store Connect team.

Export the Developer ID certificate and its private key together as a `.p12`,
then encode it without line wrapping before setting `APPLE_CERTIFICATE`:

```sh
openssl base64 -A -in DeveloperIDApplication.p12 -out certificate-base64.txt
gh secret set APPLE_CERTIFICATE --repo lonelam/aalookup-hub < certificate-base64.txt
```

The macOS job accepts a passwordless `.p12`, but fails before building when the
certificate, Apple ID, or app-specific notarization password is missing. Tauri
signs and notarizes each app, then the workflow notarizes each final DMG. It
checks the Developer ID authority, Team ID, hardened runtime, secure timestamps,
stapled tickets, Gatekeeper assessments, DMG signatures, and ZIP integrity
before uploading any macOS artifact.

Under **Settings -> Actions -> General -> Workflow permissions**, allow the
workflow token to request write access. Only publication and promotion preparation jobs request
`contents: write`. The macOS and source-resolution jobs request `contents: read`
for trusted Hub packaging and publication preflight at the exact workflow commit;
other platform build jobs explicitly receive no Hub repository permissions. The
private source token remains read-only and is never used for Hub publication.

## Production environment

Create a protected `production` environment with these secrets:

- `DEPLOY_SSH_PRIVATE_KEY`
- `DEPLOY_KNOWN_HOSTS`
- `DEPLOY_HOST`
- `DEPLOY_USER`

`DEPLOY_SSH_PRIVATE_KEY` must be an unencrypted (passwordless) private key;
the deployment helper intentionally rejects passphrase-protected keys.

The environment must define `DEPLOY_URL` and may define `DEPLOY_PORT`, which
defaults to `22`.

Configure the production AALookup server with:

```text
GITHUB_REPOSITORY=lonelam/aalookup-hub
```

Repository and environment secrets are available to anyone who can replace a
trusted workflow with code that exports them. Keep write access narrow, protect
the default branch, require review for `.github/workflows/**`, and add required
reviewers to the `production` environment.

## Public review pages

`deploy-review-pages.yml` is a separate bounded operation, defaulting to
`review-pages-plan`. It shares the `production-deploy` concurrency group and
production environment with full deployment. Its required inputs are an exact
private source SHA, the approved `manifest.json` SHA-256, and the root helper's
read-only filesystem inventory JSON. Choose `review-pages-apply` only for the
reviewed manifest; a changed live base or rebuilt candidate refuses publication.

The private source's public-page builder runs without deployment credentials.
The trusted Hub packager then verifies the manifest hash and packages only the
listed public HTML and fixed navigation script; local review metadata and all
other assets are excluded. The caller invokes the existing root-owned helper's
explicit review plan/apply mode with protocol 10 and the manifest hash. It never
falls back to full deployment or a weaker protocol. This mode does not stop the
API, migrate a schema, replace binaries, or enable billing.

The installed helper and caller must both support protocol 10 before dispatching
either deployment workflow. The helper rejects changed base
hashes, unapproved files and navigation code, symlinks and writable ancestry. It
retains original files and modes under
`/var/backups/aalookup-review-pages/<manifest-sha256>/`, verifies public content,
and rolls back normal failures. A hard termination may require operator recovery
from the retained record. A successful plan consumes its uploaded archive;
apply uploads the same candidate again and rechecks every precondition.
