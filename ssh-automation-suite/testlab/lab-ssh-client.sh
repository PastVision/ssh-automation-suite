#!/usr/bin/env bash
# SSH client wrapper for the disposable lab-client container.
#
# A bind-mounted private key inherits the host filesystem's permissions. On
# Windows that means mode 0777, which OpenSSH refuses with "UNPROTECTED PRIVATE
# KEY FILE". Copying the key to a 0600 file inside the container solves it,
# which is why every client invocation goes through this wrapper.
#
# Usage: lab-ssh-client KEYFILE [ssh options] [user@host] [command]
#        KEYFILE may be "-" to run without an identity (auth-rejection tests).
set -euo pipefail

key="${1:?key path required (use '-' for none)}"
shift

key_args=()
if [ "$key" != "-" ]; then
    [ -s "$key" ] || { echo "lab-ssh-client: key not found: $key" >&2; exit 255; }
    install -m 0600 "$key" /tmp/.lab_client_key
    key_args=(-i /tmp/.lab_client_key)
fi

exec ssh "${key_args[@]}" \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    -o ConnectTimeout=10 \
    "$@"
