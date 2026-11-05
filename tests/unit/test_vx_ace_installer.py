"""An RGSS patch -- XP, VX or VX Ace -- is compiled into the player's own data and undone."""

from __future__ import annotations

import json
import locale
import os
import shutil
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from fontTools.ttLib import TTFont

pytest.importorskip("rubymarshal")

from rubymarshal.classes import RubyObject, RubyString  # noqa: E402

from bokucho.appdirs import portable_root  # noqa: E402
from bokucho.application import rgss_font  # noqa: E402
from bokucho.application.errors import ReleaseBlocked  # noqa: E402
from bokucho.application.projects import CreateProjectRequest, create_project  # noqa: E402
from bokucho.application.release import release_patcher  # noqa: E402
from bokucho.engines import rgssad  # noqa: E402
from bokucho.engines.rpgmaker import (  # noqa: E402
    DATA_SUFFIX,
    VARIANT_ENGINES,
    RpgMakerAdapter,
    Variant,
    file_slots,
)
from bokucho.engines.rpgmaker_marshal import dump_marshal, load_legacy, load_marshal  # noqa: E402
from bokucho.engines.rpgmaker_scripts import load_scripts  # noqa: E402
from bokucho.exe_icon import exe_icon  # noqa: E402
from bokucho.model import StringFile  # noqa: E402
from bokucho.patcher.__main__ import main  # noqa: E402
from bokucho.patcher.core import (  # noqa: E402
    RECORD,
    State,
    applied_message,
    apply,
    inspect,
    restore,
    sha256_of,
)
from bokucho.patcher.payload import read_patch  # noqa: E402
from bokucho.project import Project  # noqa: E402


def _ruby(class_name: str, **ivars) -> RubyObject:
    obj = RubyObject(class_name)
    obj.attributes = {f"@{key}": value for key, value in ivars.items()}
    return obj


def _text(value: str) -> RubyString:
    string = RubyString(value)
    string.attributes = {"E": True}
    return string


def _snapshot(folder: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(folder): path.read_bytes() for path in folder.rglob("*") if path.is_file()
    }


def _names(blob: bytes, variant: Variant) -> list[str]:
    payload = load_marshal(blob) if variant is Variant.ACE else load_legacy(blob)
    return [slot.text for slot in file_slots("Actors", payload, variant)]


GENERATIONS = {
    "xp": (Variant.XP, "Game.rgssad"),
    "vx": (Variant.VX, "Game.rgss2a"),
    "ace": (Variant.ACE, "Game.rgss3a"),
}
TARGETS = {"ハロルド": "哈羅德", "勇者": "勇者大人", "To Title": "回到標題"}
TITLE = "勇者の旅"
INI = f"[Game]\r\nLibrary=RGSS202J.dll\r\nTitle={TITLE}\r\n[Resize]\r\nWindowSize=0\r\n".encode(
    "cp932"
)
MUSIC = b"music " * 200
GAME = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"


def _game_files(variant: Variant, scripts) -> dict[str, bytes]:
    suffix = DATA_SUFFIX[variant]
    if variant is Variant.ACE:
        actor = _ruby("RPG::Actor", id=1, name=_text("ハロルド"), description=_text("勇者"))
        system = _ruby("RPG::System", game_title=_text(TITLE))
    else:
        actor = _ruby("RPG::Actor", id=1, name="ハロルド".encode())
        system = _ruby("RPG::System", game_title=TITLE.encode())
    return {
        f"Data\\System{suffix}": dump_marshal(system),
        f"Data\\Actors{suffix}": dump_marshal([None, actor]),
        f"Data\\Scripts{suffix}": scripts([("Scene_End", 's1 = "To Title"\n')]),
        "Graphics\\Pictures\\title.png": b"original picture",
        "Audio\\BGM\\theme.ogg": MUSIC,
    }


