"""A scripted ``docker`` for the services tests: containers live in a JSON state file.

Handles the calls ``loop/services.py`` makes: ``run -d``, ``port``, ``exec``, ``ps -a``
(``-q`` or the tab-separated ``--format``), ``rm -f -v``, ``logs`` and ``info``. Each call is
appended to ``FAKE_DOCKER_LOG`` with the values of its ``-e NAME`` variables. The
script (``FAKE_DOCKER_SCRIPT``) may hold:

- ``run_fail``: service name -> an error (or a list, one per attempt); the container
  is still created, as Docker does when it cannot bind the port;
- ``port_host``: the host ``docker port`` reports (default ``127.0.0.1``);
- ``published``: host port -> names ``ps --filter publish=<port>`` prints;
- ``ps_publish_fail``: an error for ``ps --filter publish=<port>``;
- ``exec_fail``: how many ``exec`` calls exit 1 before they exit 0;
- ``rm_fail``: an error for every ``rm``;
- ``info_fail``: an error for ``info``;
- ``logs``: what ``logs`` prints.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

LOG = Path(os.environ["FAKE_DOCKER_LOG"])
STATE = Path(os.environ["FAKE_DOCKER_STATE"])
SCRIPT = Path(os.environ["FAKE_DOCKER_SCRIPT"])


def load(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else default


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def find(state: dict[str, Any], ref: str) -> str | None:
    for cid, container in state["containers"].items():
        if ref in (cid, container["name"]):
            return cid
    return None


def matches(container: dict[str, Any], filters: list[str]) -> bool:
    for item in filters:
        key, _, value = item.removeprefix("label=").partition("=")
        if key not in container["labels"] or (value and container["labels"][key] != value):
            return False
    return True


def main(argv: list[str]) -> None:
    script = load(SCRIPT, {})
    state = load(STATE, {"containers": {}, "next": 0})
    command, args = argv[0], argv[1:]
    entry: dict[str, Any] = {"argv": argv}
    if command == "run":
        name, labels, ports, env, i = "", {}, {}, {}, 0
        while args[i].startswith("-"):
            flag = args[i]
            if flag == "-d":
                i += 1
                continue
            value = args[i + 1]
            if flag == "--name":
                name = value
            elif flag == "--label":
                key, _, label = value.partition("=")
                labels[key] = label
            elif flag == "-p":
                host, host_port, port = value.split(":")
                ports[port] = f"{host}:{host_port}"
            elif flag == "-e":
                env[value] = os.environ.get(value)
            i += 2
        entry["env"] = env
        with LOG.open("a") as log:
            log.write(json.dumps(entry) + "\n")
        if find(state, name) is not None:
            fail(f'Conflict. The container name "/{name}" is already in use')
        cid = f"{state['next']:012d}"
        state["next"] += 1
        state["containers"][cid] = {
            "name": name,
            "labels": labels,
            "ports": ports,
            "env": env,
            "image": args[i],
            "command": args[i + 1 :],
        }
        STATE.write_text(json.dumps(state))
        errors = script.get("run_fail", {}).get(labels.get("helmraiser.service"))
        if isinstance(errors, list):
            if errors:
                error = errors.pop(0)
                SCRIPT.write_text(json.dumps(script))
                fail(error)
        elif errors:
            fail(errors)
        print(cid + "f" * 52)
        return
    with LOG.open("a") as log:
        log.write(json.dumps(entry) + "\n")
    if command == "port":
        cid = find(state, args[0])
        if cid is None:
            fail(f"Error: No such container: {args[0]}")
        mapping = state["containers"][cid]["ports"].get(args[1].removesuffix("/tcp"))
        if mapping is None:
            fail(f"Error: No public port '{args[1]}' published for {args[0]}")
        host = script.get("port_host", mapping.split(":")[0])
        print(f"{host}:{mapping.split(':')[1]}")
    elif command == "exec":
        if find(state, args[0]) is None:
            fail(f"Error response from daemon: No such container: {args[0]}")
        if script.get("exec_fail", 0) > 0:
            script["exec_fail"] -= 1
            SCRIPT.write_text(json.dumps(script))
            fail("not ready")
    elif command == "ps":
        filters = [args[i + 1] for i, arg in enumerate(args) if arg == "--filter"]
        publish = next((f.removeprefix("publish=") for f in filters if "publish=" in f), None)
        if publish is not None:
            if script.get("ps_publish_fail"):
                fail(script["ps_publish_fail"])
            for name in script.get("published", {}).get(publish, []):
                print(name)
            return
        found = [(cid, c) for cid, c in state["containers"].items() if matches(c, filters)]
        for cid, container in found:
            if "-q" in args:
                print(cid)
            else:
                run = container["labels"].get("helmraiser.run", "")
                print(f"{cid}\t{run}\t{container['name']}\tUp 2 minutes")
    elif command == "rm":
        if script.get("rm_fail"):
            fail(script["rm_fail"])
        for ref in [arg for arg in args if not arg.startswith("-")]:
            cid = find(state, ref)
            if cid is not None:
                del state["containers"][cid]
        STATE.write_text(json.dumps(state))
    elif command == "logs":
        print(script.get("logs", ""))
    elif command == "info":
        if script.get("info_fail"):
            fail(script["info_fail"])
        print("29.0.0")
    else:
        fail(f"fake docker: unsupported command {command}")


if __name__ == "__main__":
    main(sys.argv[1:])
