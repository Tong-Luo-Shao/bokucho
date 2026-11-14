"""Repacking a LiveMaker archive: what the overlay may be asked for."""

from __future__ import annotations

from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

pytest.importorskip("livemaker")

from bokucho.engines.livemaker import _overlay_replacements  # noqa: E402


class _UntouchedOverlay:
    """An overlay directory that fails the test the moment it is looked at."""

    def resolve(self):
        raise AssertionError("the overlay was looked at before every name was checked")


@pytest.mark.parametrize("unsafe", ["CON", "../outside.lsb"])
def test_an_unsafe_entry_refuses_the_archive_before_the_overlay_is_touched(unsafe: str) -> None:
    entries = [
        SimpleNamespace(path=PureWindowsPath("good.lsb")),
        SimpleNamespace(path=PureWindowsPath(unsafe)),
    ]

    with pytest.raises(ValueError, match="LiveMaker archive entry"):
        _overlay_replacements(entries, _UntouchedOverlay())
