"""tiktoken adapter for complete translation prompt accounting."""

from __future__ import annotations

import logging
from collections.abc import Mapping

import tiktoken

from bokucho.model import TextUnit
from bokucho.qa import source_layout
from bokucho.translators.base import TranslationContext, build_prompt

_log = logging.getLogger(__name__)


class TiktokenPromptCounter:
    def __init__(self, model: str) -> None:
        try:
            try:
                self._encoding = tiktoken.encoding_for_model(model)
            except KeyError:
                self._encoding = tiktoken.get_encoding("o200k_base")
        except OSError as error:
            _log.warning(
                "tiktoken could not fetch its token table (%s); counting every UTF-8 "
                "byte as a token, which makes requests smaller than they need be.",
                error,
            )
            self._encoding = None

    def count(
        self,
        batch: list[TextUnit],
        context: TranslationContext,
        glossary: Mapping[str, str] | None,
    ) -> int:
        regions = [unit.model_copy(update={
            "source": source_layout(unit.source, context.source_lang, context.engine)[1],
        }) for unit in batch]
        system, user = build_prompt(regions, context, glossary)
        prompt = f"{system}\n{user}"
        if self._encoding is None:
            return len(prompt.encode())
        return len(self._encoding.encode(prompt, disallowed_special=()))
