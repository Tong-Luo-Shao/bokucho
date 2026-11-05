"""RED/GREEN tests for the strings domain bridge methods."""

from __future__ import annotations

from pathlib import Path

import pytest

from bokucho.application.projects import string_filename
from bokucho.model import Context, HistoryEntry, Status, StringFile, TextUnit
from bokucho.project import Project
from bokucho.sync.json_io import load_string_file
from bokucho.translate.glossary import Glossary, GlossaryEntry
from bokucho.webapp.api import Api


def _seed_project(
    api: Api, tmp_path: Path, name: str, string_files: list[StringFile]
) -> Project:
    destination = tmp_path / name
    created = api.create_project(
        name=name,
        engine="livemaker",
        destination=str(destination),
        source_lang="ja",
        target_lang="zh-Hant",
    )
    assert "error" not in created
    project = Project.load(created["root"])
    for string_file in string_files:
        project.save_string_file(string_file, string_filename(string_file.source_file))
    return project


def test_unit_rows_returns_slices_for_the_virtual_list(api: Api, tmp_path: Path) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "rows",
        [
            StringFile(
                source_file="a.lsb",
                units=[TextUnit(id="u1", source="one"), TextUnit(id="u2", source="two")],
            )
        ],
    )
    assert "error" not in api.open_project(str(project.root))

    result = api.unit_rows(offset=0, limit=100)

    assert result["total"] == 2
    assert len(result["rows"]) == 2
    for row in result["rows"]:
        assert {"source_file", "id", "source", "status"} <= row.keys()


def test_unit_rows_filters_by_status_and_text(api: Api, tmp_path: Path) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "filter",
        [
            StringFile(
                source_file="a.lsb",
                units=[
                    TextUnit(id="u1", source="はい", status=Status.UNTRANSLATED),
                    TextUnit(id="u2", source="君", target="you", status=Status.REVIEWED),
                ],
            )
        ],
    )
    assert "error" not in api.open_project(str(project.root))

    result = api.unit_rows(status="untranslated", q="はい")

    assert len(result["rows"]) == 1
    assert result["rows"][0]["id"] == "u1"


@pytest.mark.parametrize("source_lang,expected", [
    ("ja", ["japanese", "mixed"]),
    ("en-US", ["english", "mixed"]),
    ("zh-Hant-TW", ["traditional"]),
    ("zh-cn", ["simplified"]),
])
def test_queue_language_filter_applies_before_paging_and_next_unit(
    api: Api, tmp_path: Path, source_lang: str, expected: list[str],
) -> None:
    english = "Welcome to the village. Let us begin our adventure together."
    japanese = "これは日本語の文章です。新しい冒険を一緒に始めましょう。"
    units = [
        TextUnit(id="japanese", source=japanese),
        TextUnit(id="english", source=english, target=japanese, status=Status.MT),
        TextUnit(
            id="traditional",
            source="這是一段繁體中文的文字，歡迎來到臺灣，我們一起學習語言和進行遊戲。",
        ),
        TextUnit(id="simplified", source="这里是简体中文，这个游戏让我们学习语言并开始新的冒险。"),
        TextUnit(id="mixed", source=f"{english}\n{japanese}"),
    ]
    project = _seed_project(
        api, tmp_path, "languages", [StringFile(source_file="scene", units=units)],
    )
    assert "error" not in api.open_project(str(project.root))

    result = api.unit_rows(source_lang=source_lang, offset=len(expected) - 1, limit=1)
    assert result["total"] == len(expected)
    assert [row["id"] for row in result["rows"]] == expected[-1:]
    assert [row["id"] for row in api.unit_rows(source_lang=source_lang)["rows"]] == expected
    assert api.next_unit("scene", expected[-1], source_lang=source_lang) is None
    if len(expected) > 1:
        assert api.next_unit("scene", expected[0], source_lang=source_lang)["id"] == expected[1]
    assert api.unit_rows(source_lang=source_lang, q="mixed", scope="id")["total"] == (
        1 if "mixed" in expected else 0
    )
    assert api.unit_rows()["total"] == len(units)


def test_queue_languages_use_all_source_lines_and_refresh_with_the_catalog(api: Api) -> None:
    english = "Welcome to the village. Let us begin our adventure together."
    japanese = "これは日本語の文章です。新しい冒険を一緒に始めましょう。"
    simplified = "这里是简体中文，这个游戏让我们学习语言并开始新的冒险。"
    assert api.unit_languages() == []
    api.state.string_files = [
        StringFile(source_file="scene", units=[
            TextUnit(id="mixed", source=f"  {english}\n\n{japanese}"),
            TextUnit(id="simplified", source=simplified),
            TextUnit(id="duplicate", source=english, target="這是一段繁體中文的文字。"),
            TextUnit(id="unknown", source="123"),
            TextUnit(id="short-noise", source="Pr.W"),
            TextUnit(id="short-latin", source="the Earth."),
        ]),
        StringFile(source_file="images", units=[
            TextUnit(id="picture", kind="image", source=japanese),
        ]),
    ]
    api.state.refresh_source_language_index()
    assert api.unit_languages() == ["en", "ja", "zh-cn"]
    api.state.string_files = [
        StringFile(source_file="scene", units=[TextUnit(id="english", source=english)]),
    ]
    api.state.refresh_source_language_index()
    assert api.unit_languages() == ["en"]


