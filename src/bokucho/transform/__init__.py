"""Post-processing transforms — the third extension point."""

from __future__ import annotations

from bokucho.transform.base import Transform

_REGISTRY = {
    "opencc": "bokucho.transform.opencc:OpenCCTransform",
    "punctuation": "bokucho.transform.punctuation:PunctuationTransform",
}


def available_transforms() -> list[str]:
    """Names of all registered transforms."""
    return sorted(_REGISTRY)


def get_transform(name: str, **kwargs) -> Transform:
    """Instantiate the transform registered under ``name`` (kwargs -> constructor)."""
    try:
        target = _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"Unknown transform: {name!r}. Available: {', '.join(available_transforms())}"
        ) from None
    module_path, _, class_name = target.partition(":")
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, class_name)(**kwargs)


__all__ = ["Transform", "available_transforms", "get_transform"]
