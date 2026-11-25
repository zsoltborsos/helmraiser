"""``pick-packet`` writes the one file a judge reads, without anything of the setup;
``rework`` waits for the pick; ``pick`` reports a pull request as opened."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.blinding import RUN_MODEL_COLUMNS
from helmraiser.pi import skills
from helmraiser.task.model import ModelSpec
from tests.cli.test_experiment_forking import (  # noqa: F401 -- shared fixtures
    cli_env,
    data_dir,
    queue_task,
    reading,
    start_experiment,
    work,
)
from tests.cli.test_pair_lifecycle import (  # noqa: F401 -- shared fixtures
    PICK,
    git,
    invoke,
    open_ledger,
    pi,
    run_arms,
    script,
    work_once,
)
from tests.conftest import FakePi


def model_names(data_dir: Path) -> set[str]:  # noqa: F811
    """Every way the models of the ledger's runs are written."""
    names: set[str] = set()
    with open_ledger(data_dir) as ledger:
        for run in ledger.list_runs():
            for column in RUN_MODEL_COLUMNS:
                if run[column]:
                    spec = ModelSpec.parse(run[column])
                    names |= {spec.render(), spec.model, *spec.model.split("/", 1)}
    return names


# ── pick-packet ───────────────────────────────────────────────────────

EVIDENCE = """evidence:
  - name: shot
    run: "echo EVIDENCE-TAIL && cp done.txt $HELMRAISER_EVIDENCE_DIR/shot.txt"
    artifacts: ["shot.txt"]
    criteria: The shot says ok.
"""
REVIEW_WITH_EVIDENCE = (
    "The change does what the task asks.\n\n"
    '```json\n{"verdict": "pass", "findings": [], '
    '"evidence_checks": [{"name": "shot", "meets_criteria": true, "note": "says ok"}]}\n```'
)
PACKET_ARMS = {
    "A": {"skills": "lean-set"},
    "B": {"models": {"implement": {"model": "other-provider/arm-b"}}, "skills": "none"},
}


