"""Claude translation provider — wraps the anthropic SDK (reference implementation)."""

from __future__ import annotations

import logging
from collections.abc import Mapping

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
    import anthropic

    _IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    _IMPORT_ERROR = exc

_log = logging.getLogger(__name__)
_MISSING_DEP_MSG = (
    "The Claude translator requires the anthropic SDK. Install it with:\n"
    "    pip install 'bokucho[claude]'"
)


class ClaudeTranslator(ProviderTranslator):
    """Translator backed by the Anthropic API. Bring your own ANTHROPIC_API_KEY."""

    name = "claude"
    label = "Claude"

    def __init__(
        self,
        model: str = "claude-opus-4-8",
        api_key: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        request_options: Mapping[str, object] | None = None,
    ) -> None:
        if _IMPORT_ERROR is not None:
            raise ImportError(_MISSING_DEP_MSG) from _IMPORT_ERROR
        super().__init__()
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.request_options = dict(request_options or {})
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            kwargs = {}
            key = self._secret()
            if key:
                kwargs["api_key"] = key
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def complete(self, system: str, user: str, **options: object) -> str:
        """The answer to one prompt pair, unparsed."""
        try:
            log_prompt(_log, system, user)
            request_options = {"max_tokens": 8192, **self.request_options, **options}
            with self.client.messages.stream(
                model=self.model,
                system=system,
                messages=[{"role": "user", "content": user}],
                **request_options,
            ) as stream:
                reply = ReplyStream(_log, self._stream_listener)
                for piece in stream.text_stream:
                    reply.add(piece)
                reply.finish()
                message = stream.get_final_message()
            usage = getattr(message, "usage", None)
            if usage is not None:
                cache_creation = usage.cache_creation_input_tokens or 0
                cache_read = usage.cache_read_input_tokens or 0
                self._record_usage(
                    TokenUsage(
                        input_tokens=(usage.input_tokens + cache_creation + cache_read),
                        output_tokens=usage.output_tokens,
                        cached_input_tokens=cache_creation + cache_read,
                        requests=1,
                    )
                )
            if getattr(message, "stop_reason", None) == "refusal":
                raise ProviderRefused("Claude provider declined to answer (refusal).")
            return "".join(
                block.text for block in message.content if block.type == "text"
            )
        except anthropic.RateLimitError as exc:
            raise ProviderRateLimited(
                f"Claude provider is rate limited ({failure_detail(exc, self._secret())}).",
                retry_after=retry_after_seconds(exc),
            ) from None
        except (
            anthropic.AnthropicError,
            AttributeError,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ProviderFailure(
                f"Claude provider failed ({failure_detail(exc, self._secret())})."
            ) from None

    def _sdk_error(self) -> type[Exception]:
        return anthropic.AnthropicError
