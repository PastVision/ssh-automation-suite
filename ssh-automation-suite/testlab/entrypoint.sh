#!/usr/bin/env bash
# Per-container startup for the SSH Integration Test Lab.
#
# Responsibilities, in order:
#   1. Refuse to start if the host's advertised identity does not match its
#      container hostname (catches a mis-wired compose setup early).
#   2. Generate unique SSH host keys once per container filesystem, so each
#      host is independently identifiable and keys survive a container restart.
#   3. Install the shared lab public key for the test user. Only the PUBLIC half
#      is ever present here; the private half stays in testlab/keys on the host.
#   4. Validate the sshd config and exec sshd in the foreground.
set -euo pipefail

LAB_USER="${LAB_USER:-tester}"
HOST_KEY_DIR=/etc/ssh/lab-host-keys
AUTHORIZED_KEYS_FILE="/home/${LAB_USER}/.ssh/authorized_keys"

log() { printf '[entrypoint] %s\n' "$*" >&2; }
fail() { printf '[entrypoint] FATAL: %s\n' "$*" >&2; exit 1; }

# --- 1. Hostname sanity -------------------------------------------------------
EXPECTED_HOSTNAME="${LAB_HOSTNAME:-$(hostname)}"
ACTUAL_HOSTNAME="$(hostname)"
if [ "$ACTUAL_HOSTNAME" != "$EXPECTED_HOSTNAME" ]; then
    fail "container hostname is '${ACTUAL_HOSTNAME}' but '${EXPECTED_HOSTNAME}' was expected"
fi

# --- 1b. Capability sanity ----------------------------------------------------
# sshd chroots to /run/sshd for privilege separation and dies per connection
# without CAP_SYS_CHROOT. The container would still look "healthy" (the TCP port
# is open) while every login failed with exit 255, so check it up front instead
# of failing silently 20 times.
has_cap() {  # has_cap <bit>
    local bnd
    bnd=$(awk '/^CapBnd:/{print $2}' /proc/self/status)
    [ -n "$bnd" ] || return 0      # cannot determine -> do not block startup
    local mask=$(( (0x${bnd}) >> $1 & 1 ))
    [ "$mask" -eq 1 ]
}
if ! has_cap 18; then
    fail "CAP_SYS_CHROOT is missing. sshd needs it to chroot for privilege
       separation; every login will fail with 'Connection reset'. Add
       'SYS_CHROOT' to cap_add for this service in docker-compose.yml."
fi

# --- 2. Host keys -------------------------------------------------------------
mkdir -p "$HOST_KEY_DIR"
chmod 0755 "$HOST_KEY_DIR"

if [ ! -s "${HOST_KEY_DIR}/ssh_host_ed25519_key" ]; then
    log "generating ed25519 host key for ${ACTUAL_HOSTNAME}"
    ssh-keygen -q -t ed25519 -N '' -C "${ACTUAL_HOSTNAME}" \
        -f "${HOST_KEY_DIR}/ssh_host_ed25519_key"
fi

if [ ! -s "${HOST_KEY_DIR}/ssh_host_rsa_key" ]; then
    log "generating rsa host key for ${ACTUAL_HOSTNAME}"
    ssh-keygen -q -t rsa -b 3072 -N '' -C "${ACTUAL_HOSTNAME}" \
        -f "${HOST_KEY_DIR}/ssh_host_rsa_key"
fi

chmod 0600 "${HOST_KEY_DIR}"/ssh_host_*_key
chmod 0644 "${HOST_KEY_DIR}"/ssh_host_*_key.pub

# --- 3. Shared lab public key -------------------------------------------------
PUBKEY_SOURCE="${LAB_PUBKEY_PATH:-/etc/lab/keys/id_lab.pub}"
[ -s "$PUBKEY_SOURCE" ] || fail "shared lab public key not found at ${PUBKEY_SOURCE}"

if ! ssh-keygen -l -f "$PUBKEY_SOURCE" >/dev/null 2>&1; then
    fail "${PUBKEY_SOURCE} is not a valid SSH public key"
fi

install -d -m 0700 -o "$LAB_USER" -g "$LAB_USER" "/home/${LAB_USER}/.ssh"
install -m 0600 -o "$LAB_USER" -g "$LAB_USER" "$PUBKEY_SOURCE" "$AUTHORIZED_KEYS_FILE"

log "installed shared lab public key for ${LAB_USER}@${ACTUAL_HOSTNAME}"

# --- 4. Start sshd ------------------------------------------------------------
mkdir -p /run/sshd
/usr/sbin/sshd -t -f /etc/ssh/sshd_config.lab \
    || fail "sshd configuration is invalid"

# Readiness marker used by scripts/lab.py health checks.
printf '%s %s\n' "$ACTUAL_HOSTNAME" "$(cat "${HOST_KEY_DIR}/ssh_host_ed25519_key.pub")" \
    > /run/lab/ready

log "starting sshd on ${ACTUAL_HOSTNAME} (publickey only)"
exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config.lab
