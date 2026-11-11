# SSH Integration Test Lab

A reproducible set of **20 independent Linux containers**, each running a real
OpenSSH server, for testing SSH connectivity and the future execution engine
against something that behaves like real remote hosts.

The lab is completely separate from the application: it shares no code, no
network, no volume, and no credential with `backend/` or `frontend/`. Nothing
here is imported by the application, and the application's database and
encryption key are never touched.

> **Docker is required.** There is no fallback environment — no mocks, no local
> `sshd`, no simulated transport. If Docker is unavailable, the scripts say so
> and exit non-zero rather than quietly testing something else.

---

## Requirements

- Docker Engine 24+ or Docker Desktop (WSL2 backend on Windows)
- Docker Compose v2 (`docker compose`, not `docker-compose`)
- `ssh-keygen` on the host, for generating the lab key pair

Check before anything else:

```powershell
python scripts/lab.py preflight
```

## Quick start

```powershell
cd testlab

python scripts/lab.py keys        # 1. generate the lab key pair (idempotent)
python scripts/lab.py up          # 2. build the image and start 20 hosts
python scripts/lab.py health      # 3. container health
python scripts/lab.py verify-auth # 4. public-key auth on all 20 hosts
python scripts/lab.py exec -- hostname   # 5. run a command everywhere
python scripts/lab.py results     # 6. per-host connectivity table
python scripts/lab.py down        # 7. stop and remove the containers
```

`up` waits for every container to report healthy and returns non-zero if any
host does not come up.

## Teardown

```powershell
python scripts/lab.py down            # keep host-key volumes for a fast restart
python scripts/lab.py down --volumes  # also delete host-key volumes (full reset)
```

Equivalent raw Compose commands, if you prefer not to use the wrapper:

```powershell
docker compose up -d --build
docker compose ps
docker compose down
docker compose down -v
```

## What gets created

| Component | Detail |
|---|---|
| Image | `ssh-lab/ssh-server:local`, built from `Dockerfile` (one image, 20 containers) |
| Containers | `ssh-lab-ssh-host-01` … `ssh-lab-ssh-host-20` |
| Hostnames | `ssh-host-01` … `ssh-host-20` (inside the lab network) |
| Test user | `tester` (password login impossible — the account is locked) |
| Auth | Public key only, one shared key across all 20 containers |
| Network | `ssh-lab-net`, bridge, `172.28.0.0/16` |
| Host keys | Unique per container, in a per-service named volume |
| Host ports | **None** (see below) |

## Network exposure

The base `docker-compose.yml` publishes **no SSH port to the Windows host**. The
only way to reach a lab host is from another container on `ssh-lab-net`, which is
how `scripts/lab.py` and the tests operate (they run an ephemeral `lab-client`
container with the key mounted read-only).

This is the strictest interpretation of "do not expose SSH ports to the Windows
host": there is no listening port on `127.0.0.1`, `0.0.0.0`, or any interface.

If you genuinely need a host-side port — for example, to point the application at
`127.0.0.1:2201` from Windows — use the opt-in override. It binds **loopback
only**, so nothing is reachable from your LAN:

```powershell
python scripts/lab.py up --loopback
# or:
docker compose -f docker-compose.yml -f compose.loopback.yml up -d
```

Ports are `127.0.0.1:2201` … `127.0.0.1:2220`, one per host.

## Keys

`python scripts/lab.py keys` writes an ed25519 pair to `testlab/keys/`:

```
keys/id_lab       private key, mode 0600, git-ignored, never mounted into a server container
keys/id_lab.pub   public key, installed into tester's authorized_keys on every host
```

- The **private** key is mounted read-only into the disposable `lab-client` only.
- Server containers receive the **public** key only, via a read-only bind mount.
- These keys are entirely separate from the application's stored credentials and
  from the application's Fernet master key. Nothing in `backend/data/` is used.
- The whole `keys/` directory is git-ignored — verified, so a key can never be
  committed by accident.

The command is idempotent and refuses to overwrite an existing private key unless
you pass `--force`.

## Failure simulation

Scenarios are **opt-in** and never run during a normal start-up. Standard
containers boot clean.

```powershell
python simulations/simulate.py all
python simulations/simulate.py auth-failure
python simulations/simulate.py unreachable
python simulations/simulate.py exit-1
python simulations/simulate.py long-running --seconds 120 --timeout 10
python simulations/simulate.py large-stdout
python simulations/simulate.py large-stderr
```

