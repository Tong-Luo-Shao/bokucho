"""Unity engine adapter: reads assets and code, writes XUnity.AutoTranslator files.

The game is never changed. Extraction reads TextAsset and MonoBehaviour strings
with UnityPy and the C# string literals the game's code shows: from its own
assemblies in a Mono build, from ``global-metadata.dat`` in an IL2CPP build.
Inject writes the translation file XUnity.AutoTranslator looks up at run time.
The textures the assets hold unpack as PNG files for the image track, each named
after the texture XUnity replaces with its translated picture.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import struct
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressEvent,
    ProgressLevel,
    ProgressSink,
)
from bokucho.application.errors import InvalidProject, MissingDependency
from bokucho.engines.base import EngineAdapter, unpacked_from, unpacked_stamp
from bokucho.engines.unity_il2cpp import METADATA, metadata_string_literals
from bokucho.language import matches_source_language, normalize_language_tag
from bokucho.model import Context, Status, TextUnit

_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()

TRANSLATION_FILE = "Bokucho.txt"
CONFIG_FILE = "BepInEx/config/AutoTranslatorConfig.ini"

#: Object names and TextMeshPro font character sets: never text the player reads.
_SKIPPED_FIELDS = frozenset({"m_Name", "characterSequence"})
#: TextMeshPro's line-breaking rules: character lists, not text.
_SKIPPED_ASSETS = frozenset(
    {"LineBreaking Leading Characters", "LineBreaking Following Characters"}
)
#: Framework assemblies: their literals are not the game's.
_FRAMEWORK_PREFIXES = ("System", "Mono.", "mscorlib", "netstandard", "Unity.", "UnityEngine",
                       "Microsoft.")
_SCRIPTS = {
    "ja": re.compile(r"[぀-ヿ㐀-鿿]"),
    "zh": re.compile(r"[㐀-鿿]"),
    "ko": re.compile(r"[가-힯]"),
}
#: Control characters other than tab and newlines, and undecodable bytes.
_NOT_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\udc80-\udcff]")
#: A literal this long that is mostly other scripts is a character table, not a line.
_TABLE_LENGTH = 64
#: .NET's own literals that read as CJK text: calendar and era names, and the bounds of
#: Unicode block tables. A Mono build keeps them in framework assemblies, which are
#: never read; an IL2CPP build merges every assembly's literals into one table.
_FRAMEWORK_LITERALS = frozenset({
    "元", "年", "月", "日", "時", "时", "分", "秒", "明", "大", "昭", "平", "令",
    "明治", "大正", "昭和", "平成", "令和", "中華民國", "中華民國曆",
    "㌀㐀", "㐀䷀", "䷀一", "一ꀀ",
})
_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
#: ``key = value`` or ``key: value``.
_PROPERTY = re.compile(r"^\s*[\w.\-]+\s*[=:]\s*(.*)$")
#: The version a serialized file or bundle header names, e.g. 2019.4.29f1 or 6000.3.10f1.
_UNITY_VERSION = re.compile(rb"(?:[56]\d{3}|20\d\d|5)\.\d+\.\d+[abfpx]\d+")
#: Where a patch keeps its TMP fallback font, relative to the game folder.
FONT_DIR = "BepInEx/fonts"
#: Characters a Windows file name cannot hold.
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: The longest texture name an unpacked picture keeps, before its key.
_NAME_LENGTH = 80
#: C# format items and named placeholders: {0}, {0:N0}, {name}.
_PLACEHOLDER = re.compile(r"\{[^{}\s]+\}")
_REGEX_SPECIAL = frozenset(".*+?()[{}|$")
#: Characters a one-character class cannot hold without a backslash.
_UNRULABLE = frozenset('\\]^"')


class UnityLayout:
    """Where one Windows Unity build keeps its data and code."""

    def __init__(self, game: Path) -> None:
        # A project may name the game's executable instead of its folder.
        if game.is_file():
            game = game.parent
        self.root = game
        found = sorted(
            path for path in game.glob("*_Data")
            if (path / "globalgamemanagers").is_file() or (path / "data.unity3d").is_file()
        )
        if not found:
            raise InvalidProject(
                f"{game} is not a Unity game folder: no *_Data folder with globalgamemanagers "
                "or data.unity3d beside the executable."
            )
        self.data = found[0]
        self.il2cpp = (game / "GameAssembly.dll").is_file()

    def version(self) -> str | None:
        """The Unity version the data was built with, e.g. ``2019.4.29f1``."""
        for name in ("globalgamemanagers", "data.unity3d"):
            path = self.data / name
            if path.is_file():
                with path.open("rb") as stream:
                    match = _UNITY_VERSION.search(stream.read(256))
                if match:
                    return match[0].decode("ascii")
        return None

    @property
    def backend(self) -> str:
        """``il2cpp`` or ``mono``: how the game's C# code was built."""
        return "il2cpp" if self.il2cpp else "mono"

    def metadata(self) -> Path:
        """An IL2CPP build's ``global-metadata.dat``, where its string literals are kept."""
        return self.data.joinpath(*METADATA.split("/"))

    def uses_text_mesh_pro(self) -> bool:
        managed = self.data / "Managed"
        if managed.is_dir() and any(managed.glob("*TextMeshPro*.dll")):
            return True
        # An IL2CPP build has no Managed folder; Unity 2019+ lists the assemblies it compiled.
        listing = self.data / "ScriptingAssemblies.json"
        try:
            return "TextMeshPro" in listing.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

    def assemblies(self) -> list[Path]:
        """The game's own managed assemblies, framework ones left out."""
        managed = self.data / "Managed"
        return sorted(
            path for path in managed.glob("*.dll")
            if not path.name.startswith(_FRAMEWORK_PREFIXES)
        ) if managed.is_dir() else []


