"""Secret-safe local crash diagnostics for GUI background jobs."""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from bokucho.appdirs import app_data_dir
from bokucho.sync.atomic import atomic_write_text


class SecretRedactor:
    def __init__(self, secrets: list[str] | tuple[str, ...] = ()) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    def register(self, secrets: Iterable[str]) -> None:
        self._secrets = tuple(
            dict.fromkeys((*self._secrets, *(secret for secret in secrets if secret)))
        )

    def redact(self, text: str) -> str:
        sanitized = text
        for secret in self._secrets:
            sanitized = sanitized.replace(secret, "[REDACTED]")
        lines: list[str] = []
        for line in sanitized.splitlines(keepends=True):
            lowered = line.lower()
            if any(
                marker in lowered
                for marker in ("authorization", "provider request", "request body")
            ):
                separator = ":" if ":" in line else "="
                prefix = line.split(separator, maxsplit=1)[0]
                line = f"{prefix}{separator} [REDACTED]\n"
            lines.append(line)
        return "".join(lines)


@dataclass(frozen=True, slots=True)
class CrashReport:
    diagnostic: str
    path: Path


class CrashLogger:
    def __init__(
        self,
        log_dir: str | Path | None = None,
        redactor: SecretRedactor | None = None,
        secret_source: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        if log_dir is None:
            log_dir = app_data_dir() / "logs"
        self.log_dir = Path(log_dir)
        self.redactor = redactor or SecretRedactor()
        self._secret_source = secret_source

    def prepare(self) -> None:
        if self._secret_source is None:
            return
        try:
            self.redactor.register(self._secret_source())
        except Exception:  # noqa: BLE001 - diagnostics must not block application work
            return

    def register(self, secrets: Iterable[str]) -> None:
        self.redactor.register(secrets)

    def capture(self, error: BaseException) -> CrashReport:
        self.prepare()
        raw = "".join(traceback.format_exception(error))
        diagnostic = self.redactor.redact(raw)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.log_dir / f"crash-{timestamp}-{uuid4().hex[:8]}.log"
        atomic_write_text(path, diagnostic)
        return CrashReport(diagnostic, path)