def test_queue_language_filter_uses_loaded_index(
    api: Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "indexed-languages",
        [StringFile(source_file="scene", units=[
            TextUnit(id="japanese", source="これは日本語の文章です。"),
            TextUnit(id="english", source="Welcome to this village and begin the adventure."),
        ])],
    )
    assert "error" not in api.open_project(str(project.root))

    def fail_per_row_detection(*_args: object) -> bool:
        raise AssertionError("language filter ran per-row detection")

    monkeypatch.setattr("bokucho.webapp.api.matches_source_language", fail_per_row_detection)

    assert [row["id"] for row in api.unit_rows(source_lang="ja")["rows"]] == ["japanese"]
    assert [row["id"] for row in api.unit_rows(source_lang="en")["rows"]] == ["english"]


def test_unit_rows_only_materializes_the_requested_slice(
    api: Api,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "bounded-rows",
        [
            StringFile(
                source_file="a.lsb",
                units=[
                    TextUnit(id="u1", source="one"),
                    TextUnit(id="u2", source="two"),
                    TextUnit(id="u3", source="three"),
                ],
            )
        ],
    )
    assert "error" not in api.open_project(str(project.root))
    materialized: list[str] = []
    original = api._unit_row

    def record_row(source_file: str, unit: TextUnit) -> dict:
        materialized.append(unit.id)
        return original(source_file, unit)

    monkeypatch.setattr(api, "_unit_row", record_row)

    result = api.unit_rows(offset=1, limit=1)

    assert result["total"] == 3
    assert [row["id"] for row in result["rows"]] == ["u2"]
    assert materialized == ["u2"]


def test_unit_rows_count_only_does_not_materialize_row_payloads(
    api: Api,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "count-only",
        [StringFile(source_file="a.lsb", units=[TextUnit(id="u1", source="one")])],
    )
    assert "error" not in api.open_project(str(project.root))

    def fail_if_materialized(source_file: str, unit: TextUnit) -> dict:
        pytest.fail(f"count-only request materialized {source_file}:{unit.id}")

    monkeypatch.setattr(api, "_unit_row", fail_if_materialized)

    assert api.unit_rows(offset=0, limit=0) == {"total": 1, "rows": []}


def test_unit_counts_returns_all_statuses_in_one_summary(api: Api, tmp_path: Path) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "counts",
        [
            StringFile(
                source_file="a.lsb",
                units=[
                    TextUnit(id="u1", source="one", status=Status.UNTRANSLATED),
                    TextUnit(id="u2", source="two", target="二", status=Status.MT),
                    TextUnit(id="u3", source="three", target="三", status=Status.REVIEWED),
                ],
            )
        ],
    )
    assert "error" not in api.open_project(str(project.root))

    result = api.unit_counts()

    assert result["total"] == 3
    assert result["status_counts"] == {
        "untranslated": 1,
        "mt": 1,
        "reviewed": 1,
        "ignored": 0,
    }


def test_a_picture_with_no_text_stays_out_of_the_translation_queue(
    api: Api, tmp_path: Path
) -> None:
    def picture(name: str, source: str = "") -> TextUnit:
        return TextUnit(
            id=f"img:{name}", source=source, kind="image", engine_ref={"image": name}
        )

    project = _seed_project(
        api,
        tmp_path,
        "pictures",
        [
            StringFile(
                source_file="images",
                units=[picture("a.png"), picture("b.png", "道具"), picture("c.png")],
            ),
            StringFile(source_file="a.lsb", units=[TextUnit(id="u1", source="はい")]),
        ],
    )
    assert "error" not in api.open_project(str(project.root))

    rows = api.unit_rows(status="untranslated")["rows"]
    assert [row["id"] for row in rows] == ["u1", "img:b.png"]
    counts = api.unit_counts()
    assert counts["status_counts"]["untranslated"] == 2
    assert (counts["total"], counts["image_units"]) == (2, 3)
    assert api.next_unit("a.lsb", "u1")["id"] == "img:b.png"