def architecture(binary: Path) -> str:
    """``x64`` or ``x86``: the machine a Windows PE file (UnityPlayer.dll) is built for."""
    with binary.open("rb") as stream:
        head = stream.read(4096)
    try:
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        machine = struct.unpack_from("<H", head, pe + 4)[0]
    except struct.error:
        machine = 0
    if machine not in (0x8664, 0x14C) or head[pe : pe + 4] != b"PE\0\0":
        raise InvalidProject(f"{binary.name} is not a Windows x64 or x86 program.")
    return "x64" if machine == 0x8664 else "x86"


def user_strings(assembly: bytes) -> list[str]:
    """The C# string literals of a .NET assembly: its ``#US`` metadata heap, in order."""
    try:
        heap = _user_string_heap(assembly)
    except (struct.error, ValueError, IndexError) as exc:
        raise ValueError(f"not a .NET assembly: {exc}") from exc
    found: list[str] = []
    position = 1  # entry 0 is the empty string
    while position < len(heap):
        first = heap[position]
        if (first & 0x80) == 0:
            length, position = first, position + 1
        elif (first & 0xC0) == 0x80:
            length, position = ((first & 0x3F) << 8) | heap[position + 1], position + 2
        else:
            length = int.from_bytes(heap[position : position + 4], "big") & 0x1FFFFFFF
            position += 4
        if length:
            # The last byte flags non-ASCII content; the string is the UTF-16 before it.
            found.append(heap[position : position + length - 1].decode("utf-16-le", "replace"))
        position += length
    return found


def _user_string_heap(image: bytes) -> bytes:
    pe = struct.unpack_from("<I", image, 0x3C)[0]
    if image[pe : pe + 4] != b"PE\0\0":
        raise ValueError("no PE signature")
    sections, optional_size = struct.unpack_from("<H12xH", image, pe + 6)
    optional = pe + 24
    magic = struct.unpack_from("<H", image, optional)[0]
    directories = optional + {0x10B: 96, 0x20B: 112}[magic]
    cli_rva = struct.unpack_from("<I", image, directories + 14 * 8)[0]
    table = optional + optional_size

    def offset(rva: int) -> int:
        for index in range(sections):
            size, address, raw_size, raw = struct.unpack_from("<4I", image, table + index * 40 + 8)
            if address <= rva < address + max(size, raw_size):
                return rva - address + raw
        raise ValueError(f"RVA {rva:#x} is in no section")

    if not cli_rva:
        raise ValueError("no CLI header")
    metadata = offset(struct.unpack_from("<I", image, offset(cli_rva) + 8)[0])
    if image[metadata : metadata + 4] != b"BSJB":
        raise ValueError("no metadata root")
    version_length = struct.unpack_from("<I", image, metadata + 12)[0]
    cursor = metadata + 16 + version_length
    streams = struct.unpack_from("<H", image, cursor + 2)[0]
    cursor += 4
    for _ in range(streams):
        start, size = struct.unpack_from("<II", image, cursor)
        end = image.index(b"\0", cursor + 8)
        name = image[cursor + 8 : end]
        cursor = (end + 4) & ~3
        if name == b"#US":
            return image[metadata + start : metadata + start + size]
    raise ValueError("no #US heap")


