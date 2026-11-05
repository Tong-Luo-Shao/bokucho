"""Provider replies the way the SDKs stream them, for tests standing in for a provider."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from types import SimpleNamespace
from typing import Any

Reply = str | BaseException | Iterable[Any]


def _pieces(text: str, size: int) -> list[str]:
    return [text[start : start + size] for start in range(0, len(text), size)] or [""]


def openai_chunks(
    text: str,
    *,
    finish_reason: str | None = "stop",
    usage: object | None = None,
    size: int = 4,
) -> list[SimpleNamespace]:
    """The chunks of one chat-completions reply; usage rides on a last, choiceless one."""
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=piece), finish_reason=None)],
            usage=None,
        )
        for piece in _pieces(text, size)
    ]
    chunks[-1].choices[0].finish_reason = finish_reason
    if usage is not None:
        chunks.append(SimpleNamespace(choices=[], usage=usage))
    return chunks


class OpenAIStreamClient:
    """``chat.completions.create`` answering with a streamed reply."""

    def __init__(self, reply: Reply | Callable[..., Reply]) -> None:
        self._reply = reply
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Iterator[Any]:
        self.requests.append(kwargs)
        reply = self._reply(**kwargs) if callable(self._reply) else self._reply
        if isinstance(reply, BaseException):
            raise reply
        return iter(openai_chunks(reply) if isinstance(reply, str) else reply)


class ClaudeStream:
    """What ``messages.stream`` opens: the text as it arrives, then the whole message."""

    def __init__(
        self,
        text: str,
        *,
        stop_reason: str = "end_turn",
        usage: object | None = None,
        size: int = 4,
    ) -> None:
        self._text = text
        self._stop_reason = stop_reason
        self._usage = usage
        self._size = size

    def __enter__(self) -> ClaudeStream:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    @property
    def text_stream(self) -> Iterator[str]:
        yield from _pieces(self._text, self._size)

    def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)],
            stop_reason=self._stop_reason,
            usage=self._usage,
        )


class ClaudeStreamClient:
    """``messages.stream`` answering with one reply, or raising an error."""

    def __init__(
        self,
        reply: str | ClaudeStream | BaseException | Callable[..., Any],
    ) -> None:
        self._reply = reply
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs: Any) -> ClaudeStream:
        self.requests.append(kwargs)
        reply = self._reply(**kwargs) if callable(self._reply) else self._reply
        if isinstance(reply, BaseException):
            raise reply
        return ClaudeStream(reply) if isinstance(reply, str) else reply
