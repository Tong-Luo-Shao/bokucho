"""Which strings of a font-name expression name the face the proxy takes over."""

from __future__ import annotations

import pytest

pytest.importorskip("livemaker")

from livemaker.lsb.core import OpeData, OpeDataType, OpeFuncType, Param, ParamType  # noqa: E402

from bokucho.engines.livemaker import _face_literals  # noqa: E402


def _string(value: str) -> Param:
    return Param(value, ParamType.Str)


def _var(name: str) -> Param:
    return Param(name, ParamType.Var)


def test_a_face_named_outright_is_taken_over() -> None:
    face = _string("ＭＳ ゴシック")
    entries = [
        OpeData(OpeDataType.To, "____0", operands=[face]),
        OpeData(OpeDataType.To, "____arg", operands=[_var("____0")]),
    ]

    assert _face_literals(entries) == [face]


def test_the_box_a_face_is_copied_from_is_not_a_face() -> None:
    entries = [
        OpeData(OpeDataType.To, "____0", operands=[_string("メッセージボックス")]),
        OpeData(
            OpeDataType.Func,
            "____1",
            OpeFuncType.GetProp,
            [_var("____0"), Param(15, ParamType.Int)],
        ),
        OpeData(OpeDataType.To, "____arg", operands=[_var("____1")]),
    ]

    assert _face_literals(entries) == []