def wanted(text: str, source_lang: str) -> bool:
    """Whether ``text`` reads as player-facing text in the project's source language."""
    line = text.strip()
    if not line or _NOT_TEXT.search(text):
        return False
    # Unassigned code points only appear in tables of Unicode ranges.
    if any(unicodedata.category(character) == "Cn" for character in line):
        return False
    language = normalize_language_tag(source_lang).split("-")[0]
    script = _SCRIPTS.get(language)
    if script is not None:
        if len(line) >= _TABLE_LENGTH and len(script.findall(line)) * 10 < len(line):
            return False
        return bool(script.search(line)) and matches_source_language(line, source_lang)
    # shortcut: a Latin-script source keeps only text with a space, which drops one-word
    # labels such as "Start"; upgrade with a field-name allowlist when a game needs them.
    return " " in line and any(c.isalpha() for c in line) and matches_source_language(
        line, source_lang
    )


def readable(text: str) -> bool:
    """Whether ``text`` can be a line a player reads: a text file named in ``text_files``.

    Such files hold the game's text and nothing else, so one-word labels
    ("Continue") stay; only blanks, control characters and symbol rows go.
    """
    line = text.strip()
    return bool(line) and not _NOT_TEXT.search(text) and any(c.isalpha() for c in line) and not any(
        unicodedata.category(character) == "Cn" for character in line
    )


def text_asset_body(script: str | bytes) -> str | None:
    """A TextAsset's text, or ``None`` when it holds binary data."""
    raw = script if isinstance(script, bytes) else script.encode("utf-8", "surrogateescape")
    codec = "utf-8"
    for bom, name in _BOMS:
        if raw.startswith(bom):
            raw, codec = raw[len(bom) :], name
            break
    try:
        return raw.decode(codec)
    except UnicodeDecodeError:
        return None


def text_asset_strings(body: str) -> Iterator[str]:
    """The pieces of a TextAsset a game would show one at a time."""
    try:
        yield from _strings(json.loads(body))
        return
    except ValueError:
        pass
    values = property_values(body)
    if values is not None:
        yield from values
        return
    first = body.split("\n", 1)[0]
    # shortcut: a table is told by its first line, so prose with two commas in its first
    # line splits into cells; upgrade with a per-asset format setting if a game needs it.
    if "\t" in first or first.count(",") >= 2:
        reader = csv.reader(io.StringIO(body, newline=""), delimiter="\t" if "\t" in first else ",")
        try:
            rows = list(reader)
        except csv.Error:
            rows = [[line] for line in body.splitlines()]
        for row in rows:
            yield from row
        return
    yield from body.splitlines()


def property_values(body: str) -> list[str] | None:
    """The values of a ``key = value`` or ``key: value`` file as the game shows them.

    ``None`` if the body is not one. A value continues on the next line after a
    trailing backslash, and ``\\n`` in it is the line break the game draws. In a
    translation document a ``;`` comment right above a key holds that line's text
    in the other language (``; 又见面了呢。`` over ``message1: See you again.``),
    so such a comment is read as a value too.
    """
    values: list[str] = []
    pending: str | None = None
    note: str | None = None
    for line in body.splitlines():
        stripped = line.lstrip()
        if pending is not None:
            piece = stripped
        elif stripped.startswith(";"):
            note = stripped[1:].strip()
            continue
        elif not stripped or stripped.startswith(("#", "!")):
            note = None
            continue
        elif match := _PROPERTY.match(line):
            piece = match[1]
            if note:
                values.append(note)
            note = None
        else:
            return None
        if piece.endswith("\\") and not piece.endswith("\\\\"):
            pending = (pending or "") + piece[:-1]
            continue
        values.append(((pending or "") + piece).replace("\\n", "\n").replace("\\t", "\t"))
        pending = None
    return values or None


