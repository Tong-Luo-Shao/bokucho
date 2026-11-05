"""A variable named in full-width brackets is not text to translate."""

from __future__ import annotations

import pytest

from bokucho.model import TextUnit
from bokucho.translate.prompts import BUILTIN_PRESETS
from bokucho.translators.base import TranslationContext, build_prompt

EXEMPTION = "A protected token is the exception"


@pytest.mark.parametrize("preset", BUILTIN_PRESETS, ids=lambda preset: preset.id)
def test_every_bracket_rule_leaves_protected_tokens_alone(preset) -> None:
    assert "Full-width brackets" in preset.system
    assert EXEMPTION in preset.system


def test_the_token_instructions_say_brackets_inside_a_token_stay() -> None:
    unit = TextUnit(id="u", source="<VAR 【魔物の名前】>を倒した！", engine_ref={})
    context = TranslationContext(source_lang="ja", target_lang="zh-Hant", engine="livemaker")

    system, _ = build_prompt([unit], context, None)

    assert "even the words inside full-width brackets" in system
