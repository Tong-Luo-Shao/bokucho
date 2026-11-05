"""Engine adapters — plugin seam #1."""

from __future__ import annotations

from bokucho.engines.base import EngineAdapter

_REGISTRY = {
    "livemaker": "bokucho.engines.livemaker:LiveMakerAdapter",
    "renpy": "bokucho.engines.renpy:RenpyAdapter",
    "rpgmaker": "bokucho.engines.rpgmaker:RpgMakerAdapter",
    "rpgmaker-mv": "bokucho.engines.rpgmaker:RpgMakerAdapter",
    "rpgmaker-mz": "bokucho.engines.rpgmaker:RpgMakerAdapter",
    "rpgmaker-xp": "bokucho.engines.rpgmaker:RpgMakerAdapter",
    "rpgmaker-vx": "bokucho.engines.rpgmaker:RpgMakerAdapter",
    "rpgmaker-vx-ace": "bokucho.engines.rpgmaker:RpgMakerAdapter",
}

RPGMAKER_ENGINES = frozenset({
    "rpgmaker", "rpgmaker-mv", "rpgmaker-mz", "rpgmaker-xp", "rpgmaker-vx",
    "rpgmaker-vx-ace",
})


def available_engines() -> list[str]:
    """Names of all registered engine adapters."""
    return sorted(name for name in _REGISTRY if name != "rpgmaker")


def is_rpgmaker(name: str) -> bool:
    """Whether ``name`` selects any current or legacy RPG Maker adapter."""
    return name in RPGMAKER_ENGINES


def engine_family(name: str) -> str:
    """Shared behavior family for an engine's concrete project type."""
    return "rpgmaker" if is_rpgmaker(name) else name


def adapter_class(name: str) -> type[EngineAdapter]:
    """The adapter class registered under ``name``, without building one."""
    try:
        target = _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"Unknown engine adapter: {name!r}. Available: {', '.join(available_engines())}"
        ) from None
    module_path, _, class_name = target.partition(":")
    import importlib

    return getattr(importlib.import_module(module_path), class_name)


def get_adapter(name: str, **kwargs) -> EngineAdapter:
    """Instantiate the engine adapter registered under ``name`` (kwargs -> constructor)."""
    return adapter_class(name)(**kwargs)


__all__ = [
    "EngineAdapter", "RPGMAKER_ENGINES", "adapter_class", "available_engines",
    "engine_family", "get_adapter", "is_rpgmaker",
]
