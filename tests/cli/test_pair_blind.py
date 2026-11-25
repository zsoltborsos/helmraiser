"""An unpicked pair is blind: ``show``, ``log``, ``status``, ``wait`` and ``attach``
hide which arm, models and skills a run used until the pick, ``--reveal`` shows them
and marks the pair, and ``show <task>`` and ``diff <task> X|Y`` work on the pair."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from helmraiser.ledger.rundir import RunDir
from helmraiser.loop.status import status_path
from tests.cli.test_experiment_forking import (  # noqa: F401 -- shared fixtures
    ARMS,
    cli_env,
    data_dir,
    queue_task,
    reading,
    start_experiment,
    work,
)
from tests.cli.test_pair_lifecycle import (  # noqa: F401 -- shared fixtures
    JUDGE,
    PICK,
    REASON,
    git,
    invoke,
    open_ledger,
    pi,
    run_arms,
    script,
    work_once,
)
from tests.cli.test_pick_packet import model_names

HIDDEN_MODELS = dict.fromkeys(("plan", "implement", "fallback", "review"), "hidden")
HIDDEN_ARM = dict.fromkeys(("name", "overrides", "replaced"), "hidden")
HIDDEN_REVEAL = {label: {"arm": "hidden", "overrides": "hidden"} for label in ("X", "Y")}
# ``reading`` puts arm B behind X: only X ran with this model.
ARM_B = "other-provider/arm-b"


def events_of(run_id: str, data_dir: Path, *args: str) -> list[dict[str, Any]]:  # noqa: F811
    result = invoke("log", run_id, "--json", *args, data_dir=data_dir)
    assert result.exit_code == 0, result.output
    return [json.loads(line) for line in result.stdout.splitlines()]


# ── blinding ──────────────────────────────────────────────────────────


def test_show_log_and_wait_hide_a_runs_setup_until_the_pick(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    x, y, _ = work(data_dir=data_dir)
    names = model_names(data_dir)
    assert {ARM_B, "arm-b", "other-provider", "openai-codex"} <= names

    for label, run in (("X", x), ("Y", y)):
        run_id = run["run_id"]
        shown = invoke("show", run_id, "--json", data_dir=data_dir)
        assert shown.exit_code == 0, shown.output
        payload = json.loads(shown.stdout)
        assert payload["models"] == HIDDEN_MODELS and payload["arm"] == HIDDEN_ARM
        assert payload["pair"] == {
            "task_id": task_id,
            "label": label,
            "picked": False,
            "awaiting_pick": True,
            "unblinded": False,
        }
        human = invoke("show", run_id, data_dir=data_dir).output
        assert "models: plan=hidden implement=hidden fallback=hidden review=hidden" in human
        assert "arm: hidden" in human and f"pair: {label} of {task_id} (unpicked)" in human

        events = events_of(run_id, data_dir)
        by_type = {event["type"]: event for event in events}
        assert by_type["experiment_arm"]["payload"] == HIDDEN_ARM
        assert by_type["iteration_start"]["payload"]["model"] == "hidden"
        assert by_type["iteration_start"]["payload"]["n"] == 1
        sessions = [event for event in events if event["source"] == "pi"]
        assert {event["type"] for event in sessions} >= {"message_end", "agent_end"}
        for event in sessions:
            payload = event["payload"]
            assert all(value == "hidden" for key, value in payload.items() if key != "type")
        # What the run did stays: its verify result and its review verdict.
        assert by_type["verify_result"]["payload"]["passed"] is True
        assert by_type["review_verdict"]["payload"]["verdict"] == "pass"
        timeline = invoke("log", run_id, data_dir=data_dir).output
        assert "experiment_arm" in timeline and 'name="hidden"' in timeline

        waited = invoke("wait", run_id, "--json", data_dir=data_dir)
        assert waited.exit_code == 0, waited.output
        for text in (shown.stdout, human, json.dumps(events), timeline, waited.stdout):
            assert not [name for name in names if name in text]

    # The status output, its file and the report name no model of arm B either.
    status = invoke("status", "--json", data_dir=data_dir)
    report = invoke("report", "--models", "--by", "setup", "--json", data_dir=data_dir)
    for text in (
        status.stdout,
        status_path(data_dir).read_text(),
        invoke("status", data_dir=data_dir).output,
        report.stdout,
        invoke("report", "--models", data_dir=data_dir).output,
    ):
        assert "other-provider" not in text and "arm-b" not in text
    assert set(json.loads(status.stdout)["spend"]) == {"hidden"}

    assert invoke("pick", task_id, "X", *PICK, "--json", data_dir=data_dir).exit_code == 0

    assert (
        "other-provider"
        in json.loads(invoke("status", "--json", data_dir=data_dir).stdout)["spend"]
    )
    after = json.loads(invoke("report", "--models", "--json", data_dir=data_dir).stdout)
    assert ARM_B in after["cost"]["by_model"] and ARM_B in {row["model"] for row in after["models"]}
    payload = json.loads(invoke("show", x["run_id"], "--json", data_dir=data_dir).stdout)
    assert payload["models"]["implement"] == ARM_B
    assert payload["arm"] == {
        "name": "B",
        "overrides": ARMS["B"],
        "replaced": payload["arm"]["replaced"],
    }
    assert payload["pair"]["picked"] is True
    human = invoke("show", x["run_id"], data_dir=data_dir).output
    assert f"implement={ARM_B}" in human and "arm: B {" in human
    events = {event["type"]: event for event in events_of(x["run_id"], data_dir)}
    assert events["experiment_arm"]["payload"]["name"] == "B"
    assert events["iteration_start"]["payload"]["model"] == ARM_B
    assert isinstance(events["message_end"]["payload"]["message"], dict)
    other = json.loads(invoke("show", y["run_id"], "--json", data_dir=data_dir).stdout)
    assert other["arm"]["name"] == "A" and other["models"]["implement"] != "hidden"


def test_status_hides_a_runs_setup_until_the_pick(tmp_project, data_dir, pi, reading):  # noqa: F811
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    # Both arms fail, with a reason that names X's model.
    x, y = run_arms(data_dir, pi, x=False, y=False)
    with open_ledger(data_dir) as ledger:
        ledger.update_run(x["run_id"], failure_reason=f"pi_contract: {ARM_B} gave no usage")

    def views() -> tuple[dict[str, Any], str, str]:
        result = invoke("status", "--json", data_dir=data_dir)
        assert result.exit_code == 0, result.output
        human = invoke("status", data_dir=data_dir).output
        return json.loads(result.stdout), status_path(data_dir).read_text(), human

    status, written, human = views()
    reasons = {row["run_id"]: row["reason"] for row in status["failed"]}
    assert reasons[x["run_id"]] == "pi_contract: hidden gave no usage"
    # Spend per provider would tell the arms apart by their cost.
    assert "hidden" in status["spend"] and "other-provider" not in status["spend"]
    for text in (json.dumps(status), written, human):
        assert "other-provider" not in text and "arm-b" not in text
    shown = json.loads(invoke("show", x["run_id"], "--json", data_dir=data_dir).stdout)
    assert shown["failure_reason"] == "pi_contract: hidden gave no usage"
    waited = invoke("wait", x["run_id"], "--json", data_dir=data_dir)
    assert json.loads(waited.stdout)["failure_reason"] == "pi_contract: hidden gave no usage"
    refused = invoke("pick", task_id, "X", *PICK, "--json", data_dir=data_dir)
    assert refused.exit_code == 1 and ARM_B not in refused.output
    assert "arm X cannot be delivered: it is failed (pi_contract: hidden gave no usage)" in (
        json.loads(refused.stdout)["error"]
    )

    # Once the task has run again, without its pair, the pair's runs stay blinded.
    report = json.loads(invoke("report", "--models", "--json", data_dir=data_dir).stdout)
    assert set(report["cost"]["by_model"]) == {"hidden"}
    assert [row["model"] for row in report["models"]] == ["hidden"]
    assert ARM_B not in json.dumps(report)


def test_reveal_shows_the_setup_and_marks_the_pair_unblinded(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    experiment_id = start_experiment(data_dir)
    x, y, _ = work(data_dir=data_dir)

    shown = invoke("show", x["run_id"], "--json", "--reveal", data_dir=data_dir)

    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.stdout)
    assert payload["models"]["implement"] == ARM_B and payload["arm"]["name"] == "B"
    assert payload["pair"]["unblinded"] is True and payload["pair"]["picked"] is False
    assert shown.stderr == f"warning: the pair of task {task_id} is now unblinded\n"
    with open_ledger(data_dir) as ledger:
        assert ledger.get_pair(experiment_id, task_id)["unblinded"] is True
    # The mark is the pair's; without --reveal the setup stays hidden.
    again = json.loads(invoke("show", x["run_id"], "--json", data_dir=data_dir).stdout)
    assert again["models"] == HIDDEN_MODELS and again["pair"]["unblinded"] is True
    assert "(unpicked, unblinded)" in invoke("show", y["run_id"], data_dir=data_dir).output

    revealed = {event["type"]: event for event in events_of(y["run_id"], data_dir, "--reveal")}
    assert revealed["experiment_arm"]["payload"]["name"] == "A"
    assert revealed["iteration_start"]["payload"]["model"] != "hidden"

    picked = invoke("pick", task_id, "Y", *PICK, data_dir=data_dir)
    assert picked.exit_code == 0, picked.output
    assert "  the pair was unblinded before this pick (--reveal)" in picked.output.splitlines()
    report = invoke("experiment", "report", "--json", data_dir=data_dir)
    assert [reason["unblinded"] for reason in json.loads(report.stdout)["reasons"]] == [True]
    human = invoke("experiment", "report", data_dir=data_dir).output
    assert f"reason: {REASON}; judge: {JUDGE}; unblinded" in human


def test_log_reveal_marks_the_pair_and_a_pick_reports_it(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    x, _y, _ = work(data_dir=data_dir)

    result = invoke("log", x["run_id"], "--reveal", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    assert f"warning: the pair of task {task_id} is now unblinded" in result.stderr
    assert 'name="B"' in result.stdout
    picked = invoke("pick", task_id, "X", *PICK, "--json", data_dir=data_dir)
    assert json.loads(picked.stdout)["unblinded"] is True
    # A picked pair has nothing left to reveal: no warning, no mark to set.
    after = invoke("show", x["run_id"], "--json", "--reveal", data_dir=data_dir)
    assert after.exit_code == 0 and after.stderr == ""


def test_attach_is_refused_for_a_blinded_run_without_reveal(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    experiment_id = start_experiment(data_dir)
    x, _y, _ = work(data_dir=data_dir)

    refused = invoke("attach", x["run_id"], "--json", data_dir=data_dir)

    assert refused.exit_code == 2, refused.output
    assert "is one arm of an unpicked pair" in json.loads(refused.stdout)["error"]
    with open_ledger(data_dir) as ledger:
        assert ledger.get_pair(experiment_id, task_id)["unblinded"] is False

    revealed = invoke("attach", x["run_id"], "--json", "--reveal", data_dir=data_dir)

    # Past the refusal: the scripted pi wrote no session file to open.
    assert "unpicked pair" not in revealed.stdout and "is now unblinded" in revealed.stderr
    with open_ledger(data_dir) as ledger:
        assert ledger.get_pair(experiment_id, task_id)["unblinded"] is True


# ── show <task> ───────────────────────────────────────────────────────


def test_show_of_a_task_puts_its_pair_side_by_side(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    script(pi)
    x = work_once(data_dir)
    # Y changes a file that verify does not want: it fails before its merge decision,
    # and X is picked by failure, undelivered.
    pi.write_script({"sessions": {"*-implement": {"text": "Done.", "files": [["no.txt", "x\n"]]}}})
    y = work_once(data_dir)
    assert (x["status"], y["status"]) == ("awaiting_approval", "failed")

    result = invoke("show", "fix-widget", "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert (payload["task_id"], payload["title"], payload["project"]) == (
        task_id,
        "Fix the widget",
        str(tmp_project),
    )
    pair = payload["pair"]
    assert (pair["state"], pair["picked"], pair["awaiting_pick"], pair["unblinded"]) == (
        "picked",
        True,
        False,
        False,
    )
    assert (pair["pick"], pair["decided_by"], pair["judge"]) == ("X", "failure", "helmraiser")
    assert pair["reason"].startswith("other arm failed: verify_") and pair["picked_at"]
    assert pair["reveal"] == {
        "X": {"arm": "B", "overrides": ARMS["B"]},
        "Y": {"arm": "A", "overrides": {}},
    }
    minutes = pair["x"].pop("minutes")
    assert isinstance(minutes, float) and minutes >= 0
    assert pair["x"] == {
        "run_id": x["run_id"],
        "status": "awaiting_approval",
        "stage": None,
        "pending_approval": "merge",
        "failure_reason": None,
        "verdict": "pass",
        "findings": 0,
        "verify": "pass",
        "evidence": None,
        "cost_usd": 0.07,
        "iterations": 1,
        "branch_name": x["branch_name"],
        "pr_url": None,
    }
    assert (pair["y"]["status"], pair["y"]["verify"], pair["y"]["verdict"]) == (
        "failed",
        "fail",
        None,
    )
    assert pair["y"]["findings"] is None and pair["y"]["iterations"] >= 1
    assert pair["y"]["failure_reason"].startswith("verify_")

    human = invoke("show", task_id, data_dir=data_dir).output.splitlines()
    assert human[0] == f"task: {task_id}"
    assert human[1] == "pair: picked X (failure), judged by helmraiser"
    width = len(x["branch_name"])  # the widest cell under X
    assert f"{'':<12}{'X':<{width}}  Y" in human
    assert f"{'run':<12}{x['run_id']:<{width}}  {y['run_id']}" in human
    assert f"{'status':<12}{'awaiting_approval':<{width}}  failed" in human
    assert f"{'verify':<12}{'pass':<{width}}  fail" in human
    assert f"{'evidence':<12}{'-':<{width}}  -" in human
    assert 'X: arm B {"models": {"implement": {"model": "other-provider/arm-b"}}}' in human
    assert "Y: arm A {}" in human
    # A run of the pair is shown as a run.
    assert "pair" in json.loads(invoke("show", x["run_id"], "--json", data_dir=data_dir).stdout)
    assert "limits" in json.loads(invoke("show", x["run_id"], "--json", data_dir=data_dir).stdout)


def test_show_of_a_task_keeps_an_unpicked_pair_blind(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    experiment_id = start_experiment(data_dir)
    x, y, _ = work(data_dir=data_dir)
    names = model_names(data_dir)

    result = invoke("show", task_id, "--json", data_dir=data_dir)

    assert result.exit_code == 0, result.output
    pair = json.loads(result.stdout)["pair"]
    assert (pair["state"], pair["picked"], pair["awaiting_pick"]) == ("awaiting_pick", False, True)
    assert pair["reveal"] == HIDDEN_REVEAL
    assert (pair["pick"], pair["decided_by"], pair["reason"], pair["judge"]) == (None,) * 4
    assert (pair["x"]["run_id"], pair["y"]["run_id"]) == (x["run_id"], y["run_id"])
    human = invoke("show", task_id, data_dir=data_dir).output
    assert "pair: unpicked, awaiting pick" in human
    assert "X: arm hidden" in human and "Y: arm hidden" in human
    assert f"next: helmraiser pick-packet {task_id}, then helmraiser pick {task_id}" in human
    for text in (result.stdout, human):
        assert not [name for name in names if name in text]

    revealed = invoke("show", task_id, "--json", "--reveal", data_dir=data_dir)

    assert revealed.exit_code == 0, revealed.output
    pair = json.loads(revealed.stdout)["pair"]
    assert pair["reveal"]["X"] == {"arm": "B", "overrides": ARMS["B"]}
    assert pair["unblinded"] is True and pair["picked"] is False
    assert "is now unblinded" in revealed.stderr
    with open_ledger(data_dir) as ledger:
        assert ledger.get_pair(experiment_id, task_id)["unblinded"] is True


# ── diff ──────────────────────────────────────────────────────────────


def test_diff_by_task_and_label_and_by_run(tmp_project, data_dir, pi, reading):  # noqa: F811
    task_id = queue_task(tmp_project, data_dir)
    start_experiment(data_dir)
    x, y, _ = work(data_dir=data_dir)
    patch = RunDir(x["run_id"], data_dir).diff_patch
    written = patch.read_text()
    assert "+ok" in written and "done.txt" in written

    by_label = invoke("diff", task_id, "x", data_dir=data_dir)
    assert by_label.exit_code == 0 and by_label.stdout == written
    by_run = invoke("diff", y["run_id"], "--json", data_dir=data_dir)
    assert json.loads(by_run.stdout) == {
        "run_id": y["run_id"],
        "task_id": task_id,
        "label": None,
        "base_commit": y["base_commit"],
        "source": "patch",
        "diff": RunDir(y["run_id"], data_dir).diff_patch.read_text(),
    }

    # Without the patch file the diff is git's, from the recorded commits.
    patch.unlink()
    from_git = json.loads(invoke("diff", "fix-widget", "X", "--json", data_dir=data_dir).stdout)
    assert (from_git["source"], from_git["label"], from_git["run_id"]) == ("git", "X", x["run_id"])
    assert from_git["diff"] == written
    # Without a recorded head, from the run's branch.
    with open_ledger(data_dir) as ledger:
        ledger.update_run(x["run_id"], head_commit=None)
    assert invoke("diff", x["run_id"], data_dir=data_dir).stdout == written

    missing = invoke("diff", task_id, "--json", data_dir=data_dir)
    assert missing.exit_code == 2 and "X|Y" in json.loads(missing.stdout)["error"]
    wrong = invoke("diff", task_id, "Z", "--json", data_dir=data_dir)
    assert wrong.exit_code == 2 and "X|Y" in json.loads(wrong.stdout)["error"]
    extra = invoke("diff", y["run_id"], "Y", "--json", data_dir=data_dir)
    assert extra.exit_code == 2 and "is a run" in json.loads(extra.stdout)["error"]
    unknown = invoke("diff", "no-such-task", "--json", data_dir=data_dir)
    assert unknown.exit_code == 1 and "unknown ref" in json.loads(unknown.stdout)["error"]
    with open_ledger(data_dir) as ledger:
        ledger.update_run(x["run_id"], base_commit=None)
    gone = invoke("diff", x["run_id"], "--json", data_dir=data_dir)
    assert gone.exit_code == 1 and "no base commit" in json.loads(gone.stdout)["error"]


def test_diff_of_a_task_without_a_pair_is_its_latest_runs(
    tmp_project, data_dir, pi, reading  # noqa: F811
):
    task_id = queue_task(tmp_project, data_dir)
    outcome, _summary = work(data_dir=data_dir)

    result = invoke("diff", task_id, data_dir=data_dir)

    assert result.exit_code == 0, result.output
    assert result.stdout == RunDir(outcome["run_id"], data_dir).diff_patch.read_text()
    labelled = invoke("diff", task_id, "X", "--json", data_dir=data_dir)
    assert labelled.exit_code == 2
    assert "a task without a pair" in json.loads(labelled.stdout)["error"]
