"""Scripted Leeboard CLI; never opens a real store."""

import json
import os
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    with Path(os.environ["FAKE_LB_LOG"]).open("a") as log:
        log.write(
            json.dumps(
                {
                    "argv": argv,
                    "cwd": os.getcwd(),
                    "env": {
                        key: os.environ.get(key)
                        for key in ("LEEBOARD_HOME", "LEEBOARD_HARNESS", "LEEBOARD_SESSION")
                    },
                }
            )
            + "\n"
        )
    script = json.loads(Path(os.environ["FAKE_LB_SCRIPT"]).read_text())
    command = argv[0] if argv else ""
    key = command
    if command == "check" and "--cwd" in argv:
        key = f"check@{Path(argv[argv.index('--cwd') + 1]).absolute()}"
    elif command == "list" and "--session" in argv:
        key = f"list@{argv[argv.index('--session') + 1]}"
    response = script.get(key, script.get(command, {}))
    if command == "check" and "--blocks" in argv:
        blocks = argv[argv.index("--blocks") + 1]
        response = script.get(f"{key}#{blocks}", script.get(f"check#{blocks}", response))
    time.sleep(response.get("sleep", 0))
    sys.stdout.write(response.get("stdout", ""))
    sys.stderr.write(response.get("stderr", ""))
    return response.get("exit", 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
