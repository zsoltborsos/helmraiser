import uuid

import pytest

from helmraiser.pipeline.run_identity import resolve_run_id


def test_omission_generates_canonical_uuid4():
    run_id = resolve_run_id()
    parsed = uuid.UUID(run_id)
    assert parsed.version == 4
    assert str(parsed) == run_id


def test_supplied_canonical_uuid4_is_preserved():
    run_id = "c61f2c67-9d53-43ee-8c9d-f8792766c680"
    assert resolve_run_id(run_id) == run_id


@pytest.mark.parametrize(
    "run_id",
    [
        "run-topic",
        "C61F2C67-9D53-43EE-8C9D-F8792766C680",
        "c61f2c67-9d53-13ee-8c9d-f8792766c680",
    ],
)
def test_rejects_noncanonical_or_non_v4_ids(run_id: str):
    with pytest.raises(ValueError, match="canonical UUID version 4"):
        resolve_run_id(run_id)
