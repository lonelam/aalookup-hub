#!/usr/bin/env bash

set -euo pipefail

: "${DEPLOY_SSH_PRIVATE_KEY:?Set the production DEPLOY_SSH_PRIVATE_KEY secret}"
: "${DEPLOY_KNOWN_HOSTS:?Set the production DEPLOY_KNOWN_HOSTS secret}"
: "${DEPLOY_HOST:?Set the production DEPLOY_HOST secret}"
: "${DEPLOY_USER:?Set the production DEPLOY_USER secret}"
: "${DEPLOY_URL:?Set the production DEPLOY_URL variable}"
: "${SOURCE_SHA:?Set SOURCE_SHA to the private source revision}"

[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "SOURCE_SHA must be a full 40-character lowercase commit SHA" >&2
  exit 1
}
[[ "$DEPLOY_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || {
  echo "DEPLOY_USER is invalid" >&2
  exit 1
}
deploy_port="${DEPLOY_PORT:-22}"
if [[ ! "$deploy_port" =~ ^[0-9]+$ ]] \
  || (( deploy_port < 1 || deploy_port > 65535 )); then
  echo "DEPLOY_PORT must be between 1 and 65535" >&2
  exit 1
fi
case "${DEPLOY_URL%/}" in
  http://* | https://*) ;;
  *)
    echo "DEPLOY_URL must be an HTTP(S) origin" >&2
    exit 1
    ;;
esac

ssh_dir="${RUNNER_TEMP:?RUNNER_TEMP is required}/aalookup-ssh"
install -d -m 0700 "$ssh_dir"
trap 'rm -rf -- "$ssh_dir"' EXIT
printf '%s\n' "$DEPLOY_SSH_PRIVATE_KEY" | tr -d '\r' > "$ssh_dir/deploy_key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" | tr -d '\r' > "$ssh_dir/known_hosts"
chmod 0600 "$ssh_dir/deploy_key" "$ssh_dir/known_hosts"
ssh-keygen -y -P "" -f "$ssh_dir/deploy_key" > /dev/null

destination="${DEPLOY_USER}@${DEPLOY_HOST}"
archive="aalookup-${SOURCE_SHA}.tar.gz"
remote_archive="/home/${DEPLOY_USER}/deploy/${archive}"
test -f "$archive" || {
  echo "Missing deployment archive $archive" >&2
  exit 1
}

ssh_options=(
  -i "$ssh_dir/deploy_key"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$ssh_dir/known_hosts"
  -p "$deploy_port"
)
scp_options=(
  -i "$ssh_dir/deploy_key"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$ssh_dir/known_hosts"
  -P "$deploy_port"
)

scp "${scp_options[@]}" "$archive" "$destination:$remote_archive"
printf -v deploy_command \
  'sudo -n -- /usr/local/sbin/aalookup-deploy --protocol 2 %q' "$SOURCE_SHA"
# The protocol is static and the only interpolated value is the locally quoted commit SHA.
# shellcheck disable=SC2029
if ! ssh "${ssh_options[@]}" "$destination" "$deploy_command"; then
  printf -v cleanup_command 'rm -f -- %q' "$remote_archive"
  # cleanup_command contains only the locally constructed archive path.
  # shellcheck disable=SC2029
  ssh "${ssh_options[@]}" "$destination" "$cleanup_command" || true
  exit 1
fi
