from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _scorer():
    path = Path(__file__).resolve().parents[2] / "tools" / "ocr_score.py"
    spec = importlib.util.spec_from_file_location("bokucho_ocr_score", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_forms_a_translation_does_not_distinguish_are_folded_together() -> None:
    module = _scorer()

    assert module.cjk("ｱｲｳ") == module.cjk("アイウ")
    assert module.cjk("体力回復薬") == "体力回復薬"


def test_only_the_text_that_gets_translated_is_counted() -> None:
    module = _scorer()

    assert module.cjk("- Get -") == ""
    assert module.cjk("HP 現在地 MP") == "現在地"


def test_a_reading_is_charged_one_edit_a_character() -> None:
    module = _scorer()

    assert module.edits("手提叶荷袋", "手提け荷袋") == 1
    assert module.edits("力回復藥", "体力回復薬") == 2
    assert module.edits("現在地", "現在地") == 0
    assert module.edits("", "現在地") == 3


def test_a_scan_is_scored_by_which_of_three_things_each_picture_was(
    new_project, tmp_path: Path
) -> None:
    from bokucho.application.assets import save_image_units
    from bokucho.images import unit_id
    from bokucho.model import StringFile, TextUnit

    module = _scorer()
    project = new_project("score", root=tmp_path / "project")
    sample = {"0": "read.png", "1": "blank.png", "2": "unseen.png"}
    save_image_units(
        project,
        StringFile(
            source_file="images",
            units=[
                TextUnit(
                    id=unit_id("read.png"),
                    source="手提叶荷袋",
                    kind="image",
                    engine_ref={"image": "read.png"},
                ),
                TextUnit(
                    id=unit_id("blank.png"),
                    source="ゴブリン",
                    kind="image",
                    engine_ref={"image": "blank.png"},
                ),
            ],
        ),
    )

    scored = module.score(
        project,
        {"0": ["手提け荷袋"], "1": [], "2": ["現在地"]},
        sample,
    )

    assert (scored.lettered, scored.found) == (1, 1)
    assert (scored.blank, scored.invented) == (1, 1)
    assert scored.unscored == ("unseen.png",)
    assert (scored.errors, scored.reference) == (1, 5)


def test_a_picture_the_scan_never_saw_is_reported_rather_than_scored() -> None:
    module = _scorer()

    partial = module.Score(
        lettered=2, found=1, blank=0, invented=0, errors=1, reference=6,
        unscored=("ui/menu.png",),
    )

    assert "not scanned" in partial.report()
    assert "ui/menu.png" in partial.report()


def test_a_picture_nobody_could_identify_is_kept_apart_from_one_never_scanned(
) -> None:
    module = _scorer()

    mixed = module.Score(
        lettered=2, found=2, blank=0, invented=0, errors=0, reference=6,
        unscored=("ui/menu.png",), unidentified=("6 (- Get -)",),
    )

    assert "not identified               1: 6 (- Get -)" in mixed.report()
    assert "not scanned                  1: ui/menu.png" in mixed.report()


def test_a_scan_that_read_nothing_is_not_reported_as_a_bad_reader() -> None:
    module = _scorer()

    unread = module.Score(
        lettered=27, found=0, blank=18, invented=0, errors=300, reference=300,
        unscored=(),
    )

    assert "OCR off" in unread.report()
    assert module.SAMPLE_FILENAME not in unread.report()


def test_a_rate_that_means_the_sample_is_wrong_says_so() -> None:
    module = _scorer()

    aligned = module.Score(
        lettered=27, found=26, blank=18, invented=0, errors=7, reference=300,
        unscored=(),
    )
    misaligned = module.Score(
        lettered=27, found=26, blank=18, invented=0, errors=290, reference=300,
        unscored=(),
    )

    assert module.SAMPLE_FILENAME not in aligned.report()
    assert module.SAMPLE_FILENAME in misaligned.report()


def test_a_picture_is_identified_by_its_longest_line() -> None:
    module = _scorer()
    walk = {0: "ui/閉じるボタン.gal", 1: "item/【採取】ハーブ.gal"}

    resolved = module.resolve_sample(
        {
            "0": ["使用：観察力", "アオイの観察力：", "閉じる"],
            "1": ["ハーブ"],
        },
        walk,
    )

    assert resolved["0"] is None
    assert resolved["1"] == "item/【採取】ハーブ.gal"


def test_a_line_that_names_two_pictures_names_neither() -> None:
    module = _scorer()
    walk = {
        0: "item/【採取】古代の書.gal",
        1: "item/【採取】古代の書（マウスイン）.gal",
    }

    assert module.resolve_sample({"0": ["古代の書"]}, walk)["0"] is None


def test_a_missing_ground_truth_is_explained(tmp_path, monkeypatch, capsys) -> None:
    scorer = _scorer()
    monkeypatch.setattr(scorer, "GROUND_TRUTH", tmp_path / "ocr-ground-truth.json")
    assert scorer.main(["no-such-project"]) == 2
    assert "ocr-ground-truth.json" in capsys.readouterr().err
