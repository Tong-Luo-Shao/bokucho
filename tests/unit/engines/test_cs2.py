"""CS2 (CatSystem2-on-Ren'Py) extraction and archive reading."""

from __future__ import annotations

import io
import json
import pickle
import zlib
from pathlib import Path

import pytest

from bokucho.detect import detect
from bokucho.engines.cs2 import (
    Cs2Adapter,
    apply_units_to_extras_ui,
    apply_units_to_localization,
    apply_units_to_system_prompts,
    units_from_extras_ui,
    units_from_localization,
    units_from_system_prompts,
)
from bokucho.engines.renpy_archive import _SafeUnpickler, read_member, rpa_index
from bokucho.model import Status, TextUnit


def build_rpa(path: Path, files: dict[str, bytes], key: int = 0x42) -> Path:
    """Write a minimal RPA-3.0 archive holding ``files`` ({name: bytes})."""
    header_len = len(b"RPA-3.0 ") + 16 + 1 + 8 + 1  # fixed-width header -> constant
    body = bytearray()
    index: dict[str, list] = {}
    for name, payload in files.items():
        offset = header_len + len(body)
        index[name] = [(offset ^ key, len(payload) ^ key, "")]
        body += payload
    index_offset = header_len + len(body)
    header = b"RPA-3.0 %016x %08x\n" % (index_offset, key)
    assert len(header) == header_len
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(body) + zlib.compress(pickle.dumps(index)))
    return path


