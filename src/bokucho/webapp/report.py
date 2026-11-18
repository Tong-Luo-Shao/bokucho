"""The link that opens a prefilled bug report on GitHub.

The translator reviews the form and submits it; nothing is sent from the app.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote, urlencode

ISSUE_FORM = "https://github.com/Tong-Luo-Shao/bokucho/issues/new"
#: A CJK character is nine bytes once quoted, so this keeps the whole link
#: under the 8,000 characters GitHub accepts.
MAX_ERROR_CHARS = 600
_TRUNCATED = "\n[truncated]"
# Only ASCII name characters continue a name: C:\Users\Ann must leave
# C:\Users\Anna and C:\Users\Ann.Lee alone, but a full stop may end the
# sentence, and a Chinese message may run straight on after a Chinese folder.
_NAME_STARTS = r"(?<![A-Za-z0-9_-])"
_NAME_ENDS = r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])"


def issue_url(
    *,
    version: str,
    system: str,
    engine: str,
    error: str,
    home: Path,
    project_roots: Iterable[Path] = (),
    game_path: Path | None = None,
    project_name: str = "",
) -> str:
    """A link to the ``bug.yml`` issue form with these facts already filled in.

    The projects' folders, the game's folder, the home directory and the
    project's name, which name the project, the game and the Windows user, are
    shown as placeholders.
    """
    fields = {"template": "bug.yml", "version": version, "system": system, "engine": engine}
    # A lone surrogate (possible in a Windows file name) cannot be quoted as UTF-8.
    error = error.encode("utf-8", "replace").decode("utf-8")
    hidden = [(root, "<project>") for root in project_roots]
    hidden += [(game_path, "<game>"), (home, "~")]
    # The longest first, so a project inside the home folder is named as the project.
    for path, mark in sorted(
        ((path, mark) for path, mark in hidden if path is not None),
        key=lambda item: len(str(item[0])),
        reverse=True,
    ):
        error = _hide(error, path, mark)
    if project_name.strip():
        pattern = re.escape(project_name.strip()) + _NAME_ENDS
        error = re.sub(_NAME_STARTS + pattern, "<project>", error, flags=re.IGNORECASE)
    if len(error) > MAX_ERROR_CHARS:
        error = error[:MAX_ERROR_CHARS] + _TRUNCATED
    if error:
        fields["error"] = error
    return f"{ISSUE_FORM}?{urlencode(fields, quote_via=quote)}"


def _hide(text: str, path: Path, mark: str) -> str:
    """``text`` with ``path`` shown as ``mark``, however its separators are spelled.

    Any run of ``\\`` and ``/`` counts as one separator, which also covers the
    doubled backslashes of a path an OSError shows through repr().
    """
    spelled = str(path)
    parts = [part for part in re.split(r"[\\/]+", spelled) if part]
    if not parts:
        return text
    separator = r"[\\/]+"
    pattern = separator.join(re.escape(part) for part in parts) + _NAME_ENDS
    if spelled[:1] in "\\/":
        pattern = separator + pattern
    return re.sub(pattern, lambda _: mark, text, flags=re.IGNORECASE)
