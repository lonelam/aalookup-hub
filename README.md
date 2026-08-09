# AALookup Hub

Public releases, web frontends, and automation tools for AALookup.

The application source remains in the private `lonelam/aalookup` repository.
Workflows in this repository accept an immutable source commit SHA, check out
that exact revision with a read-only credential, and either deploy the service
or publish desktop installers here. Platform-specific build and deployment
scripts stay with the private source and are executed only after that checkout.

## Run the workflows

Both workflows are manual-only. They can be started from the Actions page or
with GitHub CLI after the source revision has been pushed:

```sh
# Deploy the current private deploy branch.
git -C ../aalookup fetch origin deploy
source_sha="$(git -C ../aalookup rev-parse origin/deploy)"
gh workflow run deploy.yml --repo lonelam/aalookup-hub \
  -f source_sha="$source_sha"

# Publish a tagged private source revision as a public release.
version=v0.4.0
source_sha="$(git -C ../aalookup rev-parse "${version}^{commit}")"
gh workflow run release.yml --repo lonelam/aalookup-hub \
  -f version="$version" \
  -f source_sha="$source_sha"
```

The source SHA is deliberately separate from this repository's `GITHUB_SHA`.
The latter identifies the public workflow revision, not the application being
built.

## Repository configuration

Create `AALOOKUP_SOURCE_TOKEN` as an environment secret in both `release` and
`production`. It is a long-lived fine-grained personal access token restricted
to `lonelam/aalookup` with only **Contents: read** permission. Use no expiration
when the account policy allows it; otherwise use the longest permitted lifetime
and rotate both environment secrets together. The checkout action consumes this
token directly on every runner, including Windows, and does not persist it in
the checked-out repository.

Create a `release` environment with these secrets:

- `AALOOKUP_SOURCE_TOKEN`
- `AALOOKUP_CLIENT_TOKEN`
- `TAURI_SIGNING_PRIVATE_KEY`
- `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`
- `APPLE_CERTIFICATE` (optional)
- `APPLE_CERTIFICATE_PASSWORD` (optional)
- `APPLE_SIGNING_IDENTITY` (required when `APPLE_CERTIFICATE` is set)
- `AALOOKUP_RELEASE_REFRESH_TOKEN` (optional)

The `release` environment may define `AALOOKUP_UPDATE_ORIGIN`. It defaults to
`https://aalookup.laizn.cc`.

Under **Settings -> Actions -> General -> Workflow permissions**, allow the
workflow token to request write access. Only the final Release job requests
`contents: write`; build and source-resolution jobs explicitly receive no
repository permissions.

Create a protected `production` environment with these secrets:

- `AALOOKUP_SOURCE_TOKEN`
- `DEPLOY_SSH_PRIVATE_KEY`
- `DEPLOY_KNOWN_HOSTS`
- `DEPLOY_HOST`
- `DEPLOY_USER`

The `production` environment must define `DEPLOY_URL` and may define
`DEPLOY_PORT`, which defaults to `22`.

After releases move here, configure the production AALookup server with:

```text
GITHUB_REPOSITORY=lonelam/aalookup-hub
```

Repository and environment secrets are available to anyone who can replace a
trusted workflow with code that exports them. Keep write access narrow, protect
the default branch, require review for `.github/workflows/**`, and add required
reviewers to both environments.
