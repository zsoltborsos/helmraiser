"""The suite's implicit data/config paths must never reach the owner's files."""

from helmraiser import config
from helmraiser.ledger.db import default_data_dir


def test_default_paths_are_temporary(tmp_path):
    assert default_data_dir() == tmp_path / "helmraiser-data"
    config.load_config()
    assert config.CONFIG_FILE == tmp_path / "helmraiser-config" / "config.json"
    assert config.CONFIG_FILE.exists()


def test_default_data_dir_can_be_overridden(tmp_path, monkeypatch):
    override = tmp_path / "override"
    monkeypatch.setenv("HELMRAISER_DATA_DIR", str(override))
    assert default_data_dir() == override
