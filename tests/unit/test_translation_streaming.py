"""Replies are read as they stream in, and a run shows what has arrived."""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from bokucho.application.contracts import ProgressEvent
from bokucho.application.errors import ProviderRefused
from bokucho.application.text import translate_project
from bokucho.application.translation_limits import TranslationLane
from bokucho.model import StringFile, TextUnit
from bokucho.project import Project
from bokucho.translators.base import Translator
from bokucho.translators.claude import ClaudeTranslator
from bokucho.translators.local import LocalTranslator
from provider_streams import ClaudeStreamClient, OpenAIStreamClient

REPLY = '["你好", "再見"]'


def test_an_openai_compatible_reply_is_read_as_it_streams() -> None:
    client = OpenAIStreamClient(REPLY)
    translator = LocalTranslator("test-model", api_key="unused")
    translator._client = client
    received: list[int] = []
    translator.set_stream_listener(received.append)

    assert translator.complete("system", "user") == REPLY

    [request] = client.requests
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}
    assert received[0] == 0
    assert received[-1] == len(REPLY)
    assert received == sorted(received)
    assert len(received) > 2


def test_an_openai_compatible_stream_with_no_text_is_a_refusal() -> None:
    blocked = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason="stop")],
            usage=None,
        )
    ]
    translator = LocalTranslator("test-model", api_key="unused")
    translator._client = OpenAIStreamClient(blocked)

    with pytest.raises(ProviderRefused):
        translator.complete("system", "user")


def test_a_claude_reply_is_read_as_it_streams() -> None:
    client = ClaudeStreamClient(REPLY)
    translator = ClaudeTranslator("test-model", api_key="unused")
    translator._client = client
    received: list[int] = []
    translator.set_stream_listener(received.append)

    assert translator.complete("system", "user") == REPLY

    [request] = client.requests
    assert request["model"] == "test-model"
    assert request["max_tokens"] == 8192
    assert received[0] == 0
    assert received[-1] == len(REPLY)
    assert len(received) > 2


class _Streaming(Translator):
    """Answers every line, telling the listener how much of the reply came in."""

    name = "streaming"

    def translate(self, batch, context, glossary=None):
        del context, glossary
        listener = self._stream_listener
        assert listener is not None, "the run did not listen to the reply"
        for received in (0, 5, 12):
            listener(received)
        return ["譯文" for _ in batch]


class _Recorder:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def publish(self, event: ProgressEvent) -> None:
        self.events.append(event)


def _counted_by_units(project: Project) -> Project:
    translator = project.config.translator.model_copy(
        update={"batch_size": 10, "max_request_tokens": 0}
    )
    project.save_config(project.config.model_copy(update={"translator": translator}))
    return Project.load(project.root)


def test_a_run_shows_what_the_reply_in_flight_has_received(new_project) -> None:
    project = new_project("Streaming")
    project.save_string_file(
        StringFile(source_file="script.txt", units=[TextUnit(id="u1", source="おはよう")]),
        "script.txt.json",
    )
    project = _counted_by_units(project)
    translator = _Streaming()
    sink = _Recorder()

    translate_project(
        project,
        lanes=(TranslationLane(translator),),
        sink=sink,
        clock=itertools.count().__next__,
    )

    streaming = [event for event in sink.events if event.transient]
    assert streaming, [event.message_key for event in sink.events]
    assert streaming[-1].message_key == "translate.streaming"
    assert streaming[-1].metrics["streamed_chars"] == 12
    completed = next(
        event for event in sink.events if event.message_key == "translate.batch.completed"
    )
    assert not completed.transient
    assert completed.metrics.get("streamed_chars", 0) == 0
    assert translator._stream_listener is None
