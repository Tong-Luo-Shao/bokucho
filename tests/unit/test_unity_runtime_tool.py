"""The Unity runtime a release carries is fetched by pinned hash and laid out per architecture."""

import hashlib
import importlib.util
import os
import sys
import zipfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(f"bokucho_tool_{name}", TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _zip(path: Path, files: dict[str, bytes]) -> str:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_stage_runtime_merges_xunity_into_each_bepinex(tmp_path, monkeypatch):
    tool = _tool("fetch_unity_runtime")
    served = tmp_path / "served"
    served.mkdir()
    pins = {}
    for name, files in {
        "x64.zip": {"winhttp.dll": b"64", "BepInEx/core/BepInEx.dll": b"core"},
        "x86.zip": {"winhttp.dll": b"86", "BepInEx/core/BepInEx.dll": b"core"},
        "xu.zip": {"BepInEx/plugins/XUnity.AutoTranslator/Core.dll": b"xu"},
    }.items():
        pins[name] = tool.Download(f"https://example.test/{name}", _zip(served / name, files))
    monkeypatch.setattr(tool, "BEPINEX", {"x64": pins["x64.zip"], "x86": pins["x86.zip"]})
    monkeypatch.setattr(tool, "XUNITY", pins["xu.zip"])
    unpinned = tool.Download("https://example.test/unpinned.zip", "")
    monkeypatch.setattr(tool, "BEPINEX_IL2CPP", {"x64": unpinned, "x86": unpinned})
    monkeypatch.setattr(tool, "XUNITY_IL2CPP", unpinned)

    def downloader(url: str, destination: Path) -> None:
        destination.write_bytes((served / url.rsplit("/", 1)[1]).read_bytes())

    fonts = tmp_path / "unity-fonts"
    fonts.mkdir()
    (fonts / "bokucho_tc_sdf_u2019").write_bytes(b"UnityFS")
    staged = tool.stage_runtime(
        tmp_path / "runtime", tmp_path / "cache", downloader=downloader, fonts=fonts
    )
    assert (staged / "fonts" / "bokucho_tc_sdf_u2019").read_bytes() == b"UnityFS"
    assert (staged / "x64" / "winhttp.dll").read_bytes() == b"64"
    assert (staged / "x86" / "winhttp.dll").read_bytes() == b"86"
    assert (staged / "x86" / "BepInEx/plugins/XUnity.AutoTranslator/Core.dll").is_file()
    assert not (staged / "il2cpp-x64").exists()  # unpinned: never staged

    monkeypatch.setattr(tool, "XUNITY", tool.Download("https://example.test/xu.zip", "0" * 64))
    with pytest.raises(RuntimeError, match="pinned hash"):
        tool.stage_runtime(tmp_path / "again", tmp_path / "cache2", downloader=downloader)


def test_the_release_carries_the_unity_runtime_and_unitypy(tmp_path):
    build = _tool("build")
    runtime = tmp_path / "unity-runtime"
    arguments = build.pyinstaller_arguments(
        tmp_path / "stub.exe", tmp_path / "winmm.dll", tmp_path / "font", runtime
    )
    assert f"--add-data={runtime}{os.pathsep}bokucho/unity-runtime" in arguments
    assert "--collect-all=UnityPy" in arguments
    assert "--hidden-import=bokucho.engines.unity" in arguments


def test_pinned_il2cpp_loaders_are_staged_beside_the_mono_ones(tmp_path, monkeypatch):
    tool = _tool("fetch_unity_runtime")
    served = tmp_path / "served"
    served.mkdir()
    pins = {}
    for name, files in {
        "x64.zip": {"winhttp.dll": b"64", "BepInEx/core/BepInEx.dll": b"core"},
        "x86.zip": {"winhttp.dll": b"86", "BepInEx/core/BepInEx.dll": b"core"},
        "xu.zip": {"BepInEx/plugins/XUnity.AutoTranslator/Core.dll": b"xu"},
        "il64.zip": {"winhttp.dll": b"i64", "dotnet/coreclr.dll": b"clr"},
        "il86.zip": {"winhttp.dll": b"i86", "dotnet/coreclr.dll": b"clr"},
        "xuil.zip": {"BepInEx/plugins/XUnity.AutoTranslator/Il2Cpp.dll": b"xu6"},
    }.items():
        pins[name] = tool.Download(f"https://example.test/{name}", _zip(served / name, files))
    monkeypatch.setattr(tool, "BEPINEX", {"x64": pins["x64.zip"], "x86": pins["x86.zip"]})
    monkeypatch.setattr(tool, "XUNITY", pins["xu.zip"])
    monkeypatch.setattr(
        tool, "BEPINEX_IL2CPP", {"x64": pins["il64.zip"], "x86": pins["il86.zip"]}
    )
    monkeypatch.setattr(tool, "XUNITY_IL2CPP", pins["xuil.zip"])

    def downloader(url: str, destination: Path) -> None:
        destination.write_bytes((served / url.rsplit("/", 1)[1]).read_bytes())

    staged = tool.stage_runtime(
        tmp_path / "runtime", tmp_path / "cache", downloader=downloader, fonts=tmp_path / "none"
    )
    assert (staged / "il2cpp-x64" / "winhttp.dll").read_bytes() == b"i64"
    assert (staged / "il2cpp-x86" / "dotnet" / "coreclr.dll").read_bytes() == b"clr"
    assert (staged / "il2cpp-x64" / "BepInEx/plugins/XUnity.AutoTranslator/Il2Cpp.dll").is_file()
    assert not (staged / "x64" / "dotnet").exists()
    # The archives carry no license texts, so a patch carries the terms itself.
    mono = {path.name for path in (staged / "x64" / "BepInEx" / "licenses").iterdir()}
    il2cpp = {path.name for path in (staged / "il2cpp-x86" / "BepInEx" / "licenses").iterdir()}
    assert mono == set(tool.MONO_LICENSES)
    assert il2cpp == {*tool.IL2CPP_LICENSES, "GPL-3.0.txt"}
    for name in tool.IL2CPP_LICENSES:
        assert (tool.LICENSES / name).stat().st_size > 500


def test_an_archive_path_with_a_backslash_is_refused(tmp_path):
    tool = _tool("fetch_unity_runtime")
    archive = tmp_path / "bad.zip"
    _zip(archive, {"BepInEx\\..\\..\\evil.dll": b"x"})
    with pytest.raises(RuntimeError, match="unsafe path"):
        tool._extract(archive, tmp_path / "out")


def test_print_pins_reports_the_digest_of_each_unpinned_archive(tmp_path, monkeypatch):
    tool = _tool("fetch_unity_runtime")
    served = tmp_path / "served"
    served.mkdir()
    digest = _zip(served / "il.zip", {"winhttp.dll": b"x"})
    unpinned = tool.Download("https://example.test/il.zip", "")
    monkeypatch.setattr(tool, "BEPINEX_IL2CPP", {"x64": unpinned})
    monkeypatch.setattr(tool, "XUNITY_IL2CPP", tool.Download("https://example.test/xu", "a" * 64))

    def downloader(url: str, destination: Path) -> None:
        destination.write_bytes((served / url.rsplit("/", 1)[1]).read_bytes())

    assert tool.print_pins(tmp_path / "cache", downloader) == {unpinned.url: digest}
    with pytest.raises(RuntimeError, match="no pinned hash"):
        tool._fetch(unpinned, tmp_path / "cache", downloader)