@pytest.fixture(
    params=[(generation, layout) for generation in GENERATIONS for layout in ("packed", "loose")],
    ids="-".join,
)
def rgss_release(request, tmp_path: Path):
    generation, layout = request.param
    variant, archive_name = GENERATIONS[generation]
    ace = variant is Variant.ACE
    files = _game_files(variant, request.getfixturevalue("ace_scripts" if ace else "xp_scripts"))
    game = tmp_path / "game"
    game.mkdir()
    (game / "Game.exe").write_bytes(GAME.read_bytes() if GAME.is_file() else b"MZ game")
    (game / "Game.ini").write_bytes(INI)
    if layout == "packed":
        build = request.getfixturevalue("build_archive" if ace else "build_archive_v1")
        build(game / archive_name, files)
    else:
        for name, data in files.items():
            path = game.joinpath(*name.split("\\"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    project = create_project(
        CreateProjectRequest(
            tmp_path / "project", generation, "ja", "zh-Hant", VARIANT_ENGINES[variant]
        )
    )
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(update={"game_path": str(game)}),
            }
        )
    )
    workspace = project.root / "unpacked"
    units = RpgMakerAdapter(workspace=workspace).extract(game)
    for unit in units:
        unit.target = TARGETS.get(unit.source, "")
    project.save_string_file(StringFile(source_file="Actors", units=units), "actors.json")
    picture = project.root / "out" / "Graphics" / "Pictures" / "title.png"
    picture.parent.mkdir(parents=True)
    picture.write_bytes(b"translated picture")
    stub = tmp_path / "stub.exe"
    stub.write_bytes(Path(sys.executable).read_bytes())
    before = _snapshot(game)
    built = release_patcher(
        Project.load(project.root),
        lambda current: RpgMakerAdapter(workspace=workspace),
        project.root / "dist",
        stub,
    )
    assert _snapshot(game) == before
    player = tmp_path / "玩家 copy"
    shutil.copytree(game, player)
    return generation, layout, built, player


def test_the_recipe_names_the_generation(rgss_release) -> None:
    generation, _, built, _ = rgss_release
    with ZipFile(BytesIO(read_patch(built.executable.read_bytes()).recipe)) as archive:
        engine = json.loads(archive.read("recipe.json"))["engine"]
    wanted = {"xp": "rpgmaker-xp", "vx": "rpgmaker-vx", "ace": "rpgmaker-vx-ace"}
    assert engine == wanted[generation]


def test_the_release_is_one_patcher_carrying_translation_data_only(rgss_release) -> None:
    generation, layout, built, _ = rgss_release
    variant, archive_name = GENERATIONS[generation]
    suffix = DATA_SUFFIX[variant]
    assert built.executable.name == f"{generation}-ja2zh-tw-patcher.exe"
    assert list(built.artifact_dir.iterdir()) == [built.executable]
    patch = read_patch(built.executable.read_bytes())
    assert patch.recipe is not None
    with ZipFile(BytesIO(patch.recipe)) as archive:
        assert sorted(archive.namelist()) == [
            "out/Graphics/Pictures/title.png",
            "recipe.json",
            "strings/actors.json",
        ]
    if layout == "packed":
        assert [item.name for item in patch.files] == [archive_name]
    else:
        assert [item.name for item in patch.files] == [
            f"Data/Actors{suffix}",
            f"Data/Scripts{suffix}",
            "Graphics/Pictures/title.png",
        ]


