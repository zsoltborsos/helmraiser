"""In-sandbox TCP relays to project services (S4b, ``docs/architecture/sandboxed-verify.md``).

srt gives a sandbox no route to the host's loopback: TCP leaves only through srt's
HTTP proxy, and only to an ``allowedDomains`` entry such as ``127.0.0.1:55432``.
Database drivers ignore ``HTTP_PROXY``, so a verify or evidence step that needs a
service runs behind a bootstrap which, inside srt, starts one socat relay per
service port first:

    socat TCP-LISTEN:<port>,bind=127.0.0.1,fork,reuseaddr
          PROXY:<proxy host>:127.0.0.1:<port>,proxyport=<proxy port>,proxyauth=<user:token>

The relay listens on the same port inside the sandbox's own network namespace, so a
client connects to ``127.0.0.1:<port>`` as usual, and srt's ``NO_PROXY`` (loopback)
sends proxy-aware clients to the relay too. The proxy host, port and credentials are
parsed from ``$HTTP_PROXY`` at runtime, inside srt; they never reach Helmraiser's
argv, events or logs. The bootstrap waits until every relay listens (from
``/proc/net/tcp``), runs the step's own ``/bin/bash -o pipefail -c <run>`` argv,
stops the relays and exits with the step's status. Relay messages are appended to
the step's output without any line that contains the credentials.
"""

from __future__ import annotations

import shlex

# 50 × 0.1 s for each relay to listen.
RELAY_WAIT_TRIES = 50
# The bootstrap's own failures (no socat, no srt proxy, a relay that never listened).
RELAY_FAILED = 125

_SCRIPT = """\
__sq_fail() {{ echo "[helmraiser] service relay: $1" >&2; exit {failed}; }}
command -v socat >/dev/null 2>&1 || __sq_fail "socat not found inside the sandbox"
case "${{HTTP_PROXY:-}}" in
  http://*@*:*) ;;
  *) __sq_fail "HTTP_PROXY is not srt's authenticated proxy" ;;
esac
__sq_p=${{HTTP_PROXY#http://}}
__sq_auth=${{__sq_p%@*}}
__sq_hp=${{__sq_p##*@}}
__sq_host=${{__sq_hp%:*}}
__sq_pport=${{__sq_hp##*:}}
__sq_log=$(mktemp) || __sq_fail "mktemp failed"
__sq_pids=()
__sq_stop() {{
  kill "${{__sq_pids[@]}}" 2>/dev/null
  wait "${{__sq_pids[@]}}" 2>/dev/null
  grep -v -F -e "$__sq_auth" -e "exiting on signal" "$__sq_log" | sed "s/^/[helmraiser relay] /" >&2
  rm -f "$__sq_log"
}}
for __sq_port in {ports}; do
  socat "TCP-LISTEN:$__sq_port,bind=127.0.0.1,fork,reuseaddr" \\
    "PROXY:$__sq_host:127.0.0.1:$__sq_port,proxyport=$__sq_pport,proxyauth=$__sq_auth" \\
    2>>"$__sq_log" &
  __sq_pids+=($!)
done
for __sq_port in {ports}; do
  __sq_listen=$(printf "0100007F:%04X 00000000:0000 0A" "$__sq_port")
  for ((__sq_i = 0; __sq_i < {tries}; __sq_i++)); do
    grep -q "$__sq_listen" /proc/net/tcp && continue 2
    sleep 0.1
  done
  __sq_stop
  __sq_fail "relay for 127.0.0.1:$__sq_port did not start"
done
{step}
__sq_rc=$?
__sq_stop
exit $__sq_rc
"""


def relay_script(argv: list[str], ports: tuple[int, ...] | list[int]) -> str:
    """The bash bootstrap that relays ``ports`` and then runs ``argv``."""
    if not ports:
        raise ValueError("relay_script needs at least one port")
    for port in ports:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"not a TCP port: {port!r}")
    return _SCRIPT.format(
        failed=RELAY_FAILED,
        ports=" ".join(str(port) for port in ports),
        tries=RELAY_WAIT_TRIES,
        step=shlex.join(argv),
    )


def relay_argv(argv: list[str], ports: tuple[int, ...] | list[int]) -> list[str]:
    """``argv`` (a step's bash argv) behind the relay bootstrap, for srt to run."""
    return ["/bin/bash", "-o", "pipefail", "-c", relay_script(argv, ports)]