def test_the_pick_packet_holds_the_work_and_nothing_of_the_setup(
    tmp_project, tmp_path, data_dir, fake_pi: FakePi, reading  # noqa: F811
):
    skill = skills.SKILL_SETS_ROOT / "lean-set" / "implement" / "tdd"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: tdd\ndescription: test first\n---\n")
    fake_pi.write_script(
        {
            "sessions": {
                "*-implement": {"text": "Done.", "cost": 0.05, "files": [["done.txt", "ok\n"]]},
                "*-review": {"text": REVIEW_WITH_EVIDENCE, "cost": 0.02},
            }
        }
    )
    task_id = queue_task(tmp_project, data_dir, extra=EVIDENCE)
    with open_ledger(data_dir) as ledger:
        ledger.start_experiment("e", PACKET_ARMS, quota_brake=0.85)
    x, y, _ = work(data_dir=data_dir)
    assert x["status"] == y["status"] == "awaiting_approval", (x, y)
    # Each arm ran with its own skills: the named set against none.
    flags = {
        call["session_id"]: call["argv"]
        for call in fake_pi.calls()
        if (call["session_id"] or "").endswith("-implement")
    }
    assert str(skill) in flags[f"{y['run_id']}-implement"]
    assert str(skill) not in flags[f"{x['run_id']}-implement"]
    # What a reviewer may well write: where it looked, and what it was told.
    leaks = {
        "run id": x["run_id"],
        "short run id": x["run_id"][:8],
        "session": f"{x['run_id']}-review",
        "worktree": x["worktree_path"],
        "branch": x["branch_name"],
        "run dir": str(RunDir(x["run_id"], data_dir).path),
        "model": "other-provider/arm-b",
        "skill": str(skill / "SKILL.md"),
        "home": str(Path.home() / "notes.md"),
    }
    review_md = RunDir(x["run_id"], data_dir).review_md
    review_md.write_text(
        "\n".join(f"Seen {name}: {value} here." for name, value in leaks.items())
        + f"\nRead {x['worktree_path']}/done.txt.\n\n"
        + review_md.read_text()
    )
    with open_ledger(data_dir) as ledger:
        ledger.append_event(
            x["run_id"],
            "helmraiser",
            "review_verdict",
            {
                "verdict": "pass",
                "findings": [
                    {
                        "severity": "minor",
                        "file": f"{x['worktree_path']}/done.txt",
                        "line": 1,
                        "note": f"written by other-provider/arm-b in {x['run_id']}",
                    }
                ],
                "evidence_checks": [{"name": "shot", "meets_criteria": True, "note": "says ok"}],
            },
        )
    names = model_names(data_dir)

    result = invoke("pick-packet", "fix-widget", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    path = data_dir / "pairs" / re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-") / "packet.md"
    assert result.stdout.strip() == str(path)
    packet = path.read_text()
    # The brief, without its frontmatter, then X and then Y.
    assert packet.startswith("# Pick packet: Fix the widget\n")
    assert "## The task\n\n## Goal\n\nMake the widget work.\n" in packet
    assert "max_cost_usd" not in packet and "policy:" not in packet
    assert packet.index("\n## X\n") < packet.index("\n## Y\n")
    section = packet[packet.index("\n## X\n") : packet.index("\n## Y\n")]
    for line in (
        "- outcome: awaiting merge approval",
        "- review verdict: pass",
        "iterations: 1",
        "### Verify (X)\n\n- marker: pass",
        "### Evidence (X)\n\n- shot: pass\n  - criteria: The shot says ok.",
        "The change does what the task asks.",
        "- [minor] done.txt:1 — written by hidden in <run>",
        "- evidence shot: meets_criteria=true — says ok",
        "Read done.txt.",
        "Seen run id: <run> here.",
        "Seen short run id: <run> here.",
        "Seen session: <run>-review here.",
        "Seen worktree: <worktree> here.",
        "Seen branch: <branch> here.",
        "Seen run dir: <data-dir>/runs/<run> here.",
        "Seen model: hidden here.",
        "Seen skill: hidden here.",
        "Seen home: ~/notes.md here.",
        "### Diff (X)\n\n```diff\ndiff --git a/done.txt b/done.txt",
        "+ok\n```",
    ):
        assert line in section, line
    assert "- cost: $0.0700; minutes: " in section
    assert "- no findings" in packet[packet.index("\n## Y\n") :]
    # Nothing of the setup, of the runs' identity or of the machine.
    assert "EVIDENCE-TAIL" not in packet and '"verdict"' not in packet
    assert not [name for name in names if name in packet]
    assert "lean-set" not in packet and "arm" not in packet.lower()
    for run in (x, y):
        for value in (run["run_id"], run["run_id"][:8], run["branch_name"], run["worktree_path"]):
            assert value not in packet
    for root in (tmp_path, data_dir, Path.home(), skills.SKILL_SETS_ROOT):
        assert str(root) not in packet
    paths = set(re.findall(r"(?<![\w.<>~])/[\w.-]+(?:/[\w.-]+)+", packet))
    assert paths <= {"/dev/null"}, paths

    out = tmp_path / "elsewhere" / "p.md"
    custom = invoke("pick-packet", x["run_id"], "--out", str(out), "--json", data_dir=data_dir)

    assert custom.exit_code == 0, custom.output
    assert json.loads(custom.stdout) == {
        "task_id": task_id,
        "path": str(out),
        "bytes": len(packet.encode()),
    }
    assert out.read_text() == packet
    # Writing the packet reveals nothing and decides nothing.
    with open_ledger(data_dir) as ledger:
        pair = ledger.pair_for_task(task_id)
        assert (pair["pick"], pair["unblinded"]) == (None, False)


def test_pick_packet_is_refused_until_the_pair_can_be_picked_and_after_its_pick(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    script(pi)
    work_once(data_dir)

    under_way = invoke("pick-packet", task_id, "--json", data_dir=data_dir)

    assert under_way.exit_code == 1, under_way.output
    error = json.loads(under_way.stdout)["error"]
    assert "cannot be picked yet: arm X is awaiting merge approval; arm Y is resuming" in error
    assert not (data_dir / "pairs").exists()

    work_once(data_dir)
    assert invoke("pick-packet", task_id, data_dir=data_dir).exit_code == 0
    assert invoke("pick", task_id, "X", *PICK, "--json", data_dir=data_dir).exit_code == 0
    picked = invoke("pick-packet", task_id, "--json", data_dir=data_dir)

    assert picked.exit_code == 1, picked.output
    assert "is already picked (X, decided by judged)" in json.loads(picked.stdout)["error"]


def test_pick_packet_is_refused_without_a_pair(tmp_project, data_dir, pi, reading):  # noqa: F811
    task_id = queue_task(tmp_project, data_dir)
    outcome, _summary = work(data_dir=data_dir)

    for ref in (task_id, outcome["run_id"]):
        unpaired = invoke("pick-packet", ref, "--json", data_dir=data_dir)
        assert unpaired.exit_code == 1, unpaired.output
        assert "has no pair" in json.loads(unpaired.stdout)["error"]


def test_pick_packet_is_refused_when_both_arms_failed(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    run_arms(data_dir, pi, x=False, y=False)

    failed = invoke("pick-packet", task_id, "--json", data_dir=data_dir)

    assert failed.exit_code == 1, failed.output
    assert "cannot be picked yet: arm X is failed" in json.loads(failed.stdout)["error"]


# ── rework ────────────────────────────────────────────────────────────


def test_rework_is_refused_before_the_pick_and_allowed_after_it(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    script(pi)
    x = work_once(data_dir)

    # Whatever the arm's state: one awaits its merge approval, the other has not begun.
    with open_ledger(data_dir) as ledger:
        y_id = ledger.pair_for_task(task_id)["run_y"]
    for run_id in (x["run_id"], y_id):
        refused = invoke("rework", run_id, "--note", "tidy up", "--json", data_dir=data_dir)
        assert refused.exit_code == 2, refused.output
        assert json.loads(refused.stdout)["error"] == (
            "this run is one arm of a pair; pick first, then rework the winner "
            f"(helmraiser pick {task_id})"
        )
    with open_ledger(data_dir) as ledger:
        assert ledger.reworks(x["run_id"]) == []

    # Y fails: X is picked by failure, stays undelivered and is an ordinary run.
    script(pi, good=False)
    assert work_once(data_dir)["status"] == "failed"
    allowed = invoke("rework", x["run_id"], "--note", "tidy up", "--json", data_dir=data_dir)

    assert allowed.exit_code == 0, allowed.output
    assert json.loads(allowed.stdout)["status"] == "resuming"
    with open_ledger(data_dir) as ledger:
        assert [r["note"] for r in ledger.reworks(x["run_id"])] == ["tidy up"]


# ── pick ──────────────────────────────────────────────────────────────


def test_pick_reports_a_pull_request_as_opened_not_as_landed(
    tmp_project, tmp_path, data_dir, pi, reading, monkeypatch  # noqa: F811
):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(tmp_project, "remote", "add", "origin", str(remote))
    bin_dir = tmp_path / "gh-bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\necho https://example.test/pr/1\n", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    task_id = queue_task(tmp_project, data_dir, extra="delivery: pr\n")
    start_experiment(data_dir)
    x, _y, _ = work(data_dir=data_dir)

    result = invoke("pick", task_id, "X", *PICK, data_dir=data_dir)

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert f"  X PR opened: run {x['run_id']} → https://example.test/pr/1" in lines
    assert f"  it is not merged yet: helmraiser land {x['run_id']}" in lines
    assert not [line for line in lines if "landed" in line]
