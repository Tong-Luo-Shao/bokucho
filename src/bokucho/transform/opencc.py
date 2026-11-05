"""OpenCC transform — Chinese script conversion + regional localization."""

from __future__ import annotations

from pathlib import Path

from bokucho.transform.base import Transform

try:
    import opencc as _opencc

    _IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    _IMPORT_ERROR = exc

_MISSING_DEP_MSG = (
    "The opencc transform requires the OpenCC package. Install it with:\n"
    "    pip install 'bokucho[opencc]'"
)


def _load_custom_dict(path: Path) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        src, _, dst = line.partition("\t")
        if src and dst:
            pairs[src] = dst
    return pairs


class OpenCCTransform(Transform):
    """Script conversion + localization via OpenCC, with a custom-term overlay."""

    name = "opencc"

    def __init__(
        self,
        config: str = "s2twp",
        custom_dict: str | Path | None = None,
        base_dict: str | Path | None = None,
    ) -> None:
        if _IMPORT_ERROR is not None:
            raise ImportError(_MISSING_DEP_MSG) from _IMPORT_ERROR
        self._cc = _opencc.OpenCC(config)
        self._custom = {
            **(_load_custom_dict(Path(base_dict)) if base_dict else {}),
            **(_load_custom_dict(Path(custom_dict)) if custom_dict else {}),
        }
        self._keys_longest_first = sorted(self._custom, key=len, reverse=True)

    def convert(self, text: str) -> str:
        out = self._cc.convert(text)
        # ponytail: plain longest-first substring replace; a real tokenizer only
        # if term collisions ever bite. Known ceiling: a value containing its own
        # key (術語→XX術語) re-replaces on the next run and breaks idempotency.
        for src in self._keys_longest_first:
            out = out.replace(src, self._custom[src])
        return out
