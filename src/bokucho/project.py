"""Project config + on-disk layout."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from string import Formatter
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

from bokucho.language import normalize_language_tag
from bokucho.model import StringFile
from bokucho.sync.json_io import load_strings_dir, save_string_file

PROJECT_FILE = "project.toml"
STRINGS_DIRNAME = "strings"
UNPACKED_DIRNAME = "unpacked"


def unpacked_game_dir(root: str | Path, archive: str | Path) -> Path:
    """Where ``archive`` lands once unpacked inside the project at ``root``."""
    return Path(root) / UNPACKED_DIRNAME / Path(archive).stem


DEFAULT_SYSTEM_PROMPT = """\
You are a professional game translator. Translate each entry from \
{Orig_Lang} to {Target_Lang}.

Rules:
- Preserve ALL control codes, tags, variables and line breaks exactly as written.
- Full-width brackets are punctuation, not markup: what is inside them is \
read on screen and is translated with the rest, glossary and all. \
A protected token is the exception: it is copied character for character, brackets and all.
- Match each speaker's tone and the register of the text; keep terminology \
consistent.
- Every entry must come back in {Target_Lang}. Returning an entry unchanged \
is not an answer: translate all {n} of them, including the ones you would \
rather not.
{glossary_section}\
Each entry's text is its lines as they sit on screen. Return ONLY a JSON \
array of {n} arrays, one per entry in the same order, each holding exactly \
as many strings as the entry it answers. No commentary."""

DEFAULT_USER_PROMPT = "{context_section}{entries_json}"

THINKING_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")

_SYSTEM_PROMPT_FIELDS = {
    "Orig_Lang", "Target_Lang", "source_lang", "target_lang", "glossary_section", "n",
}
_USER_PROMPT_FIELDS = {"context_section", "entries_json", "n"}
_FORBIDDEN_REQUEST_OPTIONS = {
    "api_key",
    "authorization",
    "extra_headers",
    "headers",
    "messages",
    "model",
    "system",
}


def _api_key_variable(value: str | None) -> str | None:
    if value and not re.fullmatch(r"[a-z][a-z0-9_]*_api_key", value, re.IGNORECASE):
        raise ValueError(
            f"api_key_env must name an API key variable such as OPENAI_API_KEY, not {value!r}"
        )
    return value


ApiKeyVariable = Annotated[str | None, AfterValidator(_api_key_variable)]


def _no_forbidden_request_key(value: dict[str, Any]) -> dict[str, Any]:
    forbidden = _find_forbidden_request_key(value)
    if forbidden is not None:
        raise ValueError(f"request_options cannot contain {forbidden}")
    return value


RequestOptions = Annotated[dict[str, Any], AfterValidator(_no_forbidden_request_key)]


class EngineConfig(BaseModel):
    type: str = "livemaker"
    game_path: str = ""
    proxy_map: str = "proxy_map.json"
    sdk_path: str = ""
    language: str = ""
    original_sha256: str | None = None
    uif_dll: str | None = None


class FontSourceConfig(BaseModel):
    """One face the proxied characters may be drawn from."""

    path: str
    variation: str = ""


class FontConfig(BaseModel):
    """Where the glyphs drawn into the game's own font come from."""

    sources: list[FontSourceConfig] = Field(default_factory=list)


