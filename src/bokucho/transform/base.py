"""``Transform`` — extension point #3 (target post-processing)."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence

from bokucho.model import TextUnit
from bokucho.qa import around_markup


class Transform(ABC):
    """Abstract base class for target post-processors."""

    name: str = ""

    @abstractmethod
    def convert(self, text: str) -> str:
        """Rewrite one target string. Must be idempotent."""
        raise NotImplementedError

    def apply(
        self, units: list[TextUnit], protected: Sequence[re.Pattern[str]] = ()
    ) -> int:
        """Convert every unprotected, non-empty target in place."""
        changed = 0
        for unit in units:
            if unit.is_protected or not unit.target:
                continue
            converted = around_markup(unit.target, protected, self.convert)
            if converted != unit.target:
                unit.target = converted
                changed += 1
        return changed
