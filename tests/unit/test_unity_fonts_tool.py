"""Tests for the tool that builds the TMP fallback fonts of a Unity patch."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from build_unity_fonts import characters, find_editor, with_text_mesh_pro  # noqa: E402


def test_characters_keeps_drawable_bmp_text_with_shared_glyphs_on_a_second_line():
    names = {0x9: "tab", 0x20: "space", 0x41: "A", 0x200B: "zwsp", 0x4E00: "uni4E00"}
    names |= {
        0xE000: "pua",
        0x3042: "a-hira",
        0x20000: "ext-b",
        0xF900: "uni8C48",
        0x8C48: "uni8C48",
    }
    assert characters(names) == " Aあ一豈\n豈"


def test_find_editor_picks_the_newest_matching_install(tmp_path):
    for name in ("2019.2.17f1", "2019.2.9f1", "2021.3.45f1"):
        (tmp_path / name / "Editor").mkdir(parents=True)
        (tmp_path / name / "Editor" / "Unity.exe").write_bytes(b"")
    assert find_editor(tmp_path, "2019.2.").parent.parent.name == "2019.2.17f1"
    with pytest.raises(RuntimeError, match="install one with Unity Hub"):
        find_editor(tmp_path, "2022.3.")


def test_with_text_mesh_pro_adds_the_package_but_keeps_a_pinned_one():
    added = json.loads(with_text_mesh_pro('{"dependencies": {"com.unity.ugui": "1.0.0"}}', "2.0.1"))
    assert added["dependencies"]["com.unity.textmeshpro"] == "2.0.1"
    kept = json.loads(
        with_text_mesh_pro('{"dependencies": {"com.unity.textmeshpro": "3.0.9"}}', "3.0.6")
    )
    assert kept["dependencies"]["com.unity.textmeshpro"] == "3.0.9"
    unity6 = json.loads(with_text_mesh_pro('{"dependencies": {}}', None))
    assert unity6["dependencies"] == {"com.unity.ugui": "2.0.0"}


def _unitypackage(path: Path, entries: dict[str, dict[str, bytes]]) -> Path:
    import io
    import tarfile

    with tarfile.open(path, "w:gz") as archive:
        for guid, files in entries.items():
            for name, data in files.items():
                info = tarfile.TarInfo(f"./{guid}/{name}")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return path


def test_unpack_unitypackage_writes_assets_and_meta_files(tmp_path):
    from build_unity_fonts import unpack_unitypackage

    package = _unitypackage(
        tmp_path / "essentials.unitypackage",
        {
            "a1": {
                "pathname": b"Assets/TextMesh Pro/Shaders/TMP_SDF.shader\n00",
                "asset": b"Shader",
                "asset.meta": b"guid: a1",
            },
            "b2": {"pathname": b"Assets/TextMesh Pro", "asset.meta": b"folder"},
        },
    )
    unpack_unitypackage(package, tmp_path / "project")
    shader = tmp_path / "project" / "Assets" / "TextMesh Pro" / "Shaders" / "TMP_SDF.shader"
    assert shader.read_bytes() == b"Shader"
    assert shader.with_name("TMP_SDF.shader.meta").read_bytes() == b"guid: a1"

    unsafe = _unitypackage(
        tmp_path / "unsafe.unitypackage", {"c3": {"pathname": b"../evil.txt", "asset": b"x"}}
    )
    with pytest.raises(RuntimeError, match="unsafe path"):
        unpack_unitypackage(unsafe, tmp_path / "project")


def test_fill_names_the_editor_version_so_a_windowed_editor_does_not_ask(tmp_path):
    from build_unity_fonts import _fill

    face = tmp_path / "face.otf"
    face.write_bytes(b"OTTO")
    unity = tmp_path / "2019.2.17f1" / "Editor" / "Unity.exe"
    _fill(tmp_path / "project", face, unity)
    version = tmp_path / "project" / "ProjectSettings" / "ProjectVersion.txt"
    assert version.read_text(encoding="utf-8") == "m_EditorVersion: 2019.2.17f1\n"
    assert (tmp_path / "project" / "Assets" / "face.otf").read_bytes() == b"OTTO"
    assert (tmp_path / "project" / "Assets" / "Editor" / "BokuchoFontBuilder.cs").is_file()
