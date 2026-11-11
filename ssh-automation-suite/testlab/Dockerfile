# Reusable OpenSSH server image for the SSH Integration Test Lab.
#
# Deliberately minimal and self-contained: it contains no application code, no
# application config, and no application data. Only the public half of the lab
# key pair is supplied, and only at run time via a read-only bind mount.
FROM debian:12-slim

LABEL org.opencontainers.image.title="ssh-automation-suite test lab ssh server" \
      org.opencontainers.image.description="Ephemeral OpenSSH server for local integration testing"

ENV DEBIAN_FRONTEND=noninteractive \
    LAB_USER=tester

# Fixed, non-secret password for the lab's Run-As password path. It authenticates
# `sudo -S` for the `tester` login account only; SSH password authentication stays
# disabled and the account's own shadow field still cannot match a password, so
# this value never grants a *login*. Overridable at build time.
ARG LAB_SUDO_PASSWORD=LabSudoPassword1

# bash is required by the healthcheck probe and the test user's shell;
# procps/iproute2 for diagnostics inside the container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        openssh-server \
        openssh-client \
        bash \
        procps \
        iproute2 \
        sudo \
 && rm -rf /var/lib/apt/lists/*

# Test account. The password field is set to '*' (a value that can never match
# any crypt() hash) rather than left unlocked with '!':
#   '!'  marks the account *locked*, and sshd refuses locked accounts outright
#        with "User tester not allowed because account is locked" -- even for
#        public-key auth, so key login would never work.
#   '*'  is not a locked marker, so the account is usable, while password login
#        remains impossible because no hash can ever equal '*'.
# PasswordAuthentication no in sshd_config is the primary control; this is
# defence in depth, not a relaxation.
RUN useradd --create-home --shell /bin/bash --uid 1000 --password '*' "$LAB_USER"

# --- Run-As target accounts (Milestone 6) ------------------------------------ #
#
# Two deliberately different targets, so both sudo paths are exercised by
# separate tests rather than one standing in for the other:
#
#   deploy  `tester ALL=(deploy) NOPASSWD: ALL`   -> method=nopasswd
#   opspw   `tester ALL=(opspw) ALL` + passwords  -> method=password
#
# Neither is a login account: sshd still allows only `tester`, and their own
# password fields stay '*' so SSH password auth can never reach them. They exist
# purely as sudo *targets*, which is exactly the shape Run-As has.
#
# `tester` keeps '*' too, so SSH password login stays impossible; the password
# below exists solely so `sudo -S` can authenticate. That is the empirically
# verified behaviour of `sudo -u TARGET`: sudo verifies the **invoking** user's
# password and then switches identity through sudoers, so a password-based
# Run-As block carries the primary user's sudo password, not the target's.
RUN useradd --create-home --shell /bin/bash --password '*' deploy \
 && useradd --create-home --shell /bin/bash --password '*' opspw \
 && echo "tester:${LAB_SUDO_PASSWORD}" | chpasswd \
 && printf '%s\n' \
        'Defaults:tester !requiretty' \
        'Defaults:tester passwd_tries=3' \
        'tester ALL=(deploy) NOPASSWD: ALL' \
        'tester ALL=(opspw) ALL' \
        > /etc/sudoers.d/lab-run-as \
 && chmod 0440 /etc/sudoers.d/lab-run-as \
 && visudo -c -f /etc/sudoers.d/lab-run-as

COPY sshd_config.lab /etc/ssh/sshd_config.lab
COPY entrypoint.sh /usr/local/bin/lab-entrypoint.sh
COPY lab-ssh-client.sh /usr/local/bin/lab-ssh-client

# A Windows checkout can introduce CRLF line endings, which bash and sshd both
# reject. Normalise, fix modes, then validate the config against throwaway host
# keys and delete them so every container generates its own unique keys.
RUN sed -i 's/\r$//' /etc/ssh/sshd_config.lab /usr/local/bin/lab-entrypoint.sh \
        /usr/local/bin/lab-ssh-client \
 && chmod 0644 /etc/ssh/sshd_config.lab \
 && chmod 0755 /usr/local/bin/lab-entrypoint.sh /usr/local/bin/lab-ssh-client \
 && mkdir -p /run/sshd /run/lab /etc/ssh/lab-host-keys \
 && ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/lab-host-keys/ssh_host_ed25519_key \
 && ssh-keygen -q -t rsa -b 3072 -N '' -f /etc/ssh/lab-host-keys/ssh_host_rsa_key \
 && sshd -t -f /etc/ssh/sshd_config.lab \
 && rm -f /etc/ssh/lab-host-keys/ssh_host_*

# No EXPOSE-driven publishing: the lab network is the intended access path.
# See compose.loopback.yml for an opt-in 127.0.0.1-only binding.

CMD ["/usr/local/bin/lab-entrypoint.sh"]
