"""RED-first structural tests for the buildless frontend shell (batch 5)."""

from __future__ import annotations

import importlib.util
import os
import re
import sys

from bokucho.webapp.__main__ import INDEX_HTML

WEB_ROOT = INDEX_HTML.parent
ASSETS = WEB_ROOT / "assets"
TOOLS = WEB_ROOT.parents[2] / "tools"

_SCRIPT_TAG_RE = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
_SRC_RE = re.compile(r'src="([^"]+)"')


def _web_files() -> list:
    return [path for path in WEB_ROOT.rglob("*") if path.is_file()]


def test_index_html_scripts_are_modules_served_from_assets() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    tags = _SCRIPT_TAG_RE.findall(html)
    assert tags, "index.html has no <script> tags"
    for attrs in tags:
        assert 'type="module"' in attrs, f"non-module script tag: <script{attrs}>"
        match = _SRC_RE.search(attrs)
        assert match is not None, f"script tag has no src: <script{attrs}>"
        assert match.group(1).startswith("assets/"), match.group(1)


def test_no_external_cdn_url_appears_under_web() -> None:
    offenders = [path for path in _web_files() if "https://" in path.read_text(encoding="utf-8")]
    assert not offenders, f"external URL(s) found in: {offenders}"


def test_theme_defines_light_and_dark_palette_tokens() -> None:
    """Both palettes exist and define the same names."""
    theme = (ASSETS / "theme.css").read_text(encoding="utf-8").lower()
    dark = theme.split('html[data-theme="light"]')[0]
    light = theme.split('html[data-theme="light"]')[1]
    for token in ("--window", "--surface", "--text", "--border", "--accent"):
        assert f"{token}:" in dark, f"dark {token} missing"
        assert f"{token}:" in light, f"light {token} missing"


def test_pywebview_reference_confined_to_bridge() -> None:
    offenders = [
        path
        for path in WEB_ROOT.rglob("*.js")
        if path.name != "bridge.js"
        and "window.pywebview" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"pywebview referenced outside bridge.js: {offenders}"


_IMPORT_RE = re.compile(r'^import\b[^"\n]*"(\.[^"]+)"', re.MULTILINE)


def test_every_relative_import_resolves_to_an_existing_file() -> None:
    for path in WEB_ROOT.rglob("*.js"):
        for specifier in _IMPORT_RE.findall(path.read_text(encoding="utf-8")):
            target = (path.parent / specifier).resolve()
            assert target.is_file(), f"{path.name}: import {specifier!r} -> {target} missing"


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(f"bokucho_tool_{name}", TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_interface_font_is_bundled_where_theme_css_fetches_it() -> None:
    fonts = _tool("fetch_typeset_fonts")
    build = _tool("build")
    theme = (ASSETS / "theme.css").read_text(encoding="utf-8")
    assert f'url("fonts/{fonts.INTERFACE_FONT.filename}")' in theme
    assert (
        f"--add-data={build.INTERFACE_FONT_DIR}{os.pathsep}bokucho/web/assets/fonts"
        in build.pyinstaller_arguments()
    )
