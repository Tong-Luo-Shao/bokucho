"""Download the BepInEx and XUnity.AutoTranslator that fit the player's Unity game.

A Mono game of any Unity version gets BepInEx 5. An IL2CPP game gets a BepInEx 6
bleeding-edge build, which also brings the .NET runtime it hosts the game's code with
in a ``dotnet`` folder; whether that build can load the game follows from its Unity
version. Each archive is held to the SHA-256 of the file its project published.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import re
import struct
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

from bokucho.patcher.core import Log, PatchError, _report

#: The version a serialized file or bundle header names, e.g. 2019.4.29f1 or 6000.3.10f1.
_UNITY_VERSION = re.compile(rb"(?:[56]\d{3}|20\d\d|5)\.\d+\.\d+[abfpx]\d+")
_USER_AGENT = "Bokucho-patcher"
_TIMEOUT = 60


@dataclass(frozen=True)
class Download:
    name: str
    url: str
    sha256: str


_BEPINEX_5 = {
    "x64": Download(
        "BepInEx 5.4.23.6 (x64)",
        "https://github.com/BepInEx/BepInEx/releases/download/v5.4.23.6/"
        "BepInEx_win_x64_5.4.23.6.zip",
        "6c57a714a892efb4e9e1fef63d608a3391f30ec984031c465404e4ee381a0755",
    ),
    "x86": Download(
        "BepInEx 5.4.23.6 (x86)",
        "https://github.com/BepInEx/BepInEx/releases/download/v5.4.23.6/"
        "BepInEx_win_x86_5.4.23.6.zip",
        "a4024dafaca5d3784097e34b3c2921415030c0b461e52f30053ed8914be9622d",
    ),
}
_XUNITY = Download(
    "XUnity.AutoTranslator 5.6.3.1",
    "https://github.com/bbepis/XUnity.AutoTranslator/releases/download/v5.6.3.1/"
    "XUnity.AutoTranslator-BepInEx-5.6.3.1.zip",
    "6c3ff03aafe9708f25c2115c0c04c255b6a9d88e716896ae7bacb590a8777609",
)
#: XUnity.AutoTranslator's IL2CPP plugin is built against BepInEx 6 build 704, and
#: 6.0.0-pre.2 is the older build 697. Build 755 and later read the IL2CPP metadata
#: of Unity 6 up to version 106.
_BEPINEX_6 = {
    "x64": Download(
        "BepInEx 6.0.0-be.788 IL2CPP (x64)",
        "https://builds.bepinex.dev/projects/bepinex_be/788/"
        "BepInEx-Unity.IL2CPP-win-x64-6.0.0-be.788%2B5b766a3.zip",
        "f4cc496bd098a0df4164b81e3737297707f13a47c2478dba2f60eefab784817a",
    ),
    "x86": Download(
        "BepInEx 6.0.0-be.788 IL2CPP (x86)",
        "https://builds.bepinex.dev/projects/bepinex_be/788/"
        "BepInEx-Unity.IL2CPP-win-x86-6.0.0-be.788%2B5b766a3.zip",
        "d5954a5993ec39cd1133603d85bff93875d30b6411b712cc13dcf03c8e08a4d3",
    ),
}
_XUNITY_IL2CPP = Download(
    "XUnity.AutoTranslator 5.6.3.1 IL2CPP",
    "https://github.com/bbepis/XUnity.AutoTranslator/releases/download/v5.6.3.1/"
    "XUnity.AutoTranslator-BepInEx-IL2CPP-5.6.3.1.zip",
    "f1d4d3151a061edfcda815e4472fe1f1de8756bdc23708c9b5ebb9bfe736fb1f",
)
#: IL2CPP metadata version 107 starts at Unity 6000.5.1, and no BepInEx build reads it.
_IL2CPP_UNREADABLE = (6000, 5, 1)


def unity_version(data: Path) -> str | None:
    """The Unity version a game's data folder was built with, e.g. ``2019.4.29f1``."""
    for name in ("globalgamemanagers", "data.unity3d"):
        path = data / name
        if path.is_file():
            with path.open("rb") as stream:
                match = _UNITY_VERSION.search(stream.read(256))
            if match:
                return match[0].decode("ascii")
    return None


def architecture(binary: Path) -> str:
    """``x64`` or ``x86``: the machine a Windows PE file (UnityPlayer.dll) is built for."""
    with binary.open("rb") as stream:
        head = stream.read(4096)
    try:
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        machine = struct.unpack_from("<H", head, pe + 4)[0]
    except struct.error:
        pe, machine = 0, 0
    if machine not in (0x8664, 0x14C) or head[pe : pe + 4] != b"PE\0\0":
        raise ValueError(f"{binary.name} is not a Windows x64 or x86 program.")
    return "x64" if machine == 0x8664 else "x86"


def il2cpp_loadable(version: str | None) -> bool:
    """Whether a BepInEx build loads an IL2CPP game of this Unity version, if it is known."""
    release = tuple(int(part) for part in re.findall(r"\d+", version or "")[:3])
    return release < _IL2CPP_UNREADABLE


def downloads(backend: str, version: str | None, machine: str) -> tuple[Download, ...]:
    """BepInEx, then the XUnity.AutoTranslator built for it, for one game."""
    if backend != "il2cpp":
        return _BEPINEX_5[machine], _XUNITY
    if not il2cpp_loadable(version):
        raise PatchError(
            f"這個遊戲以 Unity {version}（IL2CPP）製作，目前還沒有 BepInEx 能載入它，"
            "所以無法安裝翻譯。遊戲檔案沒有被改動。"
        )
    return _BEPINEX_6[machine], _XUNITY_IL2CPP


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return response.read()


def fetch_loader(data: Path, player: Path, backend: str, log: Log | None) -> dict[str, bytes]:
    """The files BepInEx and XUnity.AutoTranslator put beside the game, by their path in it.

    ``data`` is the game's ``*_Data`` folder and ``player`` its UnityPlayer.dll.
    """
    version = unity_version(data)
    machine = architecture(player)
    kind = "IL2CPP" if backend == "il2cpp" else "Mono"
    _report(log, f"遊戲的 Unity 版本：{version or '無法判斷'}（{kind}，{machine}）。")
    files: dict[str, bytes] = {}
    for download in downloads(backend, version, machine):
        _report(log, f"正在下載 {download.name}。")
        try:
            archive = _get(download.url)
        except (OSError, http.client.HTTPException) as error:
            raise PatchError(
                f"無法下載 {download.name}，遊戲檔案沒有被改動。請確認網路連線後再試一次。"
                f"（{error}）"
            ) from error
        if hashlib.sha256(archive).hexdigest() != download.sha256:
            raise PatchError(f"下載的 {download.name} 與預期的檔案不符，遊戲檔案沒有被改動。")
        with ZipFile(io.BytesIO(archive)) as source:
            for info in source.infolist():
                if not info.is_dir():
                    files[info.filename] = source.read(info)
    return files
