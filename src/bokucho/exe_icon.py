"""The icon a Windows program shows, read from one and drawn onto another."""

from __future__ import annotations

import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

RT_ICON = 3
RT_GROUP_ICON = 14
_RESOURCE_DIRECTORY = 2
_GROUP_ENTRY = struct.Struct("<BBBBHHIH")


@dataclass(frozen=True)
class ExeIcon:
    group: bytes
    images: tuple[tuple[int, bytes], ...]


def exe_icon(data: bytes) -> ExeIcon | None:
    """The icon Explorer shows for the program ``data``, None for no program or no icon."""
    try:
        groups = _resources(data, RT_GROUP_ICON)
        if not groups:
            return None
        images = {name: image for (name, _), image in _resources(data, RT_ICON).items()}
        group = next(iter(groups.values()))
        (count,) = struct.unpack_from("<H", group, 4)
        ids = [_GROUP_ENTRY.unpack_from(group, 6 + _GROUP_ENTRY.size * i)[-1] for i in range(count)]
        return ExeIcon(group, tuple((i, images[i]) for i in ids))
    except (struct.error, KeyError, IndexError, TypeError, UnicodeDecodeError):
        return None


def with_icon(program: bytes, icon: ExeIcon) -> bytes:
    """``program`` showing ``icon`` in place of its own, with what follows its image kept."""
    # ponytail: Windows' own writer; a release built elsewhere keeps the patcher's icon.
    if sys.platform != "win32":
        return program
    import ctypes
    from ctypes import c_void_p, wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel32.UpdateResourceW.argtypes = [
        wintypes.HANDLE,
        c_void_p,
        c_void_p,
        wintypes.WORD,
        c_void_p,
        wintypes.DWORD,
    ]
    kernel32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]

    def update(handle, kind: int, name: int | str, language: int, data: bytes | None) -> None:
        key = name if isinstance(name, int) else ctypes.cast(ctypes.c_wchar_p(name), c_void_p)
        buffer = None if data is None else ctypes.create_string_buffer(data, len(data))
        size = 0 if data is None else len(data)
        if not kernel32.UpdateResourceW(handle, kind, key, language, buffer, size):
            raise ctypes.WinError(ctypes.get_last_error())

    end = _image_end(program)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "program.exe"
        path.write_bytes(program[:end])
        handle = kernel32.BeginUpdateResourceW(str(path), False)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            for kind in (RT_GROUP_ICON, RT_ICON):
                for name, language in _resources(program, kind):
                    update(handle, kind, name, language, None)
            for name, image in icon.images:
                update(handle, RT_ICON, name, 0, image)
            update(handle, RT_GROUP_ICON, 1, 0, icon.group)
        except BaseException:
            kernel32.EndUpdateResourceW(handle, True)
            raise
        if not kernel32.EndUpdateResourceW(handle, False):
            raise ctypes.WinError(ctypes.get_last_error())
        return path.read_bytes() + program[end:]


def _sections(data: bytes) -> tuple[int, list[tuple[int, int, int, int]]]:
    """Where the optional header starts, and each section's layout."""
    if data[:2] != b"MZ":
        raise TypeError("not a program")
    (header,) = struct.unpack_from("<I", data, 0x3C)
    if data[header : header + 4] != b"PE\0\0":
        raise TypeError("not a program")
    count, optional_size = struct.unpack_from("<H12xH", data, header + 6)
    optional = header + 24
    table = optional + optional_size
    return optional, [
        struct.unpack_from("<8xIIII", data, table + 40 * index) for index in range(count)
    ]


def _image_end(data: bytes) -> int:
    """Where the program's image ends: what follows is no part of it."""
    _, sections = _sections(data)
    return max(offset + size for _, _, size, offset in sections)


def _resources(data: bytes, kind: int) -> dict[tuple[int | str, int], bytes]:
    """(name, language) -> data of each resource of ``kind``, in the directory's order."""
    optional, sections = _sections(data)
    (magic,) = struct.unpack_from("<H", data, optional)
    directories = optional + (96 if magic == 0x10B else 112)
    rva, _ = struct.unpack_from("<II", data, directories + 8 * _RESOURCE_DIRECTORY)
    if not rva:
        return {}

    def offset(address: int) -> int:
        for virtual_size, virtual, raw_size, raw in sections:
            if virtual <= address < virtual + max(virtual_size, raw_size):
                return address - virtual + raw
        raise TypeError("an address no section holds")

    base = offset(rva)

    def entries(at: int):
        named, numbered = struct.unpack_from("<HH", data, at + 12)
        for index in range(named + numbered):
            name, child = struct.unpack_from("<II", data, at + 16 + 8 * index)
            if name & 0x80000000:
                start = base + (name & 0x7FFFFFFF)
                (length,) = struct.unpack_from("<H", data, start)
                name = data[start + 2 : start + 2 + 2 * length].decode("utf-16-le")
            yield name, base + (child & 0x7FFFFFFF)

    found = {}
    for found_kind, names in entries(base):
        if found_kind != kind:
            continue
        for name, languages in entries(names):
            for language, entry in entries(languages):
                address, size = struct.unpack_from("<II", data, entry)
                start = offset(address)
                found[(name, language)] = data[start : start + size]
    return found
