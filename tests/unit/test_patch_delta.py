from __future__ import annotations

import hashlib
import io
import random

import pytest

from bokucho.patcher.delta import apply_delta, make_delta, window_log

MB = 1 << 20


def _versions() -> tuple[bytes, bytes]:
    """A file of noise, and the same file with a block inserted and a block changed."""
    original = random.Random(7).randbytes(4 * MB)
    patched = original[:MB] + b"inserted" * 512 + original[MB : 3 * MB] + b"\0" * 4096
    patched += original[3 * MB + 4096 :]
    return original, patched


def test_the_delta_turns_the_original_into_the_patched_file() -> None:
    original, patched = _versions()
    delta, window = make_delta(original, patched)
    out = io.BytesIO()

    digest = apply_delta(original, delta, window, out)

    assert out.getvalue() == patched
    assert digest == hashlib.sha256(patched).hexdigest()


def test_a_small_change_makes_a_small_delta() -> None:
    original, patched = _versions()
    delta, _ = make_delta(original, patched)
    assert len(delta) < 64 * 1024


def test_a_delta_cut_short_is_refused() -> None:
    original, patched = _versions()
    delta, window = make_delta(original, patched)
    with pytest.raises(ValueError):
        apply_delta(original, delta[: len(delta) // 2], window, io.BytesIO())


def test_the_window_reaches_across_the_larger_file() -> None:
    assert window_log(335 * MB, 334 * MB) == 30
    assert window_log(1) == 10
    with pytest.raises(ValueError):
        window_log(1 << 31)
