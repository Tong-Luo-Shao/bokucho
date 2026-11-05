"""Names a LiveMaker game looks up in a table it loads from a TSV file."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("livemaker")

from livemaker.lsb.novel import TWdOpeHistChar  # noqa: E402

from bokucho.engines.cp932_proxy import ProxyMap  # noqa: E402
from bokucho.engines.livemaker import LiveMakerAdapter  # noqa: E402

TABLE = "データ/人物/人物.TXT"
ROWS = "画像\t文字列\r\n001★主役.gal\tハルカ\\n\r\n端役\\200◆◆男A.gal\t男A\\n\r\n"
# How the backlog names who speaks: the name the speaker's plate picture stands for.
BY_PLATE = 'DBDirectGetStr("DB人物表","画像","001★主役.gal",FALSE,FALSE,"文字列","")'
BY_NAME = 'DBDirectGetStr("DB人物表","文字列","ハルカ",FALSE,FALSE,"画像","")'


def _adapter(
    monkeypatch, tmp_path: Path, livemaker_script, *reads: str, runs=(), table=None
):
    script = livemaker_script("00000001.lsb")
    # The game spells it in lower case; the archive holds 人物.TXT.
    script.runs('DBLoadTsvFile("データ\\人物\\人物.txt")')
    for statement in runs:
        script.runs(statement)
    for read in reads:
        script.shows_in_text("名前").args["Text"].body[0] = TWdOpeHistChar(var_name=read)
    root = tmp_path / "game"
    (root / TABLE).parent.mkdir(parents=True)
    (root / TABLE).write_bytes(ROWS.encode("cp932") if table is None else table)
    (root / "00000001.lsb").write_bytes(b"stub")
    adapter = LiveMakerAdapter()
    monkeypatch.setattr(adapter, "_open", lambda path: script.lsb)
    monkeypatch.setattr(adapter, "_serialize_validated", lambda lsb: b"")
    return adapter, root


def _table_units(adapter, root: Path) -> dict:
    return {unit.id: unit for unit in adapter.extract(root) if unit.id.startswith("pylm:tsv:")}


def test_a_name_the_game_looks_up_in_a_table_is_a_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, livemaker_script
) -> None:
    adapter, root = _adapter(monkeypatch, tmp_path, livemaker_script, BY_PLATE)

    units = _table_units(adapter, root)

    assert {id_: unit.source for id_, unit in units.items()} == {
        f"pylm:tsv:{TABLE}:1:文字列": "ハルカ",
        f"pylm:tsv:{TABLE}:2:文字列": "男A",
    }
    assert {unit.engine_ref["file"] for unit in units.values()} == {TABLE}


def test_a_column_the_game_also_looks_rows_up_by_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, livemaker_script
) -> None:
    adapter, root = _adapter(monkeypatch, tmp_path, livemaker_script, BY_PLATE, BY_NAME)

    assert _table_units(adapter, root) == {}


def test_a_translated_name_is_written_into_the_table_as_the_game_stores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, livemaker_script
) -> None:
    adapter, root = _adapter(monkeypatch, tmp_path, livemaker_script, BY_PLATE)
    units = _table_units(adapter, root)
    units[f"pylm:tsv:{TABLE}:1:文字列"].target = "晴朵子"
    adapter.proxy = ProxyMap({"朵": "丐"})

    adapter.inject(list(units.values()), root, tmp_path / "out")

    written = (tmp_path / "out" / TABLE).read_bytes().decode("cp932")
    assert written == ROWS.replace("ハルカ", "晴丐子")


def test_a_table_that_changed_since_extraction_stops_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, livemaker_script
) -> None:
    adapter, root = _adapter(monkeypatch, tmp_path, livemaker_script, BY_PLATE)
    units = _table_units(adapter, root)
    units[f"pylm:tsv:{TABLE}:1:文字列"].target = "遙香"
    (root / TABLE).write_bytes(ROWS.replace("ハルカ", "ハルコ").encode("cp932"))

    with pytest.raises(ValueError, match=f"pylm:tsv:{TABLE}:1:文字列"):
        adapter.inject(list(units.values()), root, tmp_path / "out")


@pytest.mark.parametrize(
    "lookup",
    [
        'DBLocate("DB人物表","文字列","ハルカ")',
        'DBFindFirst("DB人物表","文字列","ハルカ")',
        'DBDirectGetInt("DB人物表","文字列","ハルカ",FALSE,FALSE,"画像",0)',
        'DBSort("DB人物表","文字列",TRUE)',
    ],
)
def test_a_column_any_lookup_finds_rows_by_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, livemaker_script, lookup: str
) -> None:
    adapter, root = _adapter(monkeypatch, tmp_path, livemaker_script, BY_PLATE, runs=[lookup])

    assert _table_units(adapter, root) == {}


def test_cells_left_alone_keep_their_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, livemaker_script
) -> None:
    # 0xFBFC is IBM's 髙; CP932 encodes the character back as NEC's 0xEEE0.
    table = ROWS.encode("cp932").replace("端役".encode("cp932"), b"\xfb\xfc")
    adapter, root = _adapter(monkeypatch, tmp_path, livemaker_script, BY_PLATE, table=table)
    units = _table_units(adapter, root)
    units[f"pylm:tsv:{TABLE}:1:文字列"].target = "遙香"

    adapter.inject(list(units.values()), root, tmp_path / "out")

    assert (tmp_path / "out" / TABLE).read_bytes() == table.replace(
        "ハルカ".encode("cp932"), "遙香".encode("cp932")
    )


def test_a_table_that_is_not_cp932_text_is_skipped_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, livemaker_script, caplog
) -> None:
    table = ROWS.encode("cp932") + b"\x81\t\x81\r\n"
    adapter, root = _adapter(monkeypatch, tmp_path, livemaker_script, BY_PLATE, table=table)

    assert _table_units(adapter, root) == {}
    assert "人物.TXT" in caplog.text


@pytest.mark.parametrize("target", ["遙\t香", "遙\n香", "遙\r\n香"])
def test_a_name_holding_a_tab_or_a_line_break_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, livemaker_script, target: str
) -> None:
    adapter, root = _adapter(monkeypatch, tmp_path, livemaker_script, BY_PLATE)
    units = _table_units(adapter, root)
    units[f"pylm:tsv:{TABLE}:1:文字列"].target = target

    with pytest.raises(ValueError, match="tab or a line break"):
        adapter.inject(list(units.values()), root, tmp_path / "out")