def test_the_patcher_compiles_into_the_players_data_and_restores_it(rgss_release) -> None:
    generation, layout, built, player = rgss_release
    variant, archive_name = GENERATIONS[generation]
    suffix = DATA_SUFFIX[variant]
    names = ["哈羅德", "勇者大人"] if variant is Variant.ACE else ["哈羅德"]
    before = _snapshot(player)
    patch = read_patch(built.executable.read_bytes())

    assert main([str(built.executable), "--apply", str(player)]) == 0
    assert inspect(player, patch).state is State.PATCHED
    if layout == "packed":
        archive = player / archive_name
        assert (player / f"{archive_name}.bak").read_bytes() == before[Path(archive_name)]
        assert archive.read_bytes()[:8] == before[Path(archive_name)][:8]
        contents = {entry.path: rgssad.read(archive, entry) for entry in rgssad.entries(archive)}
        assert _names(contents[f"Data/Actors{suffix}"], variant) == names
        scripts = contents[f"Data/Scripts{suffix}"]
        assert contents["Graphics/Pictures/title.png"] == b"translated picture"
        assert contents["Audio/BGM/theme.ogg"] == MUSIC
        assert not (player / "Data").exists()
    else:
        actors = player / "Data" / f"Actors{suffix}"
        assert _names(actors.read_bytes(), variant) == names
        assert actors.with_name(f"{actors.name}.bak").read_bytes() == (
            before[Path(f"Data/Actors{suffix}")]
        )
        scripts = (player / "Data" / f"Scripts{suffix}").read_bytes()
        picture = player / "Graphics" / "Pictures" / "title.png"
        assert picture.read_bytes() == b"translated picture"
        assert not (player / "Data" / f"System{suffix}.bak").exists()
    assert load_scripts(scripts).entries[0].source == 's1 = "回到標題"\n'
    assert not list(player.glob(".bokucho-compile-*"))
    assert main([str(built.executable), "--apply", str(player)]) == 1

    assert main([str(built.executable), "--restore", str(player)]) == 0
    assert _snapshot(player) == before


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own resource writer draws the icon")
def test_the_patch_shows_the_games_icon(rgss_release) -> None:
    _, _, built, player = rgss_release
    icon = exe_icon(built.executable.read_bytes())
    assert icon is not None
    assert icon == exe_icon((player / "Game.exe").read_bytes())


def test_a_game_of_another_version_is_refused(rgss_release) -> None:
    generation, layout, built, player = rgss_release
    variant, archive_name = GENERATIONS[generation]
    changed = player / (
        archive_name if layout == "packed" else f"Data/Actors{DATA_SUFFIX[variant]}"
    )
    changed.write_bytes(changed.read_bytes() + b"another version")
    before = _snapshot(player)
    assert main([str(built.executable), "--apply", str(player)]) == 1
    assert _snapshot(player) == before


def test_a_game_the_translator_patched_is_not_released_as_the_original(
    rgss_release, tmp_path: Path
) -> None:
    _, _, built, player = rgss_release
    assert main([str(built.executable), "--apply", str(player)]) == 0
    project = Project.load(tmp_path / "project")
    engine = project.config.engine.model_copy(update={"game_path": str(player)})
    project.save_config(project.config.model_copy(update={"engine": engine}))
    workspace = project.root / "unpacked"

    with pytest.raises(ReleaseBlocked, match="(?i)restore"):
        release_patcher(
            Project.load(project.root),
            lambda current: RpgMakerAdapter(workspace=workspace),
            project.root / "dist",
            tmp_path / "stub.exe",
        )


def _release_with_title(tmp_path: Path) -> Path:
    project = Project.load(tmp_path / "project")
    path = project.strings_dir / "actors.json"
    strings = StringFile.model_validate_json(path.read_text(encoding="utf-8"))
    for unit in strings.units:
        if unit.id == "System:game_title":
            unit.target = "勇者之旅"
    project.save_string_file(strings, path.name)
    workspace = project.root / "unpacked"
    return release_patcher(
        Project.load(project.root),
        lambda current: RpgMakerAdapter(workspace=workspace),
        tmp_path / "titled",
        tmp_path / "stub.exe",
    ).executable


def test_the_window_title_is_written_in_the_players_code_page_and_put_back(
    rgss_release, tmp_path: Path, monkeypatch
) -> None:
    generation, _, _, player = rgss_release
    executable = _release_with_title(tmp_path)
    monkeypatch.setattr(locale, "getencoding", lambda: "cp950")
    ini = player / "Game.ini"

    assert main([str(executable), "--apply", str(player)]) == 0
    # XP keeps no game title in System, so its window keeps the original one.
    titled = INI if generation == "xp" else INI.replace(
        TITLE.encode("cp932"), "勇者之旅".encode("cp950")
    )
    assert ini.read_bytes() == titled
    ini.write_bytes(titled.replace(b"WindowSize=0", b"WindowSize=1"))

    assert main([str(executable), "--restore", str(player)]) == 0
    assert ini.read_bytes() == INI.replace(b"WindowSize=0", b"WindowSize=1")


