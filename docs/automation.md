# Actions and repository configuration

This repository publishes AALookup releases and runs its production deployment
automation. The application source remains in the private `lonelam/aalookup`
repository. Each workflow accepts an immutable source commit SHA and checks out
that exact revision with a read-only credential.

## Run the workflows

The workflows are manual-only. They can be started from the Actions page or
with GitHub CLI after the source revision has been pushed:

```sh
# Deploy the private repository's pushed HEAD.
npm --prefix ../aalookup run app:deploy

# Or deploy any pushed private commit by its full SHA.
npm --prefix ../aalookup run app:deploy -- <40-character-source-sha>

# Build a tagged revision as a GitHub acceptance candidate (not GitHub Latest).
version=v0.4.0
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
  -f version=v0.3.30-rc.1
```

`release.yml` publishes a stable `vX.Y.Z` tag as a public GitHub prerelease with
`make_latest=false`, only after macOS, Windows, Android and iOS builds succeed.
It no longer refreshes the production mirror or dispatches a website deployment.
TestFlight upload remains part of the existing iOS testing flow; it does not
submit an App Store release.

The candidate includes `release-provenance.json` with `schemaVersion: 1`,
`sourceCommit`, `workflowCommit`, `releaseTag`, and an `assets` array sorted by
name. Each of the 13 platform artifacts has its exact `name`, `sha256` and `size`
computed after signing/notarization and before upload. The provenance file is
not self-listed. Only the reviewed installers and metadata are uploaded; private
source files are never release assets. Existing public assets cannot be replaced.

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
Each must be a regular, nonempty, statically linked binary; the workflow logs
each SHA-256 and the final
archive SHA-256. The shared event-log reader is installed at
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

This release moves directly from the installed protocol 7 to protocol 10.
The intermediate development contracts are not deployment stages. The only
candidate payload contains all five binaries and the website; do not deploy a
payment-only package first or upgrade through intermediate helpers.
To activate protocol 10, merge this workflow and caller together, install the
matching root-owned helper out of band, verify
`aalookup-deploy --check --protocol 10`, then dispatch the reviewed source SHA.
The caller runs that same read-only host check before uploading either a server
archive or reviewed pages. A rejected check cleans up local credentials and
prevents upload and deployment. Pause dispatches during coordination: the helper
and caller intentionally reject different protocol versions. Merging this
repository does not install the host helper. Installing the tools does not run
billing reconciliation, apply an existing-user campaign, or enable sales.
Before schema migration, also stop independently running maintenance writers.

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
and failed-deployment cleanup, and no fallback protocol. The 2026-09-13 local
run passed all 20 tests against protocol 10, including the existing client approval,
release provenance and reviewed-page checks. It did not contact production or
dispatch a workflow; a real Actions package and coordinated host installation remain
release prerequisites.

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
release asset set. The server's updater manifest consumes this signed APK
additively while the website's human download manifest continues to hide it.

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
workflow token to request write access. Only the final publish jobs request
`contents: write`; build and source-resolution jobs explicitly receive no
repository permissions.

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

Install the reviewed helper out of band and coordinate the protocol 10 caller
before dispatching either deployment workflow. The helper rejects changed base
hashes, unapproved files and navigation code, symlinks and writable ancestry. It
retains original files and modes under
`/var/backups/aalookup-review-pages/<manifest-sha256>/`, verifies public content,
and rolls back normal failures. A hard termination may require operator recovery
from the retained record. A successful plan consumes its uploaded archive;
apply uploads the same candidate again and rechecks every precondition.
