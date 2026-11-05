"""Persistent request quota enforcement for translator interfaces."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

from bokucho.application.contracts import CancellationToken
from bokucho.application.errors import ValidationFailure
from bokucho.sync.atomic import atomic_write_text

_log = logging.getLogger(__name__)
_MINUTE = timedelta(minutes=1)
_POLL_SECONDS = 0.25
_LEDGER_LOCK = Lock()


class PersistentRequestLimiter:
    def __init__(
        self,
        path: str | Path,
        *,
        interface_id: str,
        rpm: int,
        tpm: int,
        rpd: int,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = Path(path)
        self.interface_id = interface_id
        self.rpm = rpm
        self.tpm = tpm
        self.rpd = rpd
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._warned = False

    def acquire(
        self,
        input_tokens: int,
        cancellation: CancellationToken,
    ) -> None:
        if self.tpm and input_tokens > self.tpm:
            raise ValidationFailure(
                "A single sentence exceeds the interface TPM limit; "
                "increase TPM because sentences cannot be split."
            )

        while True:
            cancellation.raise_if_cancelled()
            wait_seconds = self._try_record(input_tokens)
            if wait_seconds is None:
                return
            self._sleep(min(wait_seconds, _POLL_SECONDS))

    def _try_record(self, input_tokens: int) -> float | None:
        """Record the request if the quotas allow it now; otherwise how long to wait."""
        now = self._aware_now()
        with _LEDGER_LOCK:
            payload = self._load()
            entries = self._prune(self._entries(payload), now)
            self._warn_at_daily_limit(entries, now)
            minute_entries = [entry for entry in entries if entry["at"] > now - _MINUTE]
            request_blocked = self.rpm and len(minute_entries) >= self.rpm
            token_blocked = self.tpm and (
                sum(entry["tokens"] for entry in minute_entries) + input_tokens > self.tpm
            )
            if request_blocked or token_blocked:
                return self._wait_seconds(minute_entries, now, input_tokens)
            entries.append({"at": now, "tokens": input_tokens})
            self._save(payload, entries)
            return None

    def _warn_at_daily_limit(self, entries: list[dict], now: datetime) -> None:
        daily_requests = sum(entry["at"].date() == now.date() for entry in entries)
        if self.rpd and daily_requests == self.rpd and not self._warned:
            self._warned = True
            _log.warning(
                "Interface %r has made %d request(s) today, its "
                "configured daily limit. Carrying on: the provider "
                "refuses when it is actually out.",
                self.interface_id,
                daily_requests,
            )

    def _wait_seconds(
        self,
        entries: list[dict],
        now: datetime,
        input_tokens: int,
    ) -> float:
        expirations: list[datetime] = []
        if self.rpm and len(entries) >= self.rpm:
            expirations.append(entries[-self.rpm]["at"] + _MINUTE)
        if self.tpm:
            total = sum(entry["tokens"] for entry in entries)
            for entry in entries:
                if total + input_tokens <= self.tpm:
                    break
                total -= entry["tokens"]
                expirations.append(entry["at"] + _MINUTE)
        if not expirations:
            return _POLL_SECONDS
        return max((max(expirations) - now).total_seconds(), 0.001)

    def _load(self) -> dict:
        if not self.path.is_file():
            return {"version": 1, "interfaces": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValidationFailure(
                f"Translation quota ledger is unreadable: {self.path}"
            ) from error
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("interfaces"), dict)
        ):
            raise ValidationFailure(
                f"Translation quota ledger has an unsupported format: {self.path}"
            )
        return payload

    def _entries(self, payload: dict) -> list[dict]:
        raw_entries = payload["interfaces"].get(self.interface_id, [])
        entries = []
        for item in raw_entries if isinstance(raw_entries, list) else []:
            try:
                at = datetime.fromisoformat(item["at"])
                tokens = int(item["tokens"])
            except (KeyError, TypeError, ValueError):
                continue
            entries.append({"at": at.astimezone(UTC), "tokens": max(tokens, 0)})
        return sorted(entries, key=lambda entry: entry["at"])

    @staticmethod
    def _prune(entries: list[dict], now: datetime) -> list[dict]:
        day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
        return [entry for entry in entries if entry["at"] >= day_start]

    def _save(self, payload: dict, entries: list[dict]) -> None:
        payload["interfaces"][self.interface_id] = [
            {"at": entry["at"].isoformat(), "tokens": entry["tokens"]}
            for entry in entries
        ]
        atomic_write_text(
            self.path,
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        )

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
