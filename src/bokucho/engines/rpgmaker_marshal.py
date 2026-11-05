"""Ruby Marshal for VX Ace, XP/VX written back by splicing, and the view the walk reads."""

from __future__ import annotations

import functools
import io
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class MissingMarshal(RuntimeError):
    """The VX Ace codec needs the rpgmaker extra."""


_INSTALL = (
    'VX Ace needs the rpgmaker extra: pip install "bokucho[rpgmaker]"\n'
    "(from a checkout: uv sync --extra rpgmaker)\n"
    "(MV and MZ need nothing extra; this is only for .rvdata2 games.)"
)


def _rubymarshal():
    """The library, imported at the point of use."""
    try:
        from rubymarshal import reader, writer
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised without the extra
        raise MissingMarshal(_INSTALL) from exc
    return reader, writer


def load_marshal(blob: bytes) -> Any:
    reader, _ = _rubymarshal()
    return reader.loads(blob)


def dump_marshal(payload: Any) -> bytes:
    _, writer = _rubymarshal()
    return writer.writes(payload)


class LegacyString(bytes):
    """A string read from a Ruby 1.8 stream, with the span of its length and bytes."""

    start: int
    end: int
    ivars: dict
    _edits: dict

    def __new__(cls, raw: bytes, start: int, end: int, ivars: dict, edits: dict) -> LegacyString:
        string = super().__new__(cls, raw)
        string.start, string.end, string.ivars, string._edits = start, end, ivars, edits
        return string

    def replaced(self, text: str) -> LegacyString:
        """Record ``text`` as this string's new bytes in the owning file's edits."""
        new = text.encode("utf-8")
        held = self._edits.setdefault((self.start, self.end), new)
        if held != new:
            raise ValueError(
                f"the string at offset {self.start} is already {held.decode('utf-8')!r}, "
                f"not {text!r}"
            )
        return LegacyString(new, self.start, self.end, self.ivars, self._edits)


@dataclass
class LegacyData:
    """A Ruby 1.8 Marshal stream as read, and the string edits waiting to be spliced in."""

    blob: bytes
    root: Any
    edits: dict[tuple[int, int], bytes]
    strings: list[LegacyString]


@functools.cache
def _legacy_reader() -> type:
    """A rubymarshal reader that keeps strings as bytes and notes where each one sits."""
    reader, _ = _rubymarshal()

    class LegacyReader(reader.Reader):
        def __init__(self, fd: io.BytesIO) -> None:
            super().__init__(fd)
            self.edits: dict[tuple[int, int], bytes] = {}
            self.strings: list[LegacyString] = []

        def read(self, in_ivar: bool = False) -> Any:
            """Strings here, in the link slot ``Reader.read`` would give them; the rest there."""
            at = self.fd.tell()
            token = self.fd.read(2)
            ivar = token == b'I"'
            if not ivar and token[:1] != b'"':
                self.fd.seek(at)
                return super().read(in_ivar)
            self.fd.seek(at + 2 if ivar else at + 1)
            index = len(self.objects)
            self.objects.append(None)
            start = self.fd.tell()
            size = self.read_long()
            raw = self.fd.read(size) if size >= 0 else b""
            if len(raw) != size:
                raise ValueError(
                    f"the string at offset {at} says {size} bytes, "
                    f"but the stream ends after {len(raw)}"
                )
            string = LegacyString(raw, start, self.fd.tell(), {}, self.edits)
            self.strings.append(string)
            if ivar:
                string.ivars.update(self.read_attributes())
            self.objects[index] = string
            return string

    return LegacyReader


def load_legacy(blob: bytes) -> LegacyData:
    """An XP/VX (Ruby 1.8) Marshal stream, its strings left as bytes with their spans."""
    fd = io.BytesIO(blob)
    if fd.read(2) != b"\x04\x08":
        raise ValueError("not a Ruby Marshal 4.8 stream")
    reader = _legacy_reader()(fd)
    try:
        root = reader.read()
    except struct.error as exc:
        raise ValueError(f"the stream ends inside a number at offset {fd.tell()}") from exc
    return LegacyData(blob, root, reader.edits, reader.strings)


def splice(blob: bytes, edits: Mapping[tuple[int, int], bytes]) -> bytes:
    """``blob`` with each span replaced by the Marshal length and bytes of its edit."""
    _, writer = _rubymarshal()
    out = io.BytesIO()
    at = 0
    for (start, end), new in sorted(edits.items()):
        out.write(blob[at:start])
        writer.Writer(out).write_long(len(new))
        out.write(new)
        at = end
    out.write(blob[at:])
    return out.getvalue()


def dump_legacy(data: LegacyData) -> bytes:
    """The game's own bytes with only the edited strings changed, checked by a reload."""
    if not data.edits:
        return data.blob
    out = splice(data.blob, data.edits)
    wanted = [data.edits.get((s.start, s.end), bytes(s)) for s in data.strings]
    if wanted != [bytes(s) for s in load_legacy(out).strings]:
        raise ValueError("the translated strings did not survive splicing")
    return out


def _wrap(value: Any, old: Any = None) -> Any:
    """A translation as Marshal needs to receive it, replacing ``old``."""
    from rubymarshal.classes import RubyString

    if type(value) is str:
        if isinstance(old, LegacyString):
            return old.replaced(value)
        return RubyString(value, {"E": True})
    return value


def _unwrap(value: Any) -> Any:
    """A Ruby value as the walk expects to meet it."""
    from rubymarshal.classes import RubyObject, RubyString

    if isinstance(value, LegacyString):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value
    if isinstance(value, RubyString):
        return str(value)
    if isinstance(value, RubyObject):
        return _Record(value)
    if isinstance(value, dict):
        return _Sequence([value[key] for key in sorted(value)])
    if isinstance(value, list):
        return _Sequence(value)
    return value


class _Record:
    """One Ruby object, read the way the walk reads a JSON record."""

    __slots__ = ("_obj",)

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def get(self, key: str, default: Any = None) -> Any:
        raw = self._obj.attributes.get(f"@{key}", default)
        return _unwrap(raw) if raw is not default else default

    def __getitem__(self, key: str) -> Any:
        return _unwrap(self._obj.attributes[f"@{key}"])

    def __setitem__(self, key: str, value: Any) -> None:
        name = f"@{key}"
        self._obj.attributes[name] = _wrap(value, self._obj.attributes.get(name))

    def items(self):
        return (
            (key.removeprefix("@"), _unwrap(value))
            for key, value in self._obj.attributes.items()
        )

    def __iter__(self):
        return (key.removeprefix("@") for key in self._obj.attributes)

    def __len__(self) -> int:
        return len(self._obj.attributes)


class _Sequence:
    """A Ruby array, or a hash's values, read the way the walk reads a list."""

    __slots__ = ("_items",)

    def __init__(self, items: list) -> None:
        self._items = items

    def __getitem__(self, index: int) -> Any:
        return _unwrap(self._items[index])

    def __setitem__(self, index: int, value: Any) -> None:
        self._items[index] = _wrap(value, self._items[index])

    def __iter__(self):
        return (_unwrap(item) for item in self._items)

    def __len__(self) -> int:
        return len(self._items)


def view(payload: Any) -> Any:
    """``payload`` as the walk reads it, writing through to the real objects."""
    return _unwrap(payload)
