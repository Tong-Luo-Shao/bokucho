"""Qt ``.ts`` translation catalogs, served through the bridge."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from functools import cache
from pathlib import Path

_TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / "resources" / "translations"


@cache
def load_catalog(locale: str) -> dict[str, dict[str, str]]:
    """Parse ``bokucho_<locale>.ts`` into ``{context: {source: translation}}``."""
    path = _TRANSLATIONS_DIR / f"bokucho_{locale}.ts"
    if not path.is_file():
        raise KeyError(f"Unknown locale: {locale}")
    root = ET.parse(path).getroot()
    catalog: dict[str, dict[str, str]] = {}
    for context in root.findall("context"):
        messages = catalog.setdefault(context.findtext("name", default=""), {})
        for message in context.findall("message"):
            source = message.findtext("source", default="")
            messages[source] = message.findtext("translation", default="")
    return catalog