def _strings(value: object, key: str = "") -> Iterator[str]:
    if isinstance(value, str):
        if key not in _SKIPPED_FIELDS:
            yield value
    elif isinstance(value, dict):
        for name, item in value.items():
            yield from _strings(item, str(name))
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, key)


def unit_id(source: str) -> str:
    """One id per distinct source text, as XUnity looks translations up by text."""
    return "u_" + hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


def xunity_language(tag: str) -> str:
    """XUnity's language code for a Bokucho tag (zh-tw -> zh-TW)."""
    language, _, region = normalize_language_tag(tag).partition("-")
    return f"{language}-{region.upper()}" if region else language


def encode_entry(text: str) -> str:
    """``text`` as one side of an XUnity ``source=translation`` line."""
    out: list[str] = []
    for index, character in enumerate(text):
        if character == "\\":
            out.append("\\\\")
        elif character == "=":
            out.append("\\=")
        elif character == "\n":
            out.append("\\n")
        elif character == "\r":
            out.append("\\r")
        elif character == "/" and index and text[index - 1] == "/":
            out.append("\\u002F")  # an unescaped "//" starts a comment
        else:
            out.append(character)
    return "".join(out)


def template_rule(source: str, target: str) -> str | None:
    """An XUnity splitter-regex line for a source with ``{placeholders}``, else ``None``.

    The game shows the template filled in, which no plain line matches; the rule
    captures each filled-in value, which XUnity looks up on its own, and puts it
    where the translation has the same placeholder.
    """
    keys = _PLACEHOLDER.findall(source)
    literals = _PLACEHOLDER.split(source)
    # A template of little but placeholders would match nearly every line the game shows.
    if not keys or _UNRULABLE & set(source) or len("".join("".join(literals).split())) < 2:
        return None
    # Special characters become one-character classes, not backslash escapes: the rule
    # then means the same whether or not XUnity unescapes the line first.
    pattern = "^"
    first: dict[str, int] = {}
    for index, literal in enumerate(literals):
        pattern += "".join(f"[{c}]" if c in _REGEX_SPECIAL else c for c in literal)
        if index < len(keys):
            first.setdefault(keys[index], index + 1)
            pattern += "(.+?)"
    pattern += "$"
    replacement = _PLACEHOLDER.sub(
        lambda match: f"${{{first[match[0]]}}}" if match[0] in first else match[0],
        target.replace("$", "$$"),
    )
    return encode_entry('sr:"' + pattern + '"') + "=" + encode_entry(replacement)


def translation_file(pairs: dict[str, str]) -> str:
    """An XUnity translation file: one ``source=translation`` line, or rule, per pair."""
    # shortcut: a source starting with r:" sr:" or # reads as a regex or a directive;
    # none has been seen in game text, so such a line is written as is.
    lines = ["// Written by Bokucho; edit the project and rebuild instead of this file."]
    lines.extend(
        template_rule(source, target) or f"{encode_entry(source)}={encode_entry(target)}"
        for source, target in pairs.items()
    )
    # Games often show a label upper-cased ("New game" as NEW GAME), and XUnity matches
    # text exactly; the upper-case line is written unless another source already is it.
    lines.extend(
        f"{encode_entry(source.upper())}={encode_entry(target.upper())}"
        for source, target in pairs.items()
        if source.upper() != source and source.upper() not in pairs
        and not _PLACEHOLDER.search(source)
    )
    return "\n".join(lines) + "\n"


def xunity_hash(data: bytes) -> str:
    """XUnity's hash of ``data``: the first ten upper-case hex digits of its SHA-1."""
    return hashlib.sha1(data).hexdigest()[:10].upper()


def texture_file(name: str) -> str:
    """``<name> [KEY].png``: the file a texture unpacks to.

    KEY is XUnity's hash of the texture's name, the key it finds the texture by;
    the name before it is only made safe for a path.
    """
    safe = _UNSAFE_NAME.sub("_", name)[:_NAME_LENGTH].strip(" .") or "texture"
    return f"{safe} [{xunity_hash(name.encode('utf-8'))}].png"


def translated_texture_name(file_name: str, original: bytes, translated: bytes) -> str | None:
    """``<name> [KEY-HASH].png``, the name XUnity loads a translated texture by.

    ``file_name`` is what the texture unpacked to (``texture_file``) and HASH is
    XUnity's hash of that original picture: XUnity loads only a file whose own
    hash differs from it, so an unchanged picture has no name (``None``).
    """
    original_hash = xunity_hash(original)
    if xunity_hash(translated) == original_hash:
        return None
    return f"{file_name.removesuffix('].png')}-{original_hash}].png"


