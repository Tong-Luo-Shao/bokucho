"""Comment-preserving project configuration writes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomlkit

from bokucho.sync.atomic import atomic_write_text

if TYPE_CHECKING:
    from bokucho.project import ProjectConfig


def _sync_table(
    document: Any,
    section_name: str,
    values: dict[str, Any],
    modeled_keys: set[str],
) -> None:
    if section_name not in document:
        document[section_name] = tomlkit.table()
    section = document[section_name]
    for key in modeled_keys:
        if key not in values:
            section.pop(key, None)
    for key, value in values.items():
        section[key] = value


def save_project_config(path: str | Path, config: ProjectConfig) -> None:
    """Update a project TOML document while retaining its comments."""
    destination = Path(path)
    document = tomlkit.parse(destination.read_text(encoding="utf-8"))
    _sync_table(
        document,
        "project",
        {
            "name": config.name,
            "source_lang": config.source_lang,
            "target_lang": config.target_lang,
        },
        {"name", "source_lang", "target_lang"},
    )
    for section_name in ("engine", "translator", "prompts", "glossary", "images", "sync"):
        section_config = getattr(config, section_name)
        values = section_config.model_dump(exclude_none=True)
        _sync_table(document, section_name, values, set(type(section_config).model_fields))

    sources = tomlkit.aot()
    for source in config.font.sources:
        table = tomlkit.table()
        table.update(source.model_dump())
        sources.append(table)
    font = tomlkit.table()
    font["source"] = sources
    document["font"] = font

    transforms = tomlkit.aot()
    for spec in config.transforms:
        table = tomlkit.table()
        table.update(spec)
        transforms.append(table)
    document["transform"] = transforms
    atomic_write_text(destination, tomlkit.dumps(document))
