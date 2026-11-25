"""Experiment definitions are validated before any ledger write."""

import copy
from datetime import UTC, datetime

import pytest

from helmraiser.loop.experiments import ExperimentError, validate_experiment
from helmraiser.pi import skills


def definition():
    return {"name": "trial", "arms": {"A": {}, "B": {}}}


def test_defaults_and_copy():
    source = definition()
    source["arms"]["A"] = {"models": {"implement": {"model": "p/a", "thinking": "high"}}}
    before = copy.deepcopy(source)
    result = validate_experiment(source, {})
    assert source == before
    assert result.end_pairs == 30
    assert result.quota_brake == 0.85
    assert result.end_at is None
    assert result.exclude_projects == ()
    source["arms"]["A"]["models"]["implement"]["model"] = "changed"
    assert result.arms == before["arms"]


@pytest.mark.parametrize(
    "field,value,key",
    [
        ("name", " ", "name"),
        ("extra", 1, "extra"),
        ("arms", [], "arms"),
        ("arms", {"A": {}}, "arms"),
        ("arms", {"A": {}, "C": {}}, "arms.C"),
        ("end_pairs", True, "end_pairs"),
        ("end_pairs", None, "end_pairs"),
        ("end_pairs", 0, "end_pairs"),
        ("end_pairs", -1, "end_pairs"),
        ("end_pairs", 1.5, "end_pairs"),
        ("end_pairs", "30", "end_pairs"),
        ("end_pairs", 9223372036854775808, "end_pairs"),
        ("quota_brake", True, "quota_brake"),
        ("quota_brake", 0, "quota_brake"),
        ("quota_brake", 1.1, "quota_brake"),
        ("quota_brake", float("nan"), "quota_brake"),
        ("quota_brake", float("inf"), "quota_brake"),
        ("quota_brake", None, "quota_brake"),
        ("end_at", "bad", "end_at"),
        ("end_at", 123, "end_at"),
        ("end_at", "0001-01-01T00:00:00+01:00", "end_at"),
        ("end_at", "9999-12-31T23:59:59.999999Z", "end_at"),
        ("exclude_projects", "path", "exclude_projects"),
        ("exclude_projects", [""], "exclude_projects.0"),
    ],
)
def test_invalid_definition(field, value, key):
    source = definition()
    source[field] = value
    with pytest.raises(ExperimentError, match=key):
        validate_experiment(source, {})


@pytest.mark.parametrize(
    "arm,key",
    [
        ([], "arms.A"),
        ({"unknown": 1}, "arms.A.unknown"),
        ({"models": []}, "arms.A.models"),
        ({"models": {"unknown": "p/a"}}, "arms.A.models.unknown"),
        *[
            ({"models": {"implement": value}}, "arms.A.models.implement")
            for value in [
                None,
                42,
                True,
                [],
                {},
                {"model": 4},
                {"model": ""},
                {"model": "p/a", "thinking": 3},
                {"model": "p/a", "extra": 1},
            ]
        ],
        *[
            ({"skills": value}, "arms.A.skills")
            for value in [1, None, [], "missing", "../x", "/tmp", ".", "..", "x/y", "x\\y"]
        ],
        ({"pi_rpc_implement": 1}, "arms.A.pi_rpc_implement"),
    ],
)
def test_invalid_arm(arm, key):
    source = definition()
    source["arms"]["A"] = arm
    with pytest.raises(ExperimentError, match=key):
        validate_experiment(source, {})


@pytest.mark.parametrize(
    "override",
    [{}, {"implement": "p/review"}, {"implement": "p/review:high", "review": "p/review:low"}],
)
def test_model_equality(override):
    source = definition()
    source["arms"]["A"]["models"] = override
    config = {
        "profiles": {"implement": "p/review" if not override else "p/impl", "review": "p/review"}
    }
    with pytest.raises(ExperimentError, match="arms.A.models.*reviewer must differ"):
        validate_experiment(source, config)


@pytest.mark.parametrize("text", ["2026-12-09", "2026-12-09T00:00:00", "2026-12-09T02:00:00+02:00"])
def test_full_definition(text, tmp_path):
    (skills.SKILL_SETS_ROOT / "named").mkdir(parents=True)
    source = definition()
    source.update(end_at=text, end_pairs=2, quota_brake=1, exclude_projects=[str(tmp_path)])
    source["arms"] = {
        "A": {"skills": "named", "pi_rpc_implement": False},
        "B": {"skills": "discover", "models": {"review": "p/other:high"}},
    }
    result = validate_experiment(source, {})
    assert result.end_at == datetime(2026, 12, 9, tzinfo=UTC).timestamp()
    assert result.exclude_projects == (str(tmp_path.resolve()),)
    assert result.arms["B"]["models"]["review"] == {"model": "p/other", "thinking": "high"}


@pytest.mark.parametrize("value", [None, [], "bad", 4])
def test_bad_top_level(value):
    with pytest.raises(ExperimentError, match="experiment"):
        validate_experiment(value, {})
