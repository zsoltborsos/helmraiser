"""Tests for Helmraiser config module."""

import copy
import importlib
import json
from pathlib import Path

import pytest

# Must import before patching env vars — conftest.py already sets HELMRAISER_CONFIG_DIR
# but we need to reload the module with the patched path.


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Redirect config to a temp directory for each test."""
    config_dir = tmp_path / ".helmraiser"
    config_dir.mkdir()
    monkeypatch.setenv("HELMRAISER_CONFIG_DIR", str(config_dir))

    # Reload the config module to pick up the new env var
    import helmraiser.config as config_module

    importlib.reload(config_module)

    yield config_module

    # No cleanup needed — tmp_path handles it


@pytest.mark.parametrize(
    "value", [None, "", "  ", {"thinking": "high"}, {"model": None}, {"model": "  "}]
)
def test_missing_models(isolated_config, value):
    config = {
        "profiles": {"implement": value, "review": "p/reviewer:high"},
        "routing": {"scout": {"model": value if isinstance(value, str) else None}},
    }
    assert isolated_config.missing_models(
        config, ["review", "implement", "plan", "routing.scout.model"]
    ) == ["implement", "plan", "routing.scout.model"]
    from helmraiser.task.loader import parse_task

    task = parse_task(
        "---\nid: t\nmodels:\n  implement: p/own\n  plan: p/planner\n---\n", Path("task.md"), config
    )
    assert isolated_config.missing_models(config, ["review", "implement", "plan"], task) == []


def test_example_config_loads_as_is(isolated_config):
    example = Path(__file__).resolve().parents[1] / "examples/config.json"
    original = example.read_text()
    isolated_config.CONFIG_FILE.write_text(original)
    expected = json.loads(original)
    loaded = isolated_config.load_config()
    assert loaded["profiles"] == expected["profiles"]
    assert loaded["routing"]["scout"]["model"] == expected["routing"]["scout"]["model"]
    assert loaded["_comment"] == expected["_comment"]
    assert loaded["subscriptions"]["opencode-go"]["warn_at"] == 0.6
    assert loaded["subscriptions"]["opencode-go"]["stop_at"] == 0.7
    assert isolated_config.CONFIG_FILE.read_text() == original


class TestLoadConfig:
    def test_creates_default_config_if_missing(self, isolated_config):
        """load_config should create a default config if the file doesn't exist."""
        config = isolated_config.load_config()

        assert config["profiles"] == isolated_config.DEFAULT_PROFILES
        written = json.loads(isolated_config.CONFIG_FILE.read_text())
        for value in (config, written):
            assert not any(p.get("model") for p in value["profiles"].values())
            assert value["routing"]["scout"]["model"] is None
        assert config["budget"] == isolated_config.DEFAULT_BUDGET
        assert config["intake"] == {"avoid_models": []}
        config["intake"]["avoid_models"].append("p/m")
        assert isolated_config.DEFAULT_CONFIG["intake"]["avoid_models"] == []
        assert config["routing"] == isolated_config.DEFAULT_ROUTING
        assert config["routing"]["plan"] == {"enabled": False}
        assert config["routing"]["implement"] == {"enabled": False, "codex_headroom": 0.15}
        config["routing"]["scout"]["focus_projects"].append("/focus")
        assert isolated_config.DEFAULT_ROUTING["scout"]["focus_projects"] == []
        assert config["pi_bin"] == "pi"
        assert config["pi_rpc_implement"] is False
        assert config["worktree_root"] is None
        assert config["max_runs_per_project"] == 1
        assert config["base_sync_interval_seconds"] == 300
        assert "models" not in config
        assert "active_preset" not in config

        # File should now exist
        assert isolated_config.CONFIG_FILE.exists()
        assert config["subscriptions"] == {}
        assert json.loads(isolated_config.CONFIG_FILE.read_text())["subscriptions"] == {}

    @pytest.mark.parametrize(
        "data, expected",
        [({}, False), ({"pi_rpc_implement": False}, False), ({"pi_rpc_implement": True}, True)],
    )
    def test_rpc_implement_default_and_loading(self, isolated_config, data, expected):
        original = json.dumps(data)
        isolated_config.CONFIG_FILE.write_text(original)
        assert isolated_config.load_config()["pi_rpc_implement"] is expected
        assert isolated_config.CONFIG_FILE.read_text() == original

    def test_reads_existing_config(self, isolated_config):
        """load_config should read an existing config file."""
        data = {"pi_bin": "/opt/pi", "profiles": {"implement": {"model": "p/m"}}}
        isolated_config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(isolated_config.CONFIG_FILE, "w") as f:
            json.dump(data, f)

        config = isolated_config.load_config()
        assert config["pi_bin"] == "/opt/pi"
        assert config["profiles"]["implement"] == {"model": "p/m"}

    def test_fills_missing_keys_with_defaults(self, isolated_config):
        """load_config should add missing keys and missing profiles/budget entries."""
        isolated_config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(isolated_config.CONFIG_FILE, "w") as f:
            json.dump({"budget": {"max_cost_usd": 1.0}}, f)

        config = isolated_config.load_config()
        assert config["subscriptions"] == {}
        assert config["budget"]["max_cost_usd"] == 1.0
        assert (
            config["budget"]["max_iterations"] == isolated_config.DEFAULT_BUDGET["max_iterations"]
        )
        assert config["profiles"] == isolated_config.DEFAULT_PROFILES
        assert config["intake"] == {"avoid_models": []}
        assert config["base_sync_interval_seconds"] == 300

    @pytest.mark.parametrize(
        "subscriptions",
        [
            {"opencode-go": {"warn_at": 0.6}},
            {"openai-codex": {}},
            {"custom": {"nested": {"limit": 7}, "live": False}},
            {
                "opencode-go": {"warn_at": 0.6, "stop_at": 0.9},
                "openai-codex": {"warn_at": 0.7, "stop_at": 0.95},
            },
            {"opencode-go": None, "openai-codex": False},
        ],
    )
    def test_only_named_subscriptions_receive_template_keys(self, isolated_config, subscriptions):
        original = json.dumps({"subscriptions": subscriptions})
        isolated_config.CONFIG_FILE.write_text(original)
        expected = copy.deepcopy(subscriptions)
        for name, block in expected.items():
            if isinstance(block, dict):
                for key, value in isolated_config.SUBSCRIPTION_TEMPLATES.get(name, {}).items():
                    block.setdefault(key, copy.deepcopy(value))

        loaded = isolated_config.load_config()["subscriptions"]

        assert loaded == expected
        assert set(loaded) == set(subscriptions)
        assert isolated_config.CONFIG_FILE.read_text() == original

    @pytest.mark.parametrize("live", [None, False])
    @pytest.mark.parametrize(
        "nested",
        [
            {},
            {"monthly_limit_usd": {}, "window_share": {}, "week_reset": {}, "month_reset": {}},
            {
                "monthly_limit_usd": {"glm-5.3-flash": 8},
                "window_share": {"5h": 0.1},
                "week_reset": {"weekday": 2},
                "month_reset": {"day": 3},
            },
        ],
    )
    def test_subscription_overrides_are_not_recursively_filled(self, isolated_config, live, nested):
        overrides = {"live": live, **nested}
        original = json.dumps({"subscriptions": {"opencode-go": overrides}})
        isolated_config.CONFIG_FILE.write_text(original)
        loaded = isolated_config.load_config()["subscriptions"]["opencode-go"]
        for key, value in overrides.items():
            assert loaded[key] == value
        assert isolated_config.CONFIG_FILE.read_text() == original

    def test_filled_subscription_values_are_deep_copies(self, isolated_config):
        original = json.dumps({"subscriptions": {"opencode-go": {}, "openai-codex": {}}})
        isolated_config.CONFIG_FILE.write_text(original)
        templates = copy.deepcopy(isolated_config.SUBSCRIPTION_TEMPLATES)
        loaded = isolated_config.load_config()["subscriptions"]
        for value in loaded["opencode-go"].values():
            if isinstance(value, dict):
                value.clear()
        loaded["openai-codex"]["warn_at"] = 0.1
        assert isolated_config.SUBSCRIPTION_TEMPLATES == templates
        assert isolated_config.load_config()["subscriptions"] == templates
        assert isolated_config.CONFIG_FILE.read_text() == original

    @pytest.mark.parametrize(
        "data, expected",
        [
            ({}, {"avoid_models": []}),
            ({"intake": {}}, {"avoid_models": []}),
            ({"intake": {"avoid_models": []}}, {"avoid_models": []}),
            (
                {"intake": {"avoid_models": ["foo/*", "bar/baz"], "extra": "kept"}},
                {"avoid_models": ["foo/*", "bar/baz"], "extra": "kept"},
            ),
        ],
    )
    def test_intake_defaults_fill(self, isolated_config, data, expected):
        original = json.dumps(data)
        isolated_config.CONFIG_FILE.write_text(original)
        config = isolated_config.load_config()
        assert config["intake"] == expected
        assert isolated_config.CONFIG_FILE.read_text() == original
        config["intake"]["avoid_models"].append("p/m")
        assert isolated_config.DEFAULT_CONFIG["intake"]["avoid_models"] == []

    @pytest.mark.parametrize(
        "value", ["foo", None, {}, 1, True, [None], [1], [True], ["foo/*", {}]]
    )
    def test_invalid_avoid_models(self, isolated_config, value):
        isolated_config.CONFIG_FILE.write_text(json.dumps({"intake": {"avoid_models": value}}))
        with pytest.raises(ValueError, match=r"^intake\.avoid_models must be a list of strings$"):
            isolated_config.load_config()

    @pytest.mark.parametrize("value", [None, [], "foo", 1, True])
    def test_invalid_intake_block(self, isolated_config, value):
        isolated_config.CONFIG_FILE.write_text(json.dumps({"intake": value}))
        with pytest.raises(ValueError, match=r"^intake\.avoid_models must be a list of strings$"):
            isolated_config.load_config()

    @pytest.mark.parametrize(
        "data, expected",
        [
            ({}, {"failed_after_days": 7}),
            ({"cleanup": {}}, {"failed_after_days": 7}),
            ({"cleanup": {"failed_after_days": None}}, {"failed_after_days": None}),
            (
                {"cleanup": {"failed_after_days": 3, "extra": "kept"}},
                {"failed_after_days": 3, "extra": "kept"},
            ),
        ],
    )
    def test_cleanup_defaults_fill(self, isolated_config, data, expected):
        original = json.dumps(data)
        isolated_config.CONFIG_FILE.write_text(original)
        config = isolated_config.load_config()
        assert config["cleanup"] == expected
        assert isolated_config.CONFIG_FILE.read_text() == original
        config["cleanup"]["failed_after_days"] = 99
        assert isolated_config.DEFAULT_CONFIG["cleanup"] == {"failed_after_days": 7}

    @pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "7", [], {}])
    def test_invalid_cleanup_days(self, isolated_config, value):
        isolated_config.CONFIG_FILE.write_text(
            json.dumps({"cleanup": {"failed_after_days": value}})
        )
        with pytest.raises(
            ValueError, match="cleanup.failed_after_days must be a positive integer or null"
        ):
            isolated_config.load_config()

    @pytest.mark.parametrize("value", [None, [], "foo", 1, True])
    def test_invalid_cleanup_block(self, isolated_config, value):
        isolated_config.CONFIG_FILE.write_text(json.dumps({"cleanup": value}))
        with pytest.raises(
            ValueError, match="cleanup.failed_after_days must be a positive integer or null"
        ):
            isolated_config.load_config()

    def test_judge_profile_default_and_fill(self, isolated_config):
        expected = {"thinking": "high"}
        assert isolated_config.DEFAULT_CONFIG["profiles"]["judge"] == expected
        data = {"profiles": {"implement": {"model": "p/custom"}}}
        isolated_config.CONFIG_FILE.write_text(json.dumps(data))
        config = isolated_config.load_config()
        assert config["profiles"]["judge"] == expected
        assert config["profiles"]["implement"] == {"model": "p/custom"}
        data["profiles"]["judge"] = {"model": "p/judge", "thinking": "low"}
        isolated_config.CONFIG_FILE.write_text(json.dumps(data))
        assert isolated_config.load_config()["profiles"]["judge"] == data["profiles"]["judge"]

    @pytest.mark.parametrize("value", [True, False, "2", 2.0, 0, -1, None])
    def test_invalid_run_limit(self, isolated_config, value):
        isolated_config.CONFIG_FILE.write_text(json.dumps({"max_runs_per_project": value}))
        with pytest.raises(ValueError, match="max_runs_per_project"):
            isolated_config.load_config()

    @pytest.mark.parametrize("data, expected", [({}, 1), ({"max_runs_per_project": 2}, 2)])
    def test_run_limit_defaults_fill(self, isolated_config, data, expected):
        original = json.dumps(data)
        isolated_config.CONFIG_FILE.write_text(original)
        assert isolated_config.load_config()["max_runs_per_project"] == expected
        assert isolated_config.CONFIG_FILE.read_text() == original

    @pytest.mark.parametrize(
        "data, expected", [({}, None), ({"worktree_root": "/mnt/runs"}, "/mnt/runs")]
    )
    def test_worktree_root_defaults_fill(self, isolated_config, data, expected):
        original = json.dumps(data)
        isolated_config.CONFIG_FILE.write_text(original)
        assert isolated_config.load_config()["worktree_root"] == expected
        assert isolated_config.CONFIG_FILE.read_text() == original

    def test_preserves_base_sync_interval(self, isolated_config):
        isolated_config.CONFIG_FILE.write_text(json.dumps({"base_sync_interval_seconds": 42}))
        assert isolated_config.load_config()["base_sync_interval_seconds"] == 42

    def test_recursively_merges_partial_routing_config(self, isolated_config):
        data = {
            "pi_bin": "/opt/pi",
            "routing": {
                "target": 0.9,
                "scout": {
                    "enabled": False,
                    "focus_projects": ["/focus"],
                    "extra": "kept",
                },
            },
        }
        isolated_config.CONFIG_FILE.write_text(json.dumps(data))

        config = isolated_config.load_config()

        assert config["pi_bin"] == "/opt/pi"
        assert config["routing"]["target"] == 0.9
        assert config["routing"]["margin"] == 0.02
        assert config["routing"]["scout"]["enabled"] is False
        assert config["routing"]["scout"]["focus_projects"] == ["/focus"]
        assert config["routing"]["scout"]["paused_projects"] == []
        assert config["routing"]["scout"]["max_pending_drafts"] == 3
        assert config["routing"]["scout"]["extra"] == "kept"
        assert config["routing"]["plan"] == {"enabled": False}
        assert config["routing"]["implement"] == {"enabled": False, "codex_headroom": 0.15}

        config["routing"]["scout"]["paused_projects"].append("/paused")
        assert isolated_config.DEFAULT_ROUTING["scout"]["paused_projects"] == []

    @pytest.mark.parametrize("threshold", [None, 0.3])
    def test_preserves_explicit_dispatch_routing_settings(self, isolated_config, threshold):
        implement = {"enabled": True}
        if threshold is not None:
            implement["codex_headroom"] = threshold
        data = {"routing": {"plan": {"enabled": True}, "implement": implement}}
        original = json.dumps(data)
        isolated_config.CONFIG_FILE.write_text(original)

        config = isolated_config.load_config()

        assert config["routing"]["plan"] == {"enabled": True}
        assert config["routing"]["implement"] == {
            "enabled": True,
            "codex_headroom": 0.15 if threshold is None else threshold,
        }
        assert isolated_config.CONFIG_FILE.read_text() == original

    def test_recovers_from_corrupt_config_file(self, isolated_config):
        """An empty or invalid config.json is replaced with the defaults."""
        isolated_config.CONFIG_FILE.write_text("")

        config = isolated_config.load_config()

        assert config["profiles"] == isolated_config.DEFAULT_PROFILES
        content = json.loads(isolated_config.CONFIG_FILE.read_text())
        assert "budget" in content
        assert config["subscriptions"] == content["subscriptions"] == {}


class TestSaveConfig:
    def test_writes_valid_json(self, isolated_config):
        """save_config should write valid JSON to the config file."""
        data = {"key": "value", "nested": {"a": 1}}
        isolated_config.save_config(data)

        assert isolated_config.CONFIG_FILE.exists()
        with open(isolated_config.CONFIG_FILE) as f:
            loaded = json.load(f)
        assert loaded == data

    def test_creates_directory_if_missing(self, isolated_config, tmp_path):
        """save_config should create the config directory if it doesn't exist."""
        new_dir = tmp_path / "new" / "dir" / ".helmraiser"
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("HELMRAISER_CONFIG_DIR", str(new_dir))

        import helmraiser.config as config_module

        importlib.reload(config_module)

        config_module.save_config({"test": True})
        assert config_module.CONFIG_FILE.exists()
        monkeypatch.undo()
