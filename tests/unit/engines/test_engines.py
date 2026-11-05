"""Tests for the engine registry lookup and the adapter factory."""

import pytest

from bokucho.engines import get_adapter
from bokucho.infrastructure import factories


def test_get_adapter_unknown_raises():
    with pytest.raises(ValueError, match="Unknown engine adapter"):
        get_adapter("nope")


def test_engine_factory_passes_livemaker_proxy_map(new_project, monkeypatch):
    project = new_project("t")
    adapter = object()
    captured = {}

    def get_fake_adapter(name, **kwargs):
        captured.update(name=name, **kwargs)
        return adapter

    monkeypatch.setattr(factories, "get_adapter", get_fake_adapter)

    assert factories.make_engine_adapter(project) is adapter
    assert captured == {
        "name": "livemaker",
        "proxy_map": project.root / project.config.engine.proxy_map,
        "workspace": project.root / "unpacked",
    }
