"""Named base prompts, shared by every project this user works on."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from bokucho.project import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_PROMPT,
    PromptConfig,
)
from bokucho.sync.atomic import atomic_write_text
from bokucho.translate.glossary import global_dir

_VISUAL_NOVEL_SYSTEM_PROMPT = """\
You are a professional visual-novel translator. Translate each entry from \
{Orig_Lang} to {Target_Lang}.

Rules:
- Preserve ALL control codes, tags and variables exactly as written: \
newlines, %name%-style variables, ruby tags, backslash escapes.
- Full-width brackets are punctuation, not markup. 【アオイ】 is a name the \
player reads on screen, so it is translated like everything else, glossary \
and all. Only the codes named above stay as written. \
A protected token is the exception: it is copied character for character, brackets and all.
- Match the speaker's tone; keep terminology consistent.
- Every entry must come back in {Target_Lang}. Returning an entry unchanged \
is not an answer: translate all {n} of them, including the ones you would \
rather not.
{glossary_section}\
Each entry's text is its lines as they sit on screen. Return ONLY a JSON \
array of {n} arrays, one per entry in the same order, each holding exactly \
as many strings as the entry it answers. No commentary."""

_UI_SYSTEM_PROMPT = """\
You are localizing short interface strings from {Orig_Lang} to {Target_Lang}.

Rules:
- Preserve ALL control codes, tags and variables exactly as written.
- Full-width brackets are punctuation, not markup: what is inside them is \
read on screen and is translated with the rest. \
A protected token is the exception: it is copied character for character, brackets and all.
- Keep each translation as short as the source; these are buttons, menu items \
and labels with no room to grow.
- Do not add punctuation the source does not have.
- Every entry must come back in {Target_Lang}: returning one unchanged is not an answer.
{glossary_section}\
Each entry's text is its lines as they sit on screen. Return ONLY a JSON \
array of {n} arrays, one per entry in the same order, each holding exactly \
as many strings as the entry it answers. No commentary."""

_LITERAL_SYSTEM_PROMPT = """\
Translate each entry from {Orig_Lang} to {Target_Lang}.

Rules:
- Preserve ALL control codes, tags and variables exactly as written.
- Full-width brackets are punctuation, not markup: what is inside them is \
read on screen and is translated with the rest. \
A protected token is the exception: it is copied character for character, brackets and all.
- Translate what the source says, without smoothing, expanding or explaining.
- Keep terminology consistent.
- Every entry must come back in {Target_Lang}: returning one unchanged is not an answer.
{glossary_section}\
Each entry's text is its lines as they sit on screen. Return ONLY a JSON \
array of {n} arrays, one per entry in the same order, each holding exactly \
as many strings as the entry it answers. No commentary."""


class PromptPreset(BaseModel):
    """One named (system, user) pair."""

    id: str
    name: str
    system: str
    user: str
    builtin: bool = False

    @field_validator("id", "name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("prompt preset id and name cannot be empty")
        return normalized

    def as_prompt_config(self) -> PromptConfig:
        """Validate the pair the same way a project's own prompts are."""
        return PromptConfig(system=self.system, user=self.user)


BUILTIN_PRESETS: tuple[PromptPreset, ...] = (
    PromptPreset(
        id="builtin:general",
        name="General",
        system=DEFAULT_SYSTEM_PROMPT,
        user=DEFAULT_USER_PROMPT,
        builtin=True,
    ),
    PromptPreset(
        id="builtin:visual-novel",
        name="Visual novel",
        system=_VISUAL_NOVEL_SYSTEM_PROMPT,
        user=DEFAULT_USER_PROMPT,
        builtin=True,
    ),
    PromptPreset(
        id="builtin:literal",
        name="Literal",
        system=_LITERAL_SYSTEM_PROMPT,
        user=DEFAULT_USER_PROMPT,
        builtin=True,
    ),
    PromptPreset(
        id="builtin:ui-strings",
        name="Interface strings",
        system=_UI_SYSTEM_PROMPT,
        user=DEFAULT_USER_PROMPT,
        builtin=True,
    ),
)


class PromptLibrary(BaseModel):
    """The user's own presets; built-ins are added when the library is read."""

    presets: list[PromptPreset] = Field(default_factory=list)


def library_path() -> Path:
    return global_dir() / "prompts.json"


def _store(destination: Path, presets: list[PromptPreset]) -> list[PromptPreset]:
    atomic_write_text(destination, PromptLibrary(presets=presets).model_dump_json(indent=2) + "\n")
    return load_library(destination)


def _is_builtin(preset_id: str) -> bool:
    return any(builtin.id == preset_id for builtin in BUILTIN_PRESETS)


def load_library(path: Path | None = None) -> list[PromptPreset]:
    """Built-ins first, then whatever the user saved."""
    source = path or library_path()
    if not source.is_file():
        return list(BUILTIN_PRESETS)
    try:
        saved = PromptLibrary.model_validate_json(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return list(BUILTIN_PRESETS)
    user_presets = [preset for preset in saved.presets if not preset.builtin]
    return [*BUILTIN_PRESETS, *user_presets]


def find_preset(preset_id: str, path: Path | None = None) -> PromptPreset | None:
    return next(
        (preset for preset in load_library(path) if preset.id == preset_id),
        None,
    )


def save_preset(preset: PromptPreset, path: Path | None = None) -> list[PromptPreset]:
    """Add or replace a user preset; refuses to touch a built-in."""
    if _is_builtin(preset.id):
        raise ValueError(f"Built-in prompt presets cannot be changed: {preset.id}")
    preset.as_prompt_config()
    destination = path or library_path()
    stored = [
        item
        for item in load_library(destination)
        if not item.builtin and item.id != preset.id
    ]
    return _store(destination, [*stored, preset.model_copy(update={"builtin": False})])


def delete_preset(preset_id: str, path: Path | None = None) -> list[PromptPreset]:
    """Remove a user preset; refuses to remove a built-in."""
    if _is_builtin(preset_id):
        raise ValueError(f"Built-in prompt presets cannot be deleted: {preset_id}")
    destination = path or library_path()
    stored = [
        item
        for item in load_library(destination)
        if not item.builtin and item.id != preset_id
    ]
    return _store(destination, stored)
