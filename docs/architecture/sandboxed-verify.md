# Sandboxed verify and evidence (S4)

**Status:** S4a and S4b built 2026-09-29. The
owner chose option (b): project-declared services started by Helmraiser outside the
sandbox, with verify and evidence running inside it. A later run-isolation slice also
put setup under its own srt cache/registry policy; the S4 discussion below is retained
as the historical design.

## Problem

Before S4, `isolation: process` wrapped only the pi session in `srt`. Setup, verify and
evidence ran as `/bin/bash -o pipefail -c <run>` with no sandbox
(`src/helmraiser/loop/shell.py`). Verify executes code the implementer just wrote, such as
tests or project CI scripts; S4 addressed that output path.

## Constraints

These are facts about srt 1.0.0 on Linux, from its README:

- **Network:** the sandbox has no network namespace. All traffic goes through srt's
  HTTP/SOCKS proxies, bound into the sandbox as Unix sockets. Loopback is denied unless an
  IP literal with a port (`127.0.0.1:6333`) is in `allowedDomains`. Clients that ignore
  `HTTP(S)_PROXY`/`ALL_PROXY`, such as database drivers, cannot reach TCP services at all.
- **Unix sockets:** they are blocked by seccomp by default. `allowAllUnixSockets: true`
  lifts the block for every socket path the sandbox can see; `allowUnixSockets` path
  lists are ignored on Linux. Abstract sockets are isolated along with the network
  namespace.
- **Filesystem:** a socket is reachable only if its path is visible, so the dangerous
  sockets must stay hidden by `denyRead`:
  - the systemd user manager and D-Bus under `/run/user/$UID`, which can start processes
    outside the sandbox;
  - `/run/docker.sock` and `/var/run/docker.sock`, which are root-equivalent;
  - podman;
  - ssh-agent sockets.
- **Container-starting CI:** a project whose CI script starts Postgres, Redis and
  Qdrant with `docker run` needs externally provided services. The Docker socket must
  never be inside srt.

## Plan

### S4a: verify and evidence inside srt (all projects)

**Built.** `ProcessIsolation.settings(..., command=True)` and `wrap_command` in
`src/helmraiser/isolation/process.py`; the runner passes the wrap to `loop/verify.py` and
`loop/evidence.py`. Two choices went beyond the plan below: the command policy writes
only the run's `evidence/` directory, not the whole run dir (so a step cannot rewrite
the diff, events or summary), and it drops pi's `allowRead` carve-outs. The
`sandbox: host` escape hatch is per step, not per project. Checks passed on real runs
of several projects. Tools that open Unix sockets (such as the `tsx` CLI) need a
project-side alternative; model caches must be outside denied directories.

- Verify and evidence run through the same srt policy as the pi session:
  - the S2 read denies, and the own-project-only rule;
  - writes limited to the worktree, the run dir and `/tmp`;
  - network limited to the configured allowed domains;
  - the S1 environment, with secret-shaped variables stripped.
- At the time of S4, setup stayed outside. It now runs through
  `srt-settings-setup.json`, with project-declared package cache writes and registry
  domains, unless the approval-gated `[setup] sandbox = "host"` escape is explicit.
- Unix sockets stay blocked.
- A task or project may opt a step out with `sandbox: host` only when the project has
  `require_approval = true`. The run summary and the review then say that the step ran
  unconfined. This is the escape hatch; project-declared services are the intended
  path for tests needing databases.

### S4b: project services over an in-sandbox TCP relay

**Built.** `[[services]]` is parsed in `src/helmraiser/task/project.py`, the Docker
lifecycle is `src/helmraiser/loop/services.py`, the relay bootstrap is
`src/helmraiser/isolation/relay.py`, and `ProcessIsolation.wrap_command(...,
service_ports)` adds the literal ports and the bootstrap. Choices the plan below left
open:

- Services start once per run, before the first implement session (after an approved
  plan on the plan-first path), so a service that cannot start fails the run before
  any model time is spent. Readiness is a TCP connection on the host port that stays
  open for 0.3 s (Docker's port proxy accepts and then closes while the container is
  not listening yet), polled for `ready_timeout` seconds (default 60). A service with
  `ready` (an argv) also needs `docker exec <container> <ready…>` to exit 0, since
  Postgres accepts TCP before it accepts queries.