def test_a_title_the_players_code_page_cannot_show_stays_original(
    rgss_release, tmp_path: Path, monkeypatch
) -> None:
    generation, _, _, player = rgss_release
    patch = read_patch(_release_with_title(tmp_path).read_bytes())
    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")
    said: list[str] = []

    apply(player, patch, said.append)
    assert inspect(player, patch).state is State.PATCHED
    assert (player / "Game.ini").read_bytes() == INI
    assert any("標題" in line for line in said) is (generation != "xp")


#: What the game's own font draws in the font tests: the title and two reference kanji.
BASE_DRAWS = "勇者之旅日本"


def _release_with_font(tmp_path: Path, monkeypatch, make_font) -> Path:
    """Release again with a Fonts/ume.ttf drawing BASE_DRAWS and a donor drawing the rest."""
    for game in (tmp_path / "game", tmp_path / "玩家 copy"):
        make_font(game / "Fonts" / "ume.ttf", "UmePlus Gothic", BASE_DRAWS)
    every = "".join(sorted(set("".join(TARGETS.values()) + BASE_DRAWS)))
    make_font(tmp_path / "donors" / "noto.ttf", "Noto Sans TC", every)
    monkeypatch.setattr(rgss_font, "donor_dirs", lambda: [tmp_path / "donors"])
    project = Project.load(tmp_path / "project")
    workspace = project.root / "unpacked"
    return release_patcher(
        project,
        lambda current: RpgMakerAdapter(workspace=workspace),
        tmp_path / "filled",
        tmp_path / "stub.exe",
    ).executable


def test_the_recipe_carries_the_missing_characters_and_the_license(
    rgss_release, tmp_path: Path, monkeypatch, make_font
) -> None:
    _, layout, _, _ = rgss_release
    patch = read_patch(_release_with_font(tmp_path, monkeypatch, make_font).read_bytes())
    with ZipFile(BytesIO(patch.recipe)) as archive:
        meta = json.loads(archive.read("recipe.json"))["font_fill"]
        fill = TTFont(BytesIO(archive.read("fonts/fill.ttf")))
        license = archive.read("font-license.txt")
    assert license == (portable_root() / "licenses" / "Noto-Sans-CJK-OFL.txt").read_bytes()
    assert meta["base"] == "Fonts/ume.ttf" and meta["file"] == "Bokucho-UmePlusGothic.ttf"
    assert meta["family"] == "Bokucho UmePlus Gothic" and meta["face"] == 0
    drawn = set(map(chr, fill.getBestCmap()))
    assert {"哈", "標"} <= drawn
    assert not drawn & set(BASE_DRAWS)
    if layout == "loose":
        assert any(item.name.startswith("Data/Scripts") for item in patch.files)


def test_a_game_without_its_own_font_carries_no_fill(rgss_release) -> None:
    _, _, built, _ = rgss_release
    with ZipFile(BytesIO(read_patch(built.executable.read_bytes()).recipe)) as archive:
        assert json.loads(archive.read("recipe.json"))["font_fill"] is None


def _scripts(player: Path, generation: str, layout: str):
    """The player's Scripts file, read from the archive when packed."""
    variant, archive_name = GENERATIONS[generation]
    name = f"Scripts{DATA_SUFFIX[variant]}"
    if layout == "loose":
        return load_scripts((player / "Data" / name).read_bytes())
    archive = player / archive_name
    entry = next(e for e in rgssad.entries(archive) if e.path == f"Data/{name}")
    return load_scripts(rgssad.read(archive, entry))


