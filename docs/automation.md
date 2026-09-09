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

# Publish a tagged private source revision as a public release.
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

# Refresh an already-published version without rebuilding it.
gh workflow run release.yml --repo lonelam/aalookup-hub \
  -f operation=refresh \
  -f version="$version"

# Build and publish a GitHub-only prerelease from any pushed private commit.
source_sha="$(git -C ../aalookup rev-parse HEAD)"
gh workflow run pre-release.yml --repo lonelam/aalookup-hub \
  -f source_sha="$source_sha"

# Or choose an explicit prerelease label whose base matches the source version.
gh workflow run pre-release.yml --repo lonelam/aalookup-hub \
  -f source_sha="$source_sha" \
  -f version=v0.3.30-rc.1
```

The source SHA is deliberately separate from this repository's `GITHUB_SHA`.
The latter identifies the public workflow revision, not the application being
built. Deployment selection never depends on the private `deploy` branch. The
workflow checks out its SSH deployment helper from this repository, so an
older source revision does not need to contain current Actions tooling.
`scripts/deploy_production.py` is that helper's caller: it validates the inputs,
copies the archive to the production host, and invokes the root-owned installer
there as `aalookup-deploy --protocol 2 <sha>`. Nothing on this side touches
production state. The installer itself lives in the private source repository at
`server/aalookup-deploy` and is installed on the host out of band, so this
workflow can ship a bad binary — which the installer will roll back — but never
a bad deployment procedure. Both are Python; the protocol number is what makes a
mismatch between them fail closed instead of half-running.

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
- `AALOOKUP_RELEASE_REFRESH_TOKEN` (optional)
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

The repository may define `AALOOKUP_UPDATE_ORIGIN` as an Actions variable. It
defaults to `https://aalookup.com`.

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