def test_the_queue_shelves_story_apart_from_lines_with_variables(
    api: Api, tmp_path: Path
) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "shelves",
        [
            StringFile(
                source_file="a.lsb",
                units=[
                    TextUnit(id="story", source="はい"),
                    TextUnit(id="named", source="<VAR NAME=名前>さん"),
                    TextUnit(id="price", source="%gold%円"),
                    TextUnit(id="item", source="短剣", kind="value"),
                ],
            ),
            StringFile(
                source_file="images",
                units=[TextUnit(id="img:a.png", source="道具", kind="image")],
            ),
        ],
    )
    assert "error" not in api.open_project(str(project.root))

    def shelf(category: str) -> list[str]:
        return [row["id"] for row in api.unit_rows(category=category)["rows"]]

    assert shelf("story") == ["story"]
    assert shelf("variables") == ["named", "price"]
    assert shelf("values") == ["item"]
    assert shelf("images") == ["img:a.png"]
    assert len(api.unit_rows()["rows"]) == 5
    assert api.next_unit("a.lsb", "named", category="variables")["id"] == "price"
    assert api.next_unit("a.lsb", "story", category="values")["id"] == "item"


def test_unit_detail_carries_suggestions_history_and_glossary_spans(
    api: Api, tmp_path: Path
) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "detail",
        [
            StringFile(
                source_file="a.lsb",
                units=[
                    TextUnit(id="u1", source="はい", target="是", status=Status.REVIEWED),
                    TextUnit(
                        id="u2",
                        source="はい",
                        history=[
                            HistoryEntry(
                                target="draft",
                                status=Status.MT,
                                at="2026-01-01T00:00:00+00:00",
                            )
                        ],
                    ),
                ],
            )
        ],
    )
    Glossary(entries=[GlossaryEntry(source="はい", target="是")]).save(project.glossary_path)
    assert "error" not in api.open_project(str(project.root))

    detail = api.unit_detail("a.lsb", "u2")

    assert "error" not in detail
    assert detail["source_file"] == "a.lsb"
    assert detail["id"] == "u2"
    assert detail["source"] == "はい"
    assert any(
        s["target"] == "是" and s["origin"] == "memory" for s in detail["suggestions"]
    )
    assert any(s["origin"] == "glossary" for s in detail["suggestions"])
    assert len(detail["history"]) == 1
    assert detail["spans"] == [[0, 2, "是"]]


def test_unit_detail_names_where_the_line_lives(api: Api, tmp_path: Path) -> None:
    project = _seed_project(api, tmp_path, "scene", [StringFile(source_file="Scripts", units=[
        TextUnit(id="Scripts:s0:1:5", source="To Title", kind="script",
                 context=Context(scene="Scene_End:1")),
        TextUnit(id="Scripts:s0:2:5", source="Shutdown", kind="script"),
    ])])
    assert "error" not in api.open_project(str(project.root))
    assert api.unit_detail("Scripts", "Scripts:s0:1:5")["scene"] == "Scene_End:1"
    assert api.unit_detail("Scripts", "Scripts:s0:2:5")["scene"] == ""


def test_save_draft_persists_without_changing_status(api: Api, tmp_path: Path) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "draft",
        [StringFile(source_file="a.lsb", units=[TextUnit(id="u1", source="one")])],
    )
    assert "error" not in api.open_project(str(project.root))

    result = api.save_draft("a.lsb", "u1", "草稿")

    assert "error" not in result
    unit = load_string_file(project.strings_dir / "a.lsb.json").by_id("u1")
    assert unit.target == "草稿"
    assert unit.status == Status.UNTRANSLATED


def test_mark_reviewed_advances_status_and_appends_history(api: Api, tmp_path: Path) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "review",
        [
            StringFile(
                source_file="a.lsb",
                units=[TextUnit(id="u1", source="one", target="定稿", status=Status.MT)],
            )
        ],
    )
    assert "error" not in api.open_project(str(project.root))

    result = api.mark_reviewed("a.lsb", "u1")

    assert "error" not in result
    unit = load_string_file(project.strings_dir / "a.lsb.json").by_id("u1")
    assert unit.status == Status.REVIEWED
    assert unit.history[-1].status == Status.REVIEWED


def test_mark_reviewed_rejects_a_target_dropping_a_variable(api: Api, tmp_path: Path) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "review-fail",
        [
            StringFile(
                source_file="a.lsb",
                units=[
                    TextUnit(
                        id="u1", source="Hello %name%", target="Hello", status=Status.MT
                    )
                ],
            )
        ],
    )
    assert "error" not in api.open_project(str(project.root))

    result = api.mark_reviewed("a.lsb", "u1")

    assert result["error"]["kind"] == "validation"
    assert "%name%" in result["error"]["message"]
    unit = load_string_file(project.strings_dir / "a.lsb.json").by_id("u1")
    assert unit.status == Status.MT


def test_import_mapping_imports_as_machine_translation(api: Api, tmp_path: Path) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "import",
        [StringFile(source_file="a.lsb", units=[TextUnit(id="u1", source="はい")])],
    )
    assert "error" not in api.open_project(str(project.root))

    result = api.import_mapping({"はい": "匯入"})

    assert "error" not in result
    unit = load_string_file(project.strings_dir / "a.lsb.json").by_id("u1")
    assert unit.target == "匯入"
    assert unit.status == Status.MT
