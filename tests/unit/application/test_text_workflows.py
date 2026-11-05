from __future__ import annotations

from bokucho.application.text import list_units
from bokucho.model import Status, StringFile, TextUnit


def test_list_units_filters_status(new_project) -> None:
    project = new_project("test")
    project.save_string_file(
        StringFile(
            source_file="a.lsb",
            units=[
                TextUnit(id="u1", source="one"),
                TextUnit(id="u2", source="two", target="mt", status=Status.MT),
            ],
        ),
        "a.lsb.json",
    )

    assert [unit.id for _, unit in list_units(project, Status.MT)] == ["u2"]