class TranslatorInterfaceConfig(BaseModel):
    id: str
    name: str
    provider: str
    model: str
    batch_size: int = Field(default=20, ge=0)
    max_request_tokens: int = Field(default=10240, ge=0)
    context_units: int = Field(default=3, ge=0)
    concurrency: int = Field(default=1, ge=1, le=16)
    base_url: str | None = None
    api_key_env: ApiKeyVariable = None
    rpm: int = Field(default=0, ge=0)
    tpm: int = Field(default=0, ge=0)
    rpd: int = Field(default=0, ge=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    thinking_level: str | None = None
    request_options: RequestOptions = Field(default_factory=dict)

    @field_validator("id", "name", "provider", "model")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("translator interface fields cannot be empty")
        return normalized

    @field_validator("thinking_level")
    @classmethod
    def validate_thinking_level(cls, value: str | None) -> str | None:
        if value is None or not value.strip() or value == "default":
            return None
        normalized = value.strip().lower()
        if normalized not in THINKING_LEVELS:
            raise ValueError(
                "thinking_level must be one of: " + ", ".join(THINKING_LEVELS)
            )
        return normalized


class TranslatorConfig(BaseModel):
    provider: str = "claude"
    model: str = "claude-opus-4-8"
    batch_size: int = Field(default=20, ge=0)
    max_request_tokens: int = Field(default=10240, ge=0)
    context_units: int = Field(default=3, ge=0)
    base_url: str | None = None
    api_key_env: ApiKeyVariable = None
    request_options: RequestOptions = Field(default_factory=dict)
    active_interface_id: str | None = None
    interfaces: list[TranslatorInterfaceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_interfaces(self) -> TranslatorConfig:
        ids = [item.id for item in self.interfaces]
        if len(ids) != len(set(ids)):
            raise ValueError("translator interface IDs must be unique")
        if not self.interfaces:
            if self.active_interface_id is not None:
                raise ValueError("active_interface_id requires a configured interface")
            return self

        if self.active_interface_id is None:
            self.active_interface_id = self.interfaces[0].id
        selected = next(
            (item for item in self.interfaces if item.id == self.active_interface_id),
            None,
        )
        if selected is None:
            raise ValueError(f"Unknown active translator interface: {self.active_interface_id}")

        self.provider = selected.provider
        self.model = selected.model
        self.batch_size = selected.batch_size
        self.max_request_tokens = selected.max_request_tokens
        self.context_units = selected.context_units
        self.base_url = selected.base_url
        self.api_key_env = selected.api_key_env
        self.request_options = dict(selected.request_options)
        return self

    def selected_interface(self) -> TranslatorInterfaceConfig:
        if self.interfaces:
            return next(
                item for item in self.interfaces if item.id == self.active_interface_id
            )
        return TranslatorInterfaceConfig(
            id="legacy",
            name=f"{self.provider} default",
            provider=self.provider,
            model=self.model,
            batch_size=self.batch_size,
            max_request_tokens=self.max_request_tokens,
            context_units=self.context_units,
            base_url=self.base_url,
            api_key_env=self.api_key_env,
            request_options=self.request_options,
        )


class PromptConfig(BaseModel):
    system: str = DEFAULT_SYSTEM_PROMPT
    user: str = DEFAULT_USER_PROMPT
    preset_id: str = ""

    @field_validator("system")
    @classmethod
    def validate_system_prompt(cls, value: str) -> str:
        return _validate_prompt(value, _SYSTEM_PROMPT_FIELDS, "system")

    @field_validator("user")
    @classmethod
    def validate_user_prompt(cls, value: str) -> str:
        validated = _validate_prompt(value, _USER_PROMPT_FIELDS, "user")
        fields = {field for _, field, _, _ in Formatter().parse(validated) if field}
        if "entries_json" not in fields:
            raise ValueError("user prompt must contain {entries_json}")
        return validated


def _validate_prompt(value: str, allowed: set[str], label: str) -> str:
    if not value.strip():
        raise ValueError(f"{label} prompt cannot be empty")
    try:
        fields = {field for _, field, _, _ in Formatter().parse(value) if field}
    except ValueError as error:
        raise ValueError(f"invalid {label} prompt braces: {error}") from error
    unknown = fields - allowed
    if unknown:
        raise ValueError(f"unknown {label} prompt placeholder: {sorted(unknown)[0]}")
    return value


def _find_forbidden_request_key(value: object) -> str | None:
    if isinstance(value, dict):
        for key in value:
            if key.casefold() in _FORBIDDEN_REQUEST_OPTIONS:
                return key
        nested = value.values()
    elif isinstance(value, list):
        nested = value
    else:
        return None
    return next(
        (key for item in nested if (key := _find_forbidden_request_key(item)) is not None),
        None,
    )


class GlossaryConfig(BaseModel):
    path: str = "glossary.json"


class ImagesConfig(BaseModel):
    font: str | None = None
    variation: str = ""


class SyncConfig(BaseModel):
    provider: str | None = None
    project_id: int | None = None


class ProjectConfig(BaseModel):
    """Typed view of ``project.toml``."""

    name: str
    source_lang: str
    target_lang: str
    engine: EngineConfig = Field(default_factory=EngineConfig)
    font: FontConfig = Field(default_factory=FontConfig)
    translator: TranslatorConfig = Field(default_factory=TranslatorConfig)
    prompts: PromptConfig = Field(default_factory=PromptConfig)
    glossary: GlossaryConfig = Field(default_factory=GlossaryConfig)
    images: ImagesConfig = Field(default_factory=ImagesConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    transforms: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def preserve_legacy_renpy_language(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not isinstance(data.get("target_lang"), str):
            return data
        engine = data.get("engine", {})
        if isinstance(engine, EngineConfig):
            engine = engine.model_dump()
        if not isinstance(engine, dict) or engine.get("type") != "renpy" or engine.get("language"):
            return data
        from bokucho.engines.renpy import default_language

        previous = default_language(data["target_lang"])
        normalized = default_language(normalize_language_tag(data["target_lang"]))
        if previous != normalized:
            return {**data, "engine": {**engine, "language": previous}}
        return data

    @field_validator("source_lang", "target_lang")
    @classmethod
    def language_region_tags(cls, value: str) -> str:
        return normalize_language_tag(value)

    @classmethod
    def from_toml_dict(cls, data: dict) -> ProjectConfig:
        """Build from a parsed ``project.toml`` mapping."""
        project = dict(data.get("project", {}))
        return cls(
            **project,
            engine=data.get("engine", {}),
            font={"sources": data.get("font", {}).get("source", [])},
            translator=data.get("translator", {}),
            prompts=data.get("prompts", {}),
            glossary=data.get("glossary", {}),
            images=data.get("images", {}),
            sync=data.get("sync", {}),
            transforms=data.get("transform", []),
        )


class Project:
    """A loaded Bokucho project rooted at ``root``."""

    def __init__(self, root: str | Path, config: ProjectConfig) -> None:
        self.root = Path(root)
        self.config = config

    @property
    def project_file(self) -> Path:
        return self.root / PROJECT_FILE

    @property
    def strings_dir(self) -> Path:
        return self.root / STRINGS_DIRNAME

    @property
    def glossary_path(self) -> Path:
        return self.root / self.config.glossary.path

    @classmethod
    def load(cls, root: str | Path) -> Project:
        """Load a project from ``root`` (which must contain ``project.toml``)."""
        root = Path(root)
        project_file = root / PROJECT_FILE
        if not project_file.is_file():
            raise FileNotFoundError(f"No {PROJECT_FILE} found in {root}")
        data = tomllib.loads(project_file.read_text(encoding="utf-8"))
        return cls(root, ProjectConfig.from_toml_dict(data))

    def load_string_files(self) -> list[StringFile]:
        """Load every neutral-format file under ``strings/``."""
        if not self.strings_dir.is_dir():
            return []
        return load_strings_dir(self.strings_dir)

    def save_string_file(self, string_file: StringFile, filename: str) -> Path:
        """Save a :class:`StringFile` under ``strings/<filename>``."""
        self.strings_dir.mkdir(parents=True, exist_ok=True)
        path = self.strings_dir / filename
        save_string_file(string_file, path)
        return path

    def save_config(self, config: ProjectConfig) -> ProjectConfig:
        """Persist ``config`` and return the configuration read back from disk."""
        from bokucho.sync.toml_io import save_project_config

        save_project_config(self.project_file, ProjectConfig.model_validate(config.model_dump()))
        return Project.load(self.root).config