| Scenario | What it produces |
|---|---|
| `auth-failure` | An unauthorised key is rejected (`Permission denied`) |
| `wrong-user` | A non-existent account is rejected |
| `unreachable` | An unresolvable hostname fails to connect |
| `exit-1` | Remote command exit status 1 propagates to the client |
| `long-running` | A `sleep` that the client timeout must abort |
| `large-stdout` | ~5 MB on stdout, intact |
| `large-stderr` | ~5 MB on stderr, kept separate from stdout |

Each scenario asserts the expected outcome and prints `PASS`/`FAIL`.

## Automated tests

Two suites, deliberately separated by whether they need a daemon.

```powershell
# Artifact/config checks — no Docker needed, always runnable
cd ..\backend
.\.venv\Scripts\python.exe -m pytest ..\testlab\tests\test_lab_config.py -v

# Real end-to-end tests — needs a running lab
.\.venv\Scripts\python.exe -m pytest ..\testlab\tests -v
```

`test_lab_config.py` (27 tests) validates the artifacts themselves: 20 host
services with matching hostnames, a dedicated network, no published ports in the
base file, loopback-only bindings in the override, publickey-only sshd config, no
private key in any container mount, and no failure injection in the entrypoint.

`test_lab_integration.py` covers, against live containers:

1. All 20 endpoints reachable
2. Public-key authentication works
3. Each host reports the expected hostname
4. Remote commands execute, with exit codes and stderr propagated
5. Hosts have independent filesystems and process namespaces, and unique host keys
6. Invalid keys, unknown users, and password-style auth are all rejected

Integration tests **skip** (not fail) when Docker is unavailable, so the suite is
still meaningful on a machine without a daemon.

## Layout

```
testlab/
├── Dockerfile                 single reusable OpenSSH server image
├── sshd_config.lab            publickey-only sshd configuration
├── entrypoint.sh              per-container host keys + authorized_keys, then exec sshd
├── docker-compose.yml         20 hosts + dedicated network; publishes no ports
├── compose.loopback.yml       opt-in 127.0.0.1-only port bindings
├── pytest.ini
├── .gitignore                 keeps keys/ out of git
├── keys/                      generated, git-ignored (private + public)
├── scripts/
│   └── lab.py                 keys / up / down / health / verify-auth / exec / results
├── simulations/
│   └── simulate.py            opt-in failure scenarios
└── tests/
    ├── conftest.py
    ├── test_lab_config.py     Docker-free artifact validation
    └── test_lab_integration.py live end-to-end tests
```

## Design notes

- **One image, 20 containers.** `Dockerfile` is built once; every service reuses
  `ssh-lab/ssh-server:local`. A custom image is worthwhile because the distro
  `sshd` defaults are not guaranteed to disable password auth.
- **Unique host keys per container**, generated on first boot into a per-service
  volume. A restart keeps identity; a `down --volumes` regenerates it.
- **Least privilege.** `cap_drop: ALL` with only the capabilities sshd needs,
  `no-new-privileges`, no `privileged`, no host network, no application mounts.
- **CRLF safety.** The Dockerfile runs `sed -i 's/\r$//'` on the shell scripts and
  sshd config, so a Windows checkout with CRLF endings still builds.
- **`UsePAM no`.** Public-key-only auth needs no PAM, and PAM would require extra
  privileges in a minimal container.
- **Client key permissions.** A bind-mounted private key inherits the *host*
  filesystem's permissions ΓÇö on Windows that is `0777`, which OpenSSH rejects with
  `UNPROTECTED PRIVATE KEY FILE`. `lab-ssh-client.sh` therefore copies the key to
  a `0600` file inside the container before running `ssh`. Without this every
  connection from a Windows checkout would fail.

## Limitations

- The lab has only been validated statically on this machine; Docker is not
  installed here, so the image has not been built and the containers have not
  been started. See the milestone report for exactly what was and was not
  verified.
- Host keys are ephemeral by design. A `down --volumes` changes every host
  identity, so anything pinning host fingerprints must be reset.
- The 20 containers are modest but not free: expect roughly 400–600 MB of RAM
  in total.
- No resource limits (`mem_limit`/`cpus`) are set, so a runaway command in one
  container can still affect the host. Add them if you run the lab alongside a
  heavy workload.
