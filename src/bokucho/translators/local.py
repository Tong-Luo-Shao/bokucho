"""OpenAI-compatible translation provider (OpenAI, Gemini, Ollama, LM Studio...)."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from bokucho.application.errors import (
    ProviderFailure,
    ProviderRateLimited,
    ProviderRefused,
)
from bokucho.translators.base import (
    ProviderTranslator,
    ReplyStream,
    TokenUsage,
    failure_detail,
    log_prompt,
    retry_after_seconds,
)

try:
    import openai

    _IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    _IMPORT_ERROR = exc

_log = logging.getLogger(__name__)
_MISSING_DEP_MSG = (
    "The local/OpenAI-compatible translator requires the openai SDK. Install it with:\n"
    "    pip install 'bokucho[local]'"
)


class LocalTranslator(ProviderTranslator):
    """Translator for any OpenAI-compatible chat-completions endpoint."""

    name = "local"
    label = "OpenAI-compatible"

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        api_key: str | None = None,
        request_options: Mapping[str, object] | None = None,
    ) -> None:
        if _IMPORT_ERROR is not None:
            raise ImportError(_MISSING_DEP_MSG) from _IMPORT_ERROR
        super().__init__()
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.api_key = api_key
        self.request_options = dict(request_options or {})
        self._client: openai.OpenAI | None = None

    @property
    def client(self) -> openai.OpenAI:
        if self._client is None:
            key = self._secret() or "unused"
            self._client = openai.OpenAI(base_url=self.base_url, api_key=key)
        return self._client

    def complete(self, system: str, user: str, **options: object) -> str:
        """The answer to one prompt pair, unparsed."""
        try:
            log_prompt(_log, system, user)
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **{
                    "stream_options": {"include_usage": True},
                    **self.request_options,
                    **options,
                    "stream": True,
                },
            )
            reply = ReplyStream(_log, self._stream_listener)
            read = _read(stream, reply)
            text = reply.finish()
            if read.usage is not None:
                self._record_usage(_token_usage(read.usage))
            if not read.chose:
                raise ProviderFailure(
                    "OpenAI-compatible provider answered without a choice."
                ) from None
            if not read.wrote or str(read.finish_reason or "").startswith("content_filter"):
                raise ProviderRefused(
                    "OpenAI-compatible provider declined to answer "
                    f"({read.finish_reason})."
                ) from None
            return text
        except openai.RateLimitError as exc:
            raise ProviderRateLimited(
                "OpenAI-compatible provider is rate limited "
                f"({failure_detail(exc, self._secret())}).",
                retry_after=retry_after_seconds(exc),
            ) from None
        except (
            openai.OpenAIError,
            AttributeError,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ProviderFailure(
                "OpenAI-compatible provider failed "
                f"({failure_detail(exc, self._secret())})."
            ) from None

    def _sdk_error(self) -> type[Exception]:
        return openai.OpenAIError


@dataclass
class _StreamRead:
    usage: object = None
    chose: bool = False
    wrote: bool = False
    finish_reason: object = None


def _read(stream: Iterable, reply: ReplyStream) -> _StreamRead:
    """Feed the streamed text to ``reply``; what the stream said about itself on the way."""
    read = _StreamRead()
    try:
        for chunk in stream:
            read.usage = getattr(chunk, "usage", None) or read.usage
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            read.chose = True
            choice = choices[0]
            content = getattr(getattr(choice, "delta", None), "content", None)
            if content is not None:
                read.wrote = True
                reply.add(content)
            read.finish_reason = getattr(choice, "finish_reason", None) or read.finish_reason
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    return read


def _token_usage(usage) -> TokenUsage:
    details = getattr(usage, "prompt_tokens_details", None)
    return TokenUsage(
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        cached_input_tokens=getattr(details, "cached_tokens", 0) or 0,
        requests=1,
    )
