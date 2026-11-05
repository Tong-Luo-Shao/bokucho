"""CP932 font-proxy map for LiveMaker."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from bokucho.sync.atomic import atomic_write_text


def cp932_ok(ch: str) -> bool:
    """True if ``ch`` encodes to CP932 (can sit literally in a .lsb)."""
    try:
        ch.encode("cp932")
        return True
    except UnicodeEncodeError:
        return False


@lru_cache(maxsize=1)
def _kanji_pool() -> tuple[str, ...]:
    """Every CP932 double-byte kanji, in code order (tail = rarest, JIS level 2)."""
    pool: list[str] = []
    seen: set[str] = set()
    for hi in range(0x81, 0xFD):
        for lo in [*range(0x40, 0x7F), *range(0x80, 0xFD)]:
            try:
                ch = bytes((hi, lo)).decode("cp932")
            except UnicodeDecodeError:
                continue
            if len(ch) == 1 and 0x4E00 <= ord(ch) <= 0x9FFF and ch not in seen:
                seen.add(ch)
                pool.append(ch)
    return tuple(pool)


@lru_cache(maxsize=1)
def _donor_order() -> tuple[str, ...]:
    """Rarest first: JIS level 1 last, and first the kanji neither Big5 nor GB2312 writes.

    GBK is no filter: it writes every CP932 kanji.
    """
    return tuple(
        sorted(
            reversed(_kanji_pool()),
            key=lambda ch: (
                0x889F <= int.from_bytes(ch.encode("cp932"), "big") <= 0x9872,
                _encodes(ch, "cp950") or _encodes(ch, "gb2312"),
            ),
        )
    )


def _encodes(ch: str, codec: str) -> bool:
    try:
        ch.encode(codec)
        return True
    except UnicodeEncodeError:
        return False


class MissingGlyphError(KeyError):
    """A non-CP932 character has no proxy mapping (rebuild with patch-runtime)."""


class ProxyMap:
    """Bidirectional real-char <-> CP932-donor mapping, stored as JSON."""

    def __init__(self, real2proxy: dict[str, str] | None = None) -> None:
        self.real2proxy: dict[str, str] = {}
        self.proxy2real: dict[str, str] = {}
        self.shadowed: list[str] = []
        for real, proxy in (real2proxy or {}).items():
            if proxy in self.proxy2real:
                self.shadowed.append(real)
                continue
            self.real2proxy[real] = proxy
            self.proxy2real[proxy] = real

    @classmethod
    def load(cls, path: str | Path) -> ProxyMap:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        atomic_write_text(
            path,
            json.dumps(self.real2proxy, ensure_ascii=False, indent=1) + "\n",
        )

    def encode(self, text: str) -> str:
        """Text as stored in a .lsb: CP932 chars pass through, others -> donor."""
        out = []
        for ch in text:
            if cp932_ok(ch):
                out.append(ch)
            elif ch in self.real2proxy:
                out.append(self.real2proxy[ch])
            else:
                raise MissingGlyphError(ch)
        return "".join(out)

    def decode(self, text: str) -> str:
        """Stored text -> intended text (donor -> real; everything else unchanged)."""
        return "".join(self.proxy2real.get(ch, ch) for ch in text)

    def free_donors(self, reserved: set[str]) -> int:
        """Unmap every char whose donor the text now writes literally; return how many."""
        colliding = [real for real, proxy in self.real2proxy.items() if proxy in reserved]
        for real in colliding:
            del self.proxy2real[self.real2proxy.pop(real)]
        return len(colliding)

    def extend(self, needed: set[str], reserved: set[str]) -> int:
        """Map every not-yet-mapped char in ``needed`` to an unused rare donor."""
        todo = sorted(ch for ch in needed if ch not in self.real2proxy)
        if not todo:
            return 0
        used = reserved | set(self.proxy2real) | set(self.real2proxy)
        donors = (ch for ch in _donor_order() if ch not in used)
        for ch in todo:
            donor = next(donors, None)
            if donor is None:
                raise RuntimeError("CP932 donor pool exhausted")
            self.real2proxy[ch] = donor
            self.proxy2real[donor] = ch
        return len(todo)
