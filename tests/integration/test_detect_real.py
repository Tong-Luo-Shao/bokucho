"""Gated detection checks against real games."""

import os
from pathlib import Path

import pytest

from bokucho.detect import CONFIDENT, detect

LSB = os.environ.get("BOKUCHO_TEST_LSB")
SDK = os.environ.get("BOKUCHO_TEST_RENPY_SDK")


@pytest.mark.skipif(
    not LSB, reason="set BOKUCHO_TEST_LSB to an unpacked LiveMaker directory"
)
def test_real_livemaker_directory_detected():
    path = Path(LSB)
    if path.is_file():
        path = path.parent
    top = detect(path)[0]
    assert top.engine == "livemaker"
    assert top.supported
    assert top.confidence >= CONFIDENT


@pytest.mark.skipif(
    not SDK, reason="set BOKUCHO_TEST_RENPY_SDK to a Ren'Py SDK directory"
)
def test_real_renpy_project_detected():
    top = detect(Path(SDK) / "the_question")[0]
    assert top.engine == "renpy"
    assert top.supported
    assert top.confidence >= CONFIDENT
