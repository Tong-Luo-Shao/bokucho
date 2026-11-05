"""Punctuation normalization for zh-tw."""

from __future__ import annotations

from bokucho.language import normalize_language_tag
from bokucho.transform.base import Transform

_PROFILES = {
    # ponytail: quotes only — the one conversion that is always safe. Ellipsis,
    # half-width punctuation etc. can be added per-profile when a real project
    # shows the need (they risk touching tags/layout).
    "zh-tw": str.maketrans({"“": "「", "”": "」", "‘": "『", "’": "』"}),
}


class PunctuationTransform(Transform):
    """Normalize punctuation to the target region's convention."""

    name = "punctuation"

    def __init__(self, profile: str = "zh-tw") -> None:
        try:
            self._table = _PROFILES[normalize_language_tag(profile)]
        except KeyError:
            raise ValueError(
                f"Unknown punctuation profile: {profile!r}. Available: {', '.join(_PROFILES)}"
            ) from None

    def convert(self, text: str) -> str:
        return text.translate(self._table)
