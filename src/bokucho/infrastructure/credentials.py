"""Portable Base64 credential storage."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from threading import RLock

from bokucho.appdirs import portable_root
from bokucho.application.contracts import CredentialStore
from bokucho.application.errors import ValidationFailure
from bokucho.sync.atomic import atomic_write_text

_FILE_LOCK = RLock()


def credential_account(provider: str, endpoint: str | None) -> str:
    """Return the stable account name for a provider and endpoint."""
    normalized = (endpoint or "default").rstrip("/").lower()
    return f"{provider.lower()}|{normalized}"


def interface_credential_scope(interface_id: str) -> str:
    """Return the isolated credential scope for one configured interface."""
    return f"interface:{interface_id}"


def shared_provider_credentials(
    store: CredentialStore,
    provider: str,
    endpoint: str | None,
    interface_id: str | None = None,
) -> tuple[str, ...]:
    """Read provider/endpoint keys, migrating the former interface scope once."""
    shared = store.get_all(provider, endpoint)
    if not interface_id or interface_id == "legacy":
        return shared
    old_scope = interface_credential_scope(interface_id)
    isolated = store.get_all(provider, old_scope)
    if not isolated:
        return shared
    for secret in isolated:
        store.add(provider, endpoint, secret)
    store.delete(provider, old_scope)
    return tuple(dict.fromkeys((*shared, *isolated)))


def default_credentials_path() -> Path:
    override = os.environ.get("BOKUCHO_CREDENTIALS_FILE")
    if override:
        return Path(override).expanduser().resolve(strict=False)
    return portable_root() / "Resource" / "credentials.json"


class Base64CredentialStore:
    """Portable JSON store with each credential represented as Base64 text."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_credentials_path()

    def get_all(self, provider: str, endpoint: str | None) -> tuple[str, ...]:
        account = credential_account(provider, endpoint)
        with _FILE_LOCK:
            return self._decode_keys(self._load()["accounts"].get(account, []))

    def add(self, provider: str, endpoint: str | None, secret: str) -> None:
        account = credential_account(provider, endpoint)
        with _FILE_LOCK:
            keys = self.get_all(provider, endpoint)
            if secret in keys:
                return
            payload = self._load()
            payload["accounts"][account] = [self._encode(key) for key in (*keys, secret)]
            self._save(payload)

    def delete(self, provider: str, endpoint: str | None) -> None:
        account = credential_account(provider, endpoint)
        with _FILE_LOCK:
            payload = self._load()
            if account in payload["accounts"]:
                del payload["accounts"][account]
                self._save(payload)

    def _load(self) -> dict[str, object]:
        if not self.path.is_file():
            return {"version": 1, "accounts": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationFailure(f"Credential file is unreadable: {self.path}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("accounts"), dict)
        ):
            raise ValidationFailure(f"Credential file has an unsupported format: {self.path}")
        return payload

    def _save(self, payload: dict[str, object]) -> None:
        atomic_write_text(
            self.path,
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        )

    @staticmethod
    def _encode(secret: str) -> str:
        return base64.b64encode(secret.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode_keys(values: object) -> tuple[str, ...]:
        if not isinstance(values, list):
            return ()
        try:
            keys = (
                base64.b64decode(value, validate=True).decode("utf-8")
                for value in values
                if isinstance(value, str)
            )
            return tuple(key for key in keys if key)
        except (ValueError, UnicodeDecodeError):
            return ()


def default_credential_store() -> Base64CredentialStore:
    """Create the portable store."""
    return Base64CredentialStore()
