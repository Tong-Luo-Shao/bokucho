from __future__ import annotations

from types import SimpleNamespace

from bokucho.application.errors import ProviderFailure
from bokucho.application.text import translate_project
from bokucho.application.translation_limits import TranslationLane
from bokucho.infrastructure.tokens import TiktokenPromptCounter
from bokucho.model import StringFile, TextUnit
from bokucho.translators.base import TokenUsage, TranslationContext, Translator, build_prompt
from bokucho.translators.claude import ClaudeTranslator
from bokucho.translators.local import LocalTranslator
from provider_streams import ClaudeStream, ClaudeStreamClient, OpenAIStreamClient, openai_chunks


def test_openai_compatible_usage_includes_cached_prompt_tokens() -> None:
    usage = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=30,
        prompt_tokens_details=SimpleNamespace(cached_tokens=80),
    )
    translator = LocalTranslator("test-model", api_key="unused")
    translator._client = OpenAIStreamClient(openai_chunks("answer", usage=usage))

    assert translator.complete("system", "user") == "answer"

    usage = translator.consume_usage()
    assert usage.input_tokens == 120
    assert usage.output_tokens == 30
    assert usage.cached_input_tokens == 80
    assert usage.total_tokens == 150
    assert usage.requests == 1


def test_claude_usage_adds_cache_creation_and_read_tokens_to_input() -> None:
    usage = SimpleNamespace(
        input_tokens=20,
        output_tokens=15,
        cache_creation_input_tokens=40,
        cache_read_input_tokens=60,
    )
    translator = ClaudeTranslator("test-model", api_key="unused")
    translator._client = ClaudeStreamClient(ClaudeStream("answer", usage=usage))

    assert translator.complete("system", "user") == "answer"

    usage = translator.consume_usage()
    assert usage.input_tokens == 120
    assert usage.output_tokens == 15
    assert usage.cached_input_tokens == 100
    assert usage.total_tokens == 135
    assert usage.requests == 1


class _LaneUsageTranslator(Translator):
    name = "lane-usage"

    def __init__(self, usage: TokenUsage, *, fail: bool = False) -> None:
        super().__init__()
        self.usage = usage
        self.fail = fail

    def translate(self, batch, context, glossary=None):
        del context, glossary
        self._record_usage(self.usage)
        if self.fail:
            raise ProviderFailure("retry this lane")
        return ["譯文" for _ in batch]


def test_retry_rotation_aggregates_usage_from_every_lane(new_project) -> None:
    project = new_project("Usage rotation", source_lang="en")
    project.save_string_file(
        StringFile(
            source_file="script.txt",
            units=[TextUnit(id="u1", source="This sentence is written in English.")],
        ),
        "script.txt.json",
    )
    first = _LaneUsageTranslator(
        TokenUsage(input_tokens=10, output_tokens=2, requests=1),
        fail=True,
    )
    second = _LaneUsageTranslator(
        TokenUsage(input_tokens=20, output_tokens=5, requests=1)
    )

    result = translate_project(
        project,
        lanes=(TranslationLane(first), TranslationLane(second)),
        token_counter=TiktokenPromptCounter("test-model"),
        sleep=lambda _: None,
    )

    assert result.input_tokens == 30
    assert result.output_tokens == 7
    assert result.total_tokens == 37


def test_tokens_are_still_counted_when_tiktoken_cannot_fetch_its_table(monkeypatch) -> None:
    import requests
    import tiktoken

    def offline(*_args, **_kwargs):
        raise requests.ConnectionError("openaipublic.blob.core.windows.net unreachable")

    monkeypatch.setattr(tiktoken, "encoding_for_model", offline)
    monkeypatch.setattr(tiktoken, "get_encoding", offline)
    counter = TiktokenPromptCounter("local-model")
    context = TranslationContext(source_lang="ja", target_lang="zh-tw")
    unit = TextUnit(id="u1", source="おはよう")

    system, user = build_prompt([unit], context, None)
    assert counter.count([unit], context, None) == len(f"{system}\n{user}".encode())
