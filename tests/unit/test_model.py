"""Tests for the neutral data model."""

from bokucho.model import Status, TextUnit


def test_units_without_history_field_load_with_empty_history():
    raw = (
        '{"schema_version": 1, "id": "a.lsb#1", "source": "はい",'
        ' "target": "是", "status": "reviewed"}'
    )
    unit = TextUnit.model_validate_json(raw)
    assert unit.history == []


def test_a_file_written_when_approved_existed_still_loads():
    unit = TextUnit.model_validate(
        {
            "id": "x",
            "source": "s",
            "target": "t",
            "status": "approved",
            "history": [
                {"target": "t", "status": "approved", "at": "2026-07-22T00:00:00+00:00"}
            ],
        }
    )
    assert unit.status is Status.REVIEWED
    assert unit.is_protected is True
    assert unit.history[0].status is Status.REVIEWED
    assert '"status":"reviewed"' in unit.model_dump_json().replace(" ", "")
    assert "approved" not in unit.model_dump_json()
