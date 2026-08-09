# Actions and repository configuration

This repository publishes AALookup releases and runs its production deployment
automation. The application source remains in the private `lonelam/aalookup`
repository. Each workflow accepts an immutable source commit SHA and checks out
that exact revision with a read-only credential.

## Run the workflows

Both workflows are manual-only. They can be started from the Actions page or
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
```

The source SHA is deliberately separate from this repository's `GITHUB_SHA`.
The latter identifies the public workflow revision, not the application being
built. Deployment selection never depends on the private `deploy` branch. The
workflow checks out its SSH deployment helper from this repository, so an
older source revision does not need to contain current Actions tooling.

## Repository secrets

Create `AALOOKUP_SOURCE_TOKEN` as a repository secret. It is a long-lived
fine-grained personal access token restricted to `lonelam/aalookup` with only
**Contents: read** permission. The checkout action consumes it directly and
does not persist it in the checked-out repository.

Create these repository secrets for release builds:

- `AALOOKUP_CLIENT_TOKEN`
- `TAURI_SIGNING_PRIVATE_KEY`
- `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` (optional for a passwordless key)
- `APPLE_CERTIFICATE` (optional)
- `APPLE_CERTIFICATE_PASSWORD` (optional)
- `APPLE_SIGNING_IDENTITY` (required when `APPLE_CERTIFICATE` is set)
- `AALOOKUP_RELEASE_REFRESH_TOKEN` (optional)

The repository may define `AALOOKUP_UPDATE_ORIGIN` as an Actions variable. It
defaults to `https://aalookup.laizn.cc`.

Under **Settings -> Actions -> General -> Workflow permissions**, allow the
workflow token to request write access. Only the final Release job requests
`contents: write`; build and source-resolution jobs explicitly receive no
repository permissions.

## Production environment

Create a protected `production` environment with these secrets:

- `DEPLOY_SSH_PRIVATE_KEY`
- `DEPLOY_KNOWN_HOSTS`
- `DEPLOY_HOST`
- `DEPLOY_USER`

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