def _json_member(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def test_rpa_round_trip(tmp_path: Path):
    archive = build_rpa(
        tmp_path / "a.rpa", {"one.txt": b"hello", "dir/two.bin": b"\x00\x01\x02world"}
    )
    assert set(rpa_index(archive)) == {"one.txt", "dir/two.bin"}
    assert read_member(archive, "one.txt") == b"hello"
    assert read_member(archive, "dir/two.bin") == b"\x00\x01\x02world"


def test_rpa_refuses_pickle_globals():
    with pytest.raises(pickle.UnpicklingError):
        _SafeUnpickler(io.BytesIO(pickle.dumps(__import__("os").system))).load()


def test_units_text_ui_names_and_prefill():
    source = {
        "text": {"01": {"6": "日文A", "8": "日文B"}},
        "ui": {"ok": "決定"},
        "names": {},
        "choices": {},
    }
    prefill = {
        "text": {"01": {"6": "中文A"}},
        "ui": {"ok": "确定"},
        "names": {"誠": "小诚"},
    }
    units = {u.id: u for u in units_from_localization(source, prefill)}
    assert set(units) == {"text/01/6", "text/01/8", "ui/ok"} | {
        i for i in units if i.startswith("names/")
    }

    drafted = units["text/01/6"]
    assert drafted.source == "日文A"
    assert drafted.target == "中文A"
    assert drafted.status is Status.MT
    assert drafted.engine_ref == {
        "kind": "cs2",
        "section": "text",
        "file": "text/01",
        "scene": "01",
        "line": "6",
    }

    untranslated = units["text/01/8"]
    assert untranslated.target == ""
    assert untranslated.status is Status.UNTRANSLATED

    assert units["ui/ok"].source == "決定" and units["ui/ok"].target == "确定"

    name_unit = next(u for u in units.values() if u.engine_ref.get("section") == "names")
    assert name_unit.source == "誠" and name_unit.target == "小诚"
    assert name_unit.status is Status.MT


def test_extract_reads_localization_from_rpa(tmp_path: Path):
    ja = {"text": {"01": {"6": "原文", "8": "二行目"}}, "ui": {"ok": "決定"}}
    zh = {"text": {"01": {"6": "原文译"}}, "ui": {"ok": "确定"}, "names": {"誠": "诚"}}
    game = tmp_path / "game"
    build_rpa(
        game / "system.rpa",
        {
            "data/localization/ja.json": _json_member(ja),
            "data/localization/zh_cn.json": _json_member(zh),
        },
    )
    units = Cs2Adapter(source_lang="ja", prefill_lang="zh_cn").extract(tmp_path)
    by_id = {u.id: u for u in units}
    assert by_id["text/01/6"].target == "原文译" and by_id["text/01/6"].status is Status.MT
    assert by_id["text/01/8"].status is Status.UNTRANSLATED
    assert any(u.engine_ref.get("section") == "names" for u in units)


def test_extract_without_prefill(tmp_path: Path):
    ja = {"text": {"01": {"6": "原文"}}}
    game = tmp_path / "game"
    build_rpa(game / "system.rpa", {"data/localization/ja.json": _json_member(ja)})
    units = Cs2Adapter(source_lang="ja").extract(tmp_path)
    assert units[0].source == "原文" and units[0].status is Status.UNTRANSLATED


def test_ruby_stripped_to_base_kanji():
    # CatSystem2 furigana [base/reading] collapses to the base before the LLM sees it.
    source = {"text": {"01": {"6": "名前は『[一/いち][之/の][瀬/せ]』。[公園/ここ]で待つ。"}}}
    prefill = {"text": {"01": {"6": "名字是『[一/yi]』。"}}}
    units = {u.id: u for u in units_from_localization(source, prefill)}
    assert units["text/01/6"].source == "名前は『一之瀬』。公園で待つ。"
    assert units["text/01/6"].target == "名字是『一』。"


SPLIT_JA = {
    "text": {"01": {"6": "「あ", "7": "\\nい」", "8": "\\n", "9": "「う", "10": "え」"}},
    # head -> exclusive end; 9-10 claims a pair whose second line lacks the \n code.
    "dialogue_ranges": {"01": {"6": 8, "8": 9, "9": 11}},
}
SPLIT_ZH = {"text": {"01": {"6": "“啊咿”", "7": "", "8": "\\n"}}}


def test_a_message_split_across_lines_is_one_unit():
    units = {u.id: u for u in units_from_localization(SPLIT_JA, SPLIT_ZH)}
    assert set(units) == {"text/01/6-7", "text/01/8", "text/01/9", "text/01/10"}
    merged = units["text/01/6-7"]
    assert merged.source == "「あ\nい」"  # the \n code becomes the line break it stands for
    assert merged.target == "“啊咿”\n"  # the draft keeps its lines where they were
    assert merged.engine_ref["lines"] == ["6", "7"]
    assert merged.engine_ref["parts"] == [
        {"block": "text/01/6"},
        {"text": "\n"},
        {"block": "text/01/7", "drop": "\\n"},
    ]


def test_a_merged_message_is_cut_back_into_its_lines():
    def written(target: str, lines: list[str]) -> list[str]:
        loc: dict = {"text": {"01": {}}}
        ref = {"section": "text", "scene": "01", "lines": lines}
        unit = TextUnit(id="m", source="", target=target, engine_ref=ref)
        apply_units_to_localization(loc, [unit])
        return [loc["text"]["01"][k] for k in lines]

    assert written("「啊\n咿」", ["6", "7"]) == ["「啊", "\\n咿」"]
    assert written("「啊咿」", ["6", "7", "8"]) == ["「啊咿」", "", ""]  # short: the rest blank
    assert written("「啊\n咿\n嗚」", ["6", "7"]) == ["「啊", "\\n咿嗚」"]  # long: folded in


def test_extracting_then_injecting_untouched_reproduces_the_slot(tmp_path: Path):
    build_rpa(
        tmp_path / "game" / "system.rpa",
        {
            "data/localization/ja.json": _json_member(SPLIT_JA),
            "data/localization/zh_cn.json": _json_member(SPLIT_ZH),
        },
    )
    adapter = Cs2Adapter(source_lang="ja", prefill_lang="zh_cn")
    adapter.inject(adapter.extract(tmp_path), tmp_path, tmp_path / "out")
    written = json.loads(
        (tmp_path / "out/game/data/localization/zh_cn.json").read_text(encoding="utf-8")
    )
    assert written["text"] == SPLIT_ZH["text"]


def test_extracting_again_joins_the_lines_already_translated(tmp_path: Path):
    from bokucho.application.projects import CreateProjectRequest, create_project, extract_text
    from bokucho.model import StringFile
    from bokucho.project import Project

    build_rpa(
        tmp_path / "game" / "system.rpa",
        {
            "data/localization/ja.json": _json_member(SPLIT_JA),
            "data/localization/zh_cn.json": _json_member(SPLIT_ZH),
        },
    )
    project = create_project(CreateProjectRequest(tmp_path / "p", "t", "ja", "zh-tw", "cs2"))
    engine = project.config.engine.model_copy(update={"game_path": str(tmp_path)})
    project.save_config(project.config.model_copy(update={"engine": engine}))
    project = Project.load(project.root)
    ref = {"kind": "cs2", "section": "text", "file": "text/01", "scene": "01"}
    project.save_string_file(
        StringFile(
            source_file="text/01",
            units=[
                TextUnit(
                    id="text/01/6",
                    source="「あ",
                    target="「啊",
                    status=Status.MT,
                    engine_ref={**ref, "line": "6"},
                ),
                TextUnit(
                    id="text/01/7",
                    source="\\nい」",
                    target="\\n咿」",
                    status=Status.MT,
                    engine_ref={**ref, "line": "7"},
                ),
            ],
        ),
        "text__01.json",
    )
    extract_text(project, Cs2Adapter(source_lang="ja", prefill_lang="zh_cn"))
    (strings,) = project.load_string_files()
    assert strings.by_id("text/01/6-7").target == "「啊\n咿」"  # not the draft
    assert strings.by_id("text/01/6").missing and strings.by_id("text/01/7").missing


def test_apply_units_overlays_only_translated():
    loc = {
        "text": {"01": {"6": "简体", "8": "简体二"}},
        "ui": {"ok": "确定"},
        "names": {"誠": "诚"},
        "dialogue_ranges": {"01": {"6": 7}},
    }
    units = [
        TextUnit(
            id="text/01/6",
            source="原文",
            target="繁體",
            engine_ref={"section": "text", "scene": "01", "line": "6"},
        ),
        TextUnit(
            id="text/01/8",
            source="原文二",
            target="",
            engine_ref={"section": "text", "scene": "01", "line": "8"},
        ),
        TextUnit(id="ui/ok", source="OK", target="確定", engine_ref={"section": "ui", "key": "ok"}),
    ]
    applied = apply_units_to_localization(loc, units)
    assert applied == 2  # the blank-target one is skipped
    assert loc["text"]["01"]["6"] == "繁體"
    assert loc["text"]["01"]["8"] == "简体二"  # untranslated keeps the base layer
    assert loc["ui"]["ok"] == "確定"
    assert loc["dialogue_ranges"] == {"01": {"6": 7}}  # structure preserved


def test_inject_writes_overlay_onto_the_slot(tmp_path: Path):
    ja = {"text": {"01": {"6": "原文", "8": "二行目"}}, "ui": {"ok": "決定"}}
    zh = {
        "text": {"01": {"6": "原文译", "8": "二行译"}},
        "ui": {"ok": "确定"},
        "dialogue_ranges": {"01": {"6": 7}},
    }
    game = tmp_path / "game"
    build_rpa(
        game / "system.rpa",
        {
            "data/localization/ja.json": _json_member(ja),
            "data/localization/zh_cn.json": _json_member(zh),
        },
    )
    adapter = Cs2Adapter(source_lang="ja", prefill_lang="zh_cn")
    units = adapter.extract(tmp_path)
    for u in units:
        if u.id == "text/01/6":
            u.target = "繁體譯文"

    out = tmp_path / "out"
    adapter.inject(units, tmp_path, out)
    written = json.loads((out / "game/data/localization/zh_cn.json").read_text(encoding="utf-8"))
    assert written["text"]["01"]["6"] == "繁體譯文"  # our translation
    assert written["text"]["01"]["8"] == "二行译"  # kept from the slot's draft
    assert written["dialogue_ranges"] == {"01": {"6": 7}}  # loader metadata preserved


SAMPLE_SP = {
    "languages": ["ja", "zh_cn"],
    "messages": [
        {
            "id": "engine.quit",
            "category": "c",
            "source": "Quit?",
            "ja": "終了しますか？",
            "zh_cn": "退出吗？",
            "status": "s",
        },
    ],
    "save_titles": [
        {
            "id": "save_title.001",
            "category": "c",
            "source": "OP",
            "ja": "オープニング",
            "zh_cn": "开幕",
            "status": "s",
        },
    ],
}
SAMPLE_EU = {"ja": {"gallery": "CG鑑賞"}, "zh_cn": {"gallery": "CG鉴赏"}}


def test_units_from_system_prompts():
    units = {u.id: u for u in units_from_system_prompts(SAMPLE_SP, "ja", "zh_cn")}
    assert set(units) == {
        "system_prompts/messages/engine.quit",
        "system_prompts/save_titles/save_title.001",
    }
    msg = units["system_prompts/messages/engine.quit"]
    assert msg.source == "終了しますか？" and msg.target == "退出吗？"
    assert msg.status is Status.MT
    assert msg.engine_ref == {
        "kind": "cs2",
        "section": "system_prompts",
        "file": "system_prompts",
        "group": "messages",
        "id": "engine.quit",
    }


def test_units_from_extras_ui():
    units = units_from_extras_ui(SAMPLE_EU, "ja", "zh_cn")
    assert len(units) == 1
    assert units[0].id == "extras_ui/gallery"
    assert units[0].source == "CG鑑賞" and units[0].target == "CG鉴赏"
    assert units[0].engine_ref["section"] == "extras_ui"


def test_apply_system_prompts_and_extras_overlay_the_slot_language():
    sp = json.loads(json.dumps(SAMPLE_SP))  # deep copy
    eu = json.loads(json.dumps(SAMPLE_EU))
    units = [
        TextUnit(
            id="system_prompts/messages/engine.quit",
            source="終了しますか？",
            target="要離開嗎？",
            engine_ref={"section": "system_prompts", "group": "messages", "id": "engine.quit"},
        ),
        TextUnit(
            id="extras_ui/gallery",
            source="CG鑑賞",
            target="CG鑑賞",
            engine_ref={"section": "extras_ui", "key": "gallery"},
        ),
    ]
    assert apply_units_to_system_prompts(sp, units, "zh_cn") == 1
    assert apply_units_to_extras_ui(eu, units, "zh_cn") == 1
    assert sp["messages"][0]["zh_cn"] == "要離開嗎？"
    assert sp["messages"][0]["ja"] == "終了しますか？"  # source language untouched
    assert eu["zh_cn"]["gallery"] == "CG鑑賞"
    assert eu["ja"]["gallery"] == "CG鑑賞"  # unchanged (source already equalled it)


def test_inject_writes_system_prompts_and_extras_overlays(tmp_path: Path):
    ja = {"text": {"01": {"6": "原文"}}}
    zh = {"text": {"01": {"6": "原文译"}}}
    game = tmp_path / "game"
    build_rpa(
        game / "system.rpa",
        {
            "data/localization/ja.json": _json_member(ja),
            "data/localization/zh_cn.json": _json_member(zh),
            "data/localization/system_prompts.json": _json_member(SAMPLE_SP),
            "data/localization/extras_ui.json": _json_member(SAMPLE_EU),
        },
    )
    adapter = Cs2Adapter(source_lang="ja", prefill_lang="zh_cn")
    units = adapter.extract(tmp_path)
    for u in units:
        if u.id == "system_prompts/messages/engine.quit":
            u.target = "要離開嗎？"
        elif u.id == "extras_ui/gallery":
            u.target = "CG鑑賞"

    out = tmp_path / "out"
    adapter.inject(units, tmp_path, out)
    loc = out / "game/data/localization"
    sp = json.loads((loc / "system_prompts.json").read_text(encoding="utf-8"))
    eu = json.loads((loc / "extras_ui.json").read_text(encoding="utf-8"))
    assert sp["messages"][0]["zh_cn"] == "要離開嗎？"
    assert sp["save_titles"][0]["zh_cn"] == "开幕"  # untranslated keeps the draft
    assert eu["zh_cn"]["gallery"] == "CG鑑賞"
    assert eu["ja"]["gallery"] == "CG鑑賞"  # source language preserved


def test_detect_ranks_cs2_first(tmp_path: Path):
    game = tmp_path / "game"
    build_rpa(game / "code.rpa", {"cs2_text.py": b"# cs2", "story/01.rpyc": b"\x00"})
    build_rpa(game / "system.rpa", {"data/localization/ja.json": _json_member({"text": {}})})
    (game / "start.rpyc").write_bytes(b"\x00")  # also looks like a plain Ren'Py game
    verdicts = detect(tmp_path)
    assert verdicts, "nothing detected"
    assert verdicts[0].engine == "cs2"