def data_fingerprint(data: Path) -> str:
    """A digest of the name, size and time of every file in ``data``; it changes with the game."""
    digest = hashlib.sha256()
    for path in sorted(item for item in data.rglob("*") if item.is_file()):
        stat = path.stat()
        relative = path.relative_to(data).as_posix()
        digest.update(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def font_bundle(generation: str) -> str:
    """The file name tools/build_unity_fonts.py gives a generation's fallback font."""
    return f"bokucho_tc_sdf_{generation}"


def font_generation(version: str | None) -> str | None:
    """The fallback font build (``u2019`` …) a game of this Unity version can load, if any."""
    year = int(version.split(".", 1)[0]) if version else 0
    # Each build loads in its own Unity year and the later ones up to the next build.
    for first, name in ((6000, "u6000"), (2022, "u2022"), (2021, "u2021"), (2019, "u2019"),
                        (2018, "u2018")):
        if year >= first:
            return name
    return None


def config_file(
    source_lang: str, target_lang: str, fallback_font: str | None = None, textures: bool = False
) -> str:
    """The AutoTranslatorConfig.ini that uses Bokucho's file and never a translation service."""
    config = (
        "[Service]\nEndpoint=\nFallbackEndpoint=\n\n"
        f"[General]\nLanguage={xunity_language(target_lang)}\n"
        f"FromLanguage={xunity_language(source_lang)}\n"
    )
    if fallback_font:
        # TextMesh Pro draws only the characters baked into the game's own atlases.
        config += f"\n[Behaviour]\nFallbackFontTextMeshPro={fallback_font}\n"
    if textures:
        # Translated pictures are named by the texture name they replace (texture_file).
        config += (
            "\n[Texture]\nEnableTextureTranslation=True\n"
            "TextureHashGenerationStrategy=FromImageName\n"
        )
    return config


def asset_group(kind: str, name: str, assets_file: Any) -> str:
    """The name a string is grouped under: its TextAsset, else the bundle or file it is in."""
    if kind == "text_asset":
        return f"TextAsset/{name}"
    # Files inside an AssetBundle are named CAB-<hash>; the bundle's own name says more.
    parent = getattr(assets_file, "parent", None)
    if type(parent).__name__ == "BundleFile" and getattr(parent, "name", ""):
        return str(parent.name)
    return str(assets_file.name)


def loose_text_files(game: Path, patterns: tuple[str, ...]) -> list[Path]:
    """The files beside the game that ``patterns`` (globs relative to it) name, in order."""
    root = game.resolve()
    found: dict[Path, None] = {}
    for pattern in patterns:
        parts = Path(pattern.replace("\\", "/")).parts
        if (
            not pattern.strip()
            or pattern.startswith(("/", "\\"))
            or ".." in parts
            or ":" in pattern
        ):
            raise InvalidProject(
                f"text_files entries are globs inside the game folder, not {pattern!r}."
            )
        for path in sorted(game.glob(pattern.replace("\\", "/"))):
            if path.is_file() and path.resolve().is_relative_to(root):
                found.setdefault(path, None)
    return list(found)


def code_literals(layout: UnityLayout) -> Iterator[tuple[str, Path, Any]]:
    """(engine_ref file, path, literals or the error reading them) for the game's code.

    A Mono build keeps its literals in each managed assembly; an IL2CPP build in
    one metadata file. Each source is read lazily so a large one is read once.
    """
    if layout.il2cpp:
        path = layout.metadata()
        try:
            yield METADATA, path, metadata_string_literals(path.read_bytes())
        except (OSError, ValueError) as exc:
            yield METADATA, path, exc
        return
    for path in layout.assemblies():
        try:
            yield f"Managed/{path.name}", path, user_strings(path.read_bytes())
        except (OSError, ValueError) as exc:
            yield f"Managed/{path.name}", path, exc


def load_type_trees(env: Any, layout: UnityLayout) -> None:
    """Let UnityPy read MonoBehaviours whose type trees the game stripped, if it can.

    The generator is optional: assets that carry their own type trees read
    without it, so any failure here only narrows what MonoBehaviours yield.
    """
    try:
        from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator

        generator = TypeTreeGenerator(env.objects[0].assets_file.unity_version)
        if layout.il2cpp:
            generator.load_il2cpp(
                (layout.root / "GameAssembly.dll").read_bytes(), layout.metadata().read_bytes()
            )
        else:
            managed = str(layout.data / "Managed")
            load = getattr(generator, "load_local_dll_folder", None) or generator.load_dll_folder
            load(managed)
    except Exception:  # noqa: BLE001 - an optional native helper; any failure leaves it off
        return
    env.typetree_generator = generator


class UnityAdapter(EngineAdapter):
    """Adapter for Windows Unity games (Mono and IL2CPP) through XUnity.AutoTranslator."""

    name = "unity"

    def __init__(
        self,
        source_lang: str = "ja",
        target_lang: str = "zh-tw",
        fallback_font: str | None = None,
        text_files: tuple[str, ...] = (),
        textures: bool = False,
    ) -> None:
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.fallback_font = fallback_font
        self.text_files = tuple(text_files)
        self.textures = textures

    @classmethod
    def game_root(cls, game_path: Path) -> Path | None:
        """``None``: a Unity game keeps its pictures inside its asset files."""
        return None

    @classmethod
    def packed_archive(cls, game_path: Path) -> Path:
        """The data folder whose assets hold the game's pictures."""
        return UnityLayout(Path(game_path)).data

    @classmethod
    def unpack_images(
        cls,
        game_path: Path,
        destination: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> None:
        """Write every named texture of the game to ``destination`` as ``texture_file``.

        Textures that share a name but not a picture are left out: XUnity finds a
        texture by its name, so it could not tell which of them a translation replaces.
        """
        layout = UnityLayout(Path(game_path))
        fingerprint = data_fingerprint(layout.data)
        if unpacked_from(destination, fingerprint):
            return
        textures = cls._textures(layout)
        staging = destination.with_name(f"{destination.name}.partial")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        #: Texture name -> digest of its picture; None once two different pictures share it.
        pictures: dict[str, str | None] = {}

        def skipped(index: int, detail: str) -> None:
            sink.publish(
                ProgressEvent(
                    "engine.unpack", index, len(textures), "engine.unpack.entry.skipped",
                    level=ProgressLevel.WARNING, detail=detail,
                )
            )

        for index, texture in enumerate(textures, start=1):
            token.raise_if_cancelled()
            try:
                data = texture.read()
                name = str(data.m_Name)
                picture = data.image if name else None
            except Exception as exc:  # noqa: BLE001 - UnityPy raises anything on a texture it cannot decode
                skipped(index, f"Texture2D {getattr(texture, 'path_id', '')}: {exc}")
                continue
            if picture is not None:
                digest = hashlib.sha256(
                    f"{picture.mode} {picture.size}".encode() + picture.tobytes()
                ).hexdigest()
                path = staging / texture_file(name)
                if name not in pictures:
                    pictures[name] = digest
                    picture.save(path)
                elif pictures[name] not in (digest, None):
                    pictures[name] = None
                    path.unlink()
                    skipped(index, f"{name}: another texture has this name")
            sink.publish(
                ProgressEvent(
                    "engine.unpack", index, len(textures), "engine.unpack.progress", detail=name
                )
            )
        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
        unpacked_stamp(destination).write_text(fingerprint, encoding="ascii")

    @staticmethod
    def _textures(layout: UnityLayout) -> list[Any]:
        """The game's Texture2D objects, not read yet."""
        try:
            import UnityPy
        except ImportError as exc:
            raise MissingDependency(
                'Unity pictures need UnityPy. Install it with: pip install "bokucho[unity]"'
            ) from exc
        return [
            obj for obj in UnityPy.load(str(layout.data)).objects if obj.type.name == "Texture2D"
        ]

    def extract(
        self,
        game_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> list[TextUnit]:
        layout = UnityLayout(Path(game_path))
        loose = loose_text_files(layout.root, self.text_files)
        total = (1 if layout.il2cpp else len(layout.assemblies())) + 1 + len(loose)
        units: dict[str, TextUnit] = {}

        def add(text: str, file: str, kind: str, asset: str = "") -> None:
            keep = readable(text) if kind == "text_file" else wanted(text, self.source_lang)
            if text in units or not keep:
                return
            ref = {"file": file, "kind": kind}
            if asset:
                ref["asset"] = asset
            units[text] = TextUnit(
                id=unit_id(text),
                source=text,
                status=Status.UNTRANSLATED,
                engine_ref=ref,
                context=Context(tags=[kind]),
            )

        skipped = 0
        for file, kind, asset, text in self._asset_strings(layout, token):
            if kind == "unreadable":
                skipped += 1
                continue
            add(text, file, kind, asset)
        if skipped:
            sink.publish(
                ProgressEvent(
                    "extract", 1, total, "extract.file.unreadable",
                    level=ProgressLevel.WARNING,
                    detail=f"{skipped} Unity objects could not be read",
                )
            )
        sink.publish(
            ProgressEvent("extract", 1, total, "extract.file.read", detail=layout.data.name)
        )
        for index, (file, path, literals) in enumerate(code_literals(layout), start=2):
            token.raise_if_cancelled()
            if isinstance(literals, Exception):
                sink.publish(
                    ProgressEvent(
                        "extract", index, total, "extract.file.unreadable",
                        level=ProgressLevel.WARNING, detail=f"{path.name}: {literals}",
                    )
                )
                continue
            for text in literals:
                if layout.il2cpp and text in _FRAMEWORK_LITERALS:
                    continue
                add(text, file, "code")
            sink.publish(
                ProgressEvent("extract", index, total, "extract.file.read", detail=path.name)
            )
        first = total - len(loose) + 1
        for index, path in enumerate(loose, start=first):
            token.raise_if_cancelled()
            relative = path.relative_to(layout.root).as_posix()
            try:
                body = text_asset_body(path.read_bytes())
            except OSError as exc:
                body, problem = None, str(exc)
            else:
                problem = "not text"
            if body is None:
                sink.publish(
                    ProgressEvent(
                        "extract", index, total, "extract.file.unreadable",
                        level=ProgressLevel.WARNING, detail=f"{relative}: {problem}",
                    )
                )
                continue
            for text in text_asset_strings(body):
                add(text, relative, "text_file")
            sink.publish(
                ProgressEvent("extract", index, total, "extract.file.read", detail=relative)
            )
        return list(units.values())

    def _asset_strings(
        self, layout: UnityLayout, token: CancellationToken
    ) -> Iterator[tuple[str, str, str, str]]:
        """(file, kind, asset name, text) for every string in the game's assets."""
        try:
            import UnityPy
        except ImportError as exc:
            raise ImportError(
                'Unity support needs UnityPy. Install it with: pip install "bokucho[unity]"'
            ) from exc
        env = UnityPy.load(str(layout.data))
        if not env.objects:
            return
        load_type_trees(env, layout)
        for obj in env.objects:
            token.raise_if_cancelled()
            kind = obj.type.name
            if kind not in ("TextAsset", "MonoBehaviour"):
                continue
            try:
                tree = obj.parse_as_dict()
            except Exception:  # noqa: BLE001 - UnityPy raises anything on a tree it cannot read
                yield obj.assets_file.name, "unreadable", "", ""
                continue
            name = str(tree.get("m_Name", ""))
            if name in _SKIPPED_ASSETS:
                continue
            if kind == "TextAsset":
                body = text_asset_body(tree.get("m_Script", ""))
                pieces = text_asset_strings(body) if body is not None else ()
                group = asset_group("text_asset", name, obj.assets_file)
                for text in pieces:
                    yield group, "text_asset", name, text
            else:
                group = asset_group("mono_behaviour", name, obj.assets_file)
                for text in _strings(tree):
                    yield group, "mono_behaviour", name, text

    def inject(
        self,
        units: list[TextUnit],
        game_path: Path,
        out_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> None:
        del game_path, sink, token
        pairs = {
            unit.source: unit.target for unit in units if unit.target and unit.kind != "image"
        }
        text_dir = out_path / "BepInEx" / "Translation" / xunity_language(self.target_lang) / "Text"
        text_dir.mkdir(parents=True, exist_ok=True)
        (text_dir / TRANSLATION_FILE).write_text(
            translation_file(pairs), encoding="utf-8", newline="\n"
        )
        config = out_path / CONFIG_FILE
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            config_file(self.source_lang, self.target_lang, self.fallback_font, self.textures),
            encoding="utf-8",
            newline="\n",
        )