- `env` values reach Docker through its environment (`-e NAME`), not its argv; events
  and the summary record the names only.
- Cleanup goes by the `helmraiser.run` label, not by name. `work` removes labelled
  containers whose run is terminal, awaiting approval or not in the ledger; `doctor`
  lists them.
- The bootstrap waits for each relay from `/proc/net/tcp`, runs the step's own
  `/bin/bash -o pipefail -c <run>`, stops the relays and keeps the step's status; it
  fails the step with 125 when `socat` or srt's authenticated proxy is missing or a
  relay does not listen. Relay messages are appended without any line that holds the
  proxy credentials.
- `sandbox: host` steps and `isolation: none` tasks get the `provides` variables and
  connect to the published port directly. pi sessions get neither; the implement
  directive (`pi/directives.py`) names the provided variables, says only verify and
  evidence get them, and tells the implementer not to start containers, nor to run the
  verify steps that need them or answer `BLOCKED:` over them.

The spike on 2026-09-28 chose this route.
Unix sockets stay **blocked**: seccomp stays on and `allowAllUnixSockets` is never set.

With sockets allowed, a sandbox with no denies reached:
- Docker, which is root-equivalent;
- the systemd user bus;
- the Hyprland IPC socket;
- ssh-agent and gpg-agent;
- the Claude Code cross-session sockets (`cc-socks`);
- RustDesk IPC;
- Chrome's singleton socket.

Closing that took a long, host-specific deny list and a private `/tmp`, and any future
socket under the home directory would still be exposed.

`.helmraiser/project.toml` declares services, which Helmraiser starts outside srt with
`docker run`:

```toml
[[services]]
name = "postgres"
image = "postgres:16"
env = { POSTGRES_PASSWORD = "helmraiser", POSTGRES_DB = "app_test" }
port = 5432                          # container port
provides = { DATABASE_URL = "postgresql://postgres:helmraiser@127.0.0.1:{port}/app_test" }

[[services]]
name = "qdrant"
image = "qdrant/qdrant:v1.x"
port = 6333
provides = { QDRANT_URL = "http://127.0.0.1:{port}" }
```

- **Ports:** Helmraiser publishes each service on `127.0.0.1:<host port>`, choosing a free
  port it owns and reading it back from Docker. `{port}` is that host port.
- **Network:** each `127.0.0.1:<host port>` is added as a literal entry to the verify
  step's srt `allowedDomains`. `localhost:<port>` is blocked; only the IP literal
  matches.
- **Relays:** before the step's command, Helmraiser starts one relay per service inside the
  sandbox:
  `socat TCP-LISTEN:<port>,bind=127.0.0.1,fork,reuseaddr
  PROXY:<proxy host>:127.0.0.1:<port>,proxyport=<proxy port>,proxyauth=<user:token>`,
  with the proxy details parsed from srt's `HTTP_PROXY`. Because the relay listens on the
  same port, clients connect as usual, and srt's default `NO_PROXY` (which covers
  loopback and would break proxy-aware clients) no longer matters. Checked in the spike:
  `psql`, `curl`, `httpx` and `qdrant-client`. Redis is untested but is plain TCP.
- **Negative checks, from the spike:** a port that isn't allowed is refused (`CONNECT
  …: Forbidden`); Unix sockets stay `Operation not permitted`; Postgres over TCP without
  the relay is refused.
- **Stdin:** verify runs with `stdin=/dev/null`. Inherited descriptors are outside
  seccomp's socket filter.
- **Wiring:** `provides` values are expanded and passed to verify and evidence as
  environment variables. CI scripts that start containers need a mode that uses
  the provided services instead of `docker run`.
- **Lifecycle:** containers are named `helmraiser-<run>-<service>` and removed on success,
  failure, abort and reconcile, including anonymous volumes (`docker rm -v`).
- **Remaining exposure:** anything else listening on an allowed `127.0.0.1:<port>`, which
  is why Helmraiser owns and chooses the ports, and srt's proxy token, which is visible
  inside the sandbox as it is today.

## Order

S1–S3 (environment, read policy, sandboxed intake) land first. Then S4a, then S4b
(both built).