def test_the_font_goes_where_the_game_loads_fonts_and_away_on_restore(
    rgss_release, tmp_path: Path, monkeypatch, make_font
) -> None:
    generation, layout, _, player = rgss_release
    executable = _release_with_font(tmp_path, monkeypatch, make_font)
    before = _snapshot(player)

    assert main([str(executable), "--apply", str(player)]) == 0
    copy = TTFont(player / "Fonts" / "Bokucho-UmePlusGothic.ttf")
    assert ord("標") in copy.getBestCmap() and ord("勇") in copy.getBestCmap()
    assert copy["name"].getDebugName(1) == "Bokucho UmePlus Gothic"
    license = (portable_root() / "licenses" / "Noto-Sans-CJK-OFL.txt").read_bytes()
    assert (player / "Bokucho-font-license.txt").read_bytes() == license
    assert (player / "Fonts" / "ume.ttf").read_bytes() == before[Path("Fonts/ume.ttf")]
    first = _scripts(player, generation, layout).entries[0].source
    assert "'Bokucho UmePlus Gothic'" in first and first.endswith('s1 = "回到標題"\n')
    copy.close()

    assert main([str(executable), "--restore", str(player)]) == 0
    assert _snapshot(player) == before


def test_restore_takes_away_nothing_outside_the_game_folder(
    rgss_release, tmp_path: Path, monkeypatch, make_font
) -> None:
    _, _, _, player = rgss_release
    patch = read_patch(_release_with_font(tmp_path, monkeypatch, make_font).read_bytes())
    apply(player, patch, lambda _line: None)
    outside = player.parent / "outside.txt"
    outside.write_bytes(b"not the patch's")
    record = json.loads((player / RECORD).read_text(encoding="utf-8"))
    record["added"].append({"name": "../outside.txt", "sha256": sha256_of(outside)})
    (player / RECORD).write_text(json.dumps(record), encoding="utf-8")

    restore(player, patch, lambda _line: None)
    assert outside.is_file() and not (player / "Fonts" / "Bokucho-UmePlusGothic.ttf").exists()


def test_a_different_font_still_gets_the_characters(
    rgss_release, tmp_path: Path, monkeypatch, make_font
) -> None:
    _, _, _, player = rgss_release
    patch = read_patch(_release_with_font(tmp_path, monkeypatch, make_font).read_bytes())
    make_font(player / "Fonts" / "ume.ttf", "UmePlus Gothic", BASE_DRAWS + "Z")
    said: list[str] = []

    apply(player, patch, said.append)
    assert any("版本不同" in line for line in said)
    with TTFont(player / "Fonts" / "Bokucho-UmePlusGothic.ttf") as copy:
        assert {ord("標"), ord("Z")} <= set(copy.getBestCmap())


def test_without_the_games_font_the_translation_still_applies(
    rgss_release, tmp_path: Path, monkeypatch, make_font
) -> None:
    generation, layout, _, player = rgss_release
    patch = read_patch(_release_with_font(tmp_path, monkeypatch, make_font).read_bytes())
    (player / "Fonts" / "ume.ttf").unlink()
    said: list[str] = []

    apply(player, patch, said.append)
    assert inspect(player, patch).state is State.PATCHED
    assert not (player / "Fonts" / "Bokucho-UmePlusGothic.ttf").exists()
    assert any("略過補字" in line for line in said)
    first = _scripts(player, generation, layout).entries[0].source
    assert first == 's1 = "回到標題"\n'


def test_the_applied_message_has_no_font_select_hint(
    rgss_release, tmp_path: Path, monkeypatch, make_font
) -> None:
    _, _, _, player = rgss_release
    patch = read_patch(_release_with_font(tmp_path, monkeypatch, make_font).read_bytes())

    apply(player, patch, lambda _line: None)
    assert (player / "Fonts" / "Bokucho-UmePlusGothic.ttf").is_file()
    assert "フォント選択" not in applied_message(patch, player)


def test_a_font_that_fails_to_fill_does_not_stop_the_release(
    rgss_release, tmp_path: Path, monkeypatch, make_font, caplog
) -> None:
    def broken(*_args):
        raise ValueError("an outline that does not draw")

    monkeypatch.setattr(rgss_font, "build_fill", broken)
    patch = read_patch(_release_with_font(tmp_path, monkeypatch, make_font).read_bytes())
    with ZipFile(BytesIO(patch.recipe)) as archive:
        assert json.loads(archive.read("recipe.json"))["font_fill"] is None
        assert "fonts/fill.ttf" not in archive.namelist()
    assert "an outline that does not draw" in caplog.text
