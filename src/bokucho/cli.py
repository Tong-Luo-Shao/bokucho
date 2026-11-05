"""Bokucho command-line interface."""

from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import NoReturn

import typer

from bokucho import __version__, logs
from bokucho import images as images_mod
from bokucho import render as render_mod
from bokucho.application.assets import (
    extract_images,
    render_images,
    typeset_font,
)
from bokucho.application.errors import AppError
from bokucho.application.fonts import patch_game_font
from bokucho.application.glossary import list_glossary_entries, upsert_glossary
from bokucho.application.projects import (
    PROJECT_TOML_TEMPLATE as PROJECT_TOML_TEMPLATE,
)
from bokucho.application.projects import (
    CreateProjectRequest,
    create_project,
    extract_text,
    inject_translations,
)
from bokucho.application.quality import run_qa
from bokucho.application.release import (
    build_patch as build_patch_executable,
)
from bokucho.application.release import build_proxy_map, bundled_uif
from bokucho.application.terms import build_glossary
from bokucho.application.text import (
    import_translations,
    list_units,
    mark_units_reviewed,
    transform_project,
    translate_project,
)
from bokucho.application.verify import verify_project
from bokucho.detect import CONFIDENT
from bokucho.detect import detect as detect_engines
from bokucho.engines.cp932_proxy import ProxyMap
from bokucho.infrastructure.factories import (
    make_engine_adapter,
    make_image_codec,
    make_prompt_token_counter,
    make_transform_pipeline,
    make_translation_lanes,
)
from bokucho.model import Status
from bokucho.project import PROJECT_FILE, STRINGS_DIRNAME, Project
from bokucho.qa import ENGINE_CODECS

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Bokucho — open, LLM-first game translation pipeline with human review.",
)

glossary_app = typer.Typer(help="Manage the project glossary (names, terms, tone notes).")
app.add_typer(glossary_app, name="glossary")

images_app = typer.Typer(help="Image-asset localization: inventory, OCR, drafts.")
app.add_typer(images_app, name="images")

def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"bokucho {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Log every step, not only warnings."
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Log every provider prompt and raw response as well (kilobytes each).",
    ),
) -> None:
    """Bokucho command-line interface."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO
    else:
        level = logging.WARNING
    logs.configure(level)


@app.command()
def init(
    path: Path = typer.Argument(
        Path("."), help="Directory to create the project in (created if missing)."
    ),
    name: str = typer.Option(
        None, "--name", help="Project name (defaults to the directory name)."
    ),
    source_lang: str = typer.Option("ja", "--source-lang", help="Source language code."),
    target_lang: str = typer.Option(
        "zh-tw", "--target-lang", help="Target language code."
    ),
    engine: str = typer.Option(
        None, "--engine", help="Engine adapter key. Detected from --game when omitted."
    ),
    game: Path = typer.Option(
        None,
        "--game",
        help=(
            "Unpacked game directory or packed game file: its engine is detected "
            "and written into project.toml."
        ),
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing project.toml."
    ),
) -> None:
    """Create a new project: project.toml, strings/ and an empty glossary."""
    game_path = ""
    detected: str | None = None
    if game is not None:
        if not game.exists():
            _fail(f"Path does not exist: {game}")
        game_path = game.resolve().as_posix()
        detected = _detected_engine(game, given=engine)
    if engine is not None and detected is not None and engine != detected:
        _warn(f"Detected {detected}, but --engine {engine} was given — using {engine}.")
    engine_type = engine or detected or "livemaker"

    try:
        project = create_project(
            CreateProjectRequest(
                destination=path,
                name=name or path.name or "bokucho-project",
                source_lang=source_lang,
                target_lang=target_lang,
                engine=engine_type,
                force=force,
                game_path=game_path,
            )
        )
    except AppError as exc:
        _exit_app_error(exc)

    project_name = project.config.name
    typer.secho(f"Initialized bokucho project '{project_name}' in {path}", fg=typer.colors.GREEN)
    typer.echo(f"  - {PROJECT_FILE}")
    typer.echo(f"  - {STRINGS_DIRNAME}/")
    typer.echo(f"  - {project.glossary_path.name}")
    typer.echo("\nNext: configure [engine] game_path, then `bokucho extract` (Phase 1).")


def _detected_engine(game: Path, given: str | None) -> str | None:
    """The supported engine ``game`` is detected as; stops when none is and none was given."""
    supported = [found for found in detect_engines(game) if found.supported]
    if supported:
        if supported[0].confidence < CONFIDENT:
            _warn(
                f"Low-confidence detection ({supported[0].confidence:.2f}) — "
                "verify [engine] type before extracting."
            )
        return supported[0].engine
    if given is None:
        _fail(
            "No supported engine found there. Run `bokucho detect` for details, "
            "or pass --engine to set it yourself."
        )
    _warn("Engine detection found nothing supported — using --engine as given.")
    return None


def _exit_app_error(exc: AppError) -> NoReturn:
    _fail(str(exc), exc)


def _fail(message: str, cause: BaseException | None = None) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1) from cause


def _warn(message: str) -> None:
    typer.secho(message, fg=typer.colors.YELLOW, err=True)


@app.command()
def detect(
    path: Path = typer.Argument(..., help="Unpacked game directory to inspect."),
) -> None:
    """Identify the game engine, with a confidence score and evidence."""
    if not path.is_dir():
        _fail(f"Not a directory: {path}")

    results = detect_engines(path)
    if not results:
        _warn("Could not identify the engine.")
        typer.echo(
            "If the game is still packed, unpack it first "
            "(LiveMaker: `lmar x Game.exe -o game_files`) and re-run."
        )
        typer.echo("For engines Bokucho does not handle, GARbro often extracts the assets.")
        raise typer.Exit(code=1)

    for found in results:
        if not found.supported:
            color = typer.colors.RED
        elif found.confidence < CONFIDENT:
            color = typer.colors.YELLOW
        else:
            color = typer.colors.GREEN
        typer.secho(
            f"{found.label} ({found.engine}) — confidence {found.confidence:.2f}",
            fg=color,
            bold=True,
        )
        typer.echo(f"  evidence: {', '.join(found.evidence)}")
        if found.supported:
            typer.echo(f"  inject codec: {ENGINE_CODECS.get(found.engine, 'utf-8')}")
        typer.echo(f"  {found.hint}")

    top = results[0]
    if not top.supported:
        raise typer.Exit(code=1)
    if top.confidence < CONFIDENT:
        _warn("Low confidence — verify before trusting this.")


@app.command()
def extract(
    project_dir: Path = typer.Argument(Path("."), help="Project directory."),
) -> None:
    """[Phase 1] Extract game text into the neutral format (strings/*.json)."""
    project = Project.load(project_dir)
    try:
        result = extract_text(project, make_engine_adapter(project))
    except AppError as exc:
        _exit_app_error(exc)
    except RuntimeError as exc:
        _fail(str(exc), exc)

    typer.secho(
        f"Extracted {result.units} text unit(s) from {result.files} file(s) → {result.strings_dir}",
        fg=typer.colors.GREEN,
    )


@app.command()
def inject(
    project_dir: Path = typer.Argument(Path("."), help="Project directory."),
    out: Path = typer.Option(
        None, "--out", help="Output directory for translated files (default: <project>/out)."
    ),
) -> None:
    """[Phase 1] Write translations back into the game (loose .lsb overlay)."""
    project = Project.load(project_dir)
    out_dir = out or (project.root / "out")
    try:
        result = inject_translations(
            project,
            make_engine_adapter(project),
            out_dir,
        )
    except AppError as exc:
        _exit_app_error(exc)
    except RuntimeError as exc:
        _fail(str(exc), exc)
    typer.secho(
        f"Injected {result.translated} translation(s) across "
        f"{result.files} file(s) → {result.out_dir}",
        fg=typer.colors.GREEN,
    )


@glossary_app.command("add")
def glossary_add(
    source: str = typer.Argument(..., help="Source-language term (e.g. a character name)."),
    target: str = typer.Argument(..., help="The translation every future run must use."),
    note: str = typer.Option(None, "--note", help="Optional tone/usage note."),
    project_dir: Path = typer.Option(Path("."), "--project", "-p", help="Project directory."),
    global_: bool = typer.Option(
        False, "--global", help="Write to the user-level glossary shared by all projects."
    ),
) -> None:
    """Add or update a glossary term. Later `translate` runs pick it up automatically.

    This is the review write-back loop: when a reviewer corrects a name, record
    it here and machine drafts stay consistent from then on. Use --global for
    terms every project should share; project entries override global ones.
    """
    project = Project.load(project_dir)
    scope = "global" if global_ else "project"
    try:
        created = upsert_glossary(project, source, target, note, scope=scope)
    except AppError as exc:
        _exit_app_error(exc)
    action = "Added" if created else "Updated"
    typer.secho(f"{action} ({scope}): {source} => {target}", fg=typer.colors.GREEN)


@glossary_app.command("list")
def glossary_list(
    project_dir: Path = typer.Option(Path("."), "--project", "-p", help="Project directory."),
) -> None:
    """Show merged glossary terms with their origin (project overrides global)."""
    project = Project.load(project_dir)
    entries = list_glossary_entries(project)
    if not entries:
        typer.echo("(empty)")
        return
    for entry, origin in entries:
        note = f"  # {entry.note}" if entry.note else ""
        typer.echo(f"{entry.source} => {entry.target}  ({origin}){note}")


@app.command("build-glossary")
def build_glossary_command(
    project_dir: Path = typer.Argument(Path("."), help="Project directory."),
) -> None:
    """[Phase 2] Read the script for proper nouns and settle one rendering each."""
    project = Project.load(project_dir)
    try:
        result = build_glossary(
            project,
            lanes=make_translation_lanes(project),
            token_counter=make_prompt_token_counter(project),
        )
    except AppError as exc:
        _exit_app_error(exc)

    typer.secho(
        f"Settled {result.added} name(s) from {result.examined} entrie(s); "
        f"{result.kept} already decided were left alone.",
        fg=typer.colors.GREEN,
    )


@app.command()
def translate(
    project_dir: Path = typer.Argument(Path("."), help="Project directory."),
    limit: int = typer.Option(
        None, "--limit", help="Translate at most N units (trial runs / cost control)."
    ),
) -> None:
    """[Phase 2] LLM-translate untranslated units -> mt. Never touches human work."""
    project = Project.load(project_dir)
    try:
        result = translate_project(
            project,
            limit=limit,
            lanes=make_translation_lanes(project),
            token_counter=make_prompt_token_counter(project),
        )
    except AppError as exc:
        _exit_app_error(exc)

    color = typer.colors.GREEN if result.failed == 0 else typer.colors.YELLOW
    typer.secho(
        f"Translated {result.translated} unit(s) -> mt; "
        f"{result.failed} failed the format check "
        f"(left untranslated).",
        fg=color,
    )


@images_app.command("extract")
def images_extract(
    project_dir: Path = typer.Argument(Path("."), help="Project directory."),
    ocr: bool = typer.Option(
        False, "--ocr", help="OCR each image with PP-OCRv6 and create image units."
    ),
) -> None:
    """[Phase 7a] Inventory the game's images (optionally OCR their text).

    Writes images-inventory.json. With --ocr, every image whose OCR yields text
    becomes a TextUnit(kind=image) in strings/images.json and flows through the
    normal translate/transform/glossary pipeline.
    """
    project = Project.load(project_dir)
    reader = None
    open_reader = None
    if ocr:
        try:
            reader = images_mod.get_ocr(locating=True, allow_download=True)
        except AppError as exc:
            _exit_app_error(exc)

        def open_reader() -> object:
            return images_mod.get_ocr(reads_crops=True, allow_download=True)

    try:
        result = extract_images(project, reader, open_reader=open_reader)
    except AppError as exc:
        _exit_app_error(exc)
    typer.secho(
        f"Inventoried {result.total} image(s) ({result.failed} unreadable) -> "
        f"{result.inventory_path.name}"
        + (f"; {result.added} new image unit(s) from OCR" if ocr else ""),
        fg=typer.colors.GREEN,
    )


_TRANSFORM_EXAMPLE = """\
No [[transform]] configured in project.toml. Example:

    [[transform]]
    type = "opencc"
    config = "s2twp"            # zh-cn -> zh-tw + vocabulary
    # custom_dict = "tw-terms.tsv"

    [[transform]]
    type = "punctuation"
    profile = "zh-tw"           # curly quotes -> corner brackets"""


@images_app.command("render")
def images_render_cmd(
    project_dir: Path = typer.Argument(Path("."), help="Project directory."),
) -> None:
    """[Phase 7b] Auto erase + typeset drafts for every image unit with a region.

    Drafts go straight into the overlay in the game's own format, and stay
    human-reviewed: the units remain `mt` until someone says otherwise.
    """
    project = Project.load(project_dir)
    try:
        font = typeset_font(project)
    except AppError as exc:
        _exit_app_error(exc)
    if font and not project.config.images.font:
        _warn(
            f"[images] font not set — using system font {font} (local preview "
            "only; use an OFL font like Taipei Sans / Noto Sans CJK for release)."
        )
    if not font or not Path(font).is_file():
        _fail("No usable font. Set [images] font in project.toml.")

    try:
        inpaint_fn = render_mod.make_inpaint_fn(render_mod.resolve_model_path())
    except (render_mod.ModelMissingError, ImportError) as exc:
        _fail(str(exc), exc)

    try:
        run = render_images(project, font, inpaint_fn, codec=make_image_codec())
    except AppError as exc:
        _exit_app_error(exc)

    by_status: dict[str, list] = defaultdict(list)
    for result in run.results:
        by_status[result.status].append(result)
    typer.secho(
        f"Rendered {len(by_status['rendered'])} draft(s) → {run.out_dir}",
        fg=typer.colors.GREEN,
    )
    for status in ("needs_manual", "skipped"):
        for result in by_status[status]:
            typer.echo(f"  {status}: {result.image} — {result.reason}")
    for image, message in run.errors:
        typer.secho(f"  failed: {image} — {message}", fg=typer.colors.RED, err=True)

@app.command()
def transform(
    project_dir: Path = typer.Argument(Path("."), help="Project directory."),
) -> None:
    """[Phase 5] Apply localization transforms to targets (idempotent).

    Runs the ordered [[transform]] pipeline from project.toml on every
    unprotected target. Never touches reviewed units.
    """
    project = Project.load(project_dir)
    specs = project.config.transforms
    if not specs:
        _warn(_TRANSFORM_EXAMPLE)
        raise typer.Exit(code=1)
    try:
        result = transform_project(project, make_transform_pipeline(project))
    except AppError as exc:
        _exit_app_error(exc)
    typer.secho(
        f"Transformed {result.changed} target(s) through "
        f"{result.transforms} transform(s).",
        fg=typer.colors.GREEN,
    )


@app.command("import")
def import_(
    file: Path = typer.Argument(
        ..., help='Existing translations as JSON {"source": "target"} (e.g. MTool format).'
    ),
    project_dir: Path = typer.Option(Path("."), "--project", "-p", help="Project directory."),
) -> None:
    """[Phase 5] Import existing translations (e.g. zh-cn) as mt targets.

    Matches by source text and skips the LLM entirely — pair with `transform`
    for the zh-cn -> zh-tw flow. Never overwrites reviewed units.
    (Neutral-format files can simply be copied into strings/ instead.)
    """
    project = Project.load(project_dir)
    mapping = json.loads(file.read_text(encoding="utf-8-sig"))
    if not isinstance(mapping, dict):
        _fail('Expected a JSON object of {"source": "target"}.')
    try:
        result = import_translations(project, mapping)
    except AppError as exc:
        _exit_app_error(exc)
    color = typer.colors.GREEN if result.imported else typer.colors.YELLOW
    typer.secho(
        f"Imported {result.imported} target(s) as mt; "
        f"{result.unmatched} entry(ies) had no matching source in this project.",
        fg=color,
    )


@app.command()
def review(
    project_dir: Path = typer.Argument(Path("."), help="Project directory."),
    status: str = typer.Option("mt", "--status", help="Status to list."),
    mark: list[str] = typer.Option(
        None, "--mark", help="Unit id(s) to mark as reviewed (repeatable)."
    ),
) -> None:
    """[Phase 5] List units awaiting review, or mark unit ids as reviewed.

    Reviewing is just editing strings/*.json (git-diffable); this command is the
    convenience view over it.
    """
    project = Project.load(project_dir)

    if mark:
        result = mark_units_reviewed(project, mark, enforce_format=False)
        if result.missing:
            _warn(f"Not found (or has no target): {', '.join(result.missing)}")
        typer.secho(f"Marked {result.marked} unit(s) as reviewed.", fg=typer.colors.GREEN)
        raise typer.Exit(code=0 if not result.missing else 1)

    try:
        wanted = Status(status)
    except ValueError:
        _fail(
            f"Unknown status: {status!r} (use one of: "
            f"{', '.join(s.value for s in Status)})"
        )

    units = list_units(project, wanted)
    for source_file, unit in units:
        src = unit.source.replace("\n", "\\n")
        tgt = unit.target.replace("\n", "\\n")
        typer.echo(f"{source_file}  {unit.id}")
        typer.echo(f"  {src[:60]}  ->  {tgt[:60]}")
    typer.echo(f"\n{len(units)} unit(s) with status '{wanted.value}'.")


@app.command()
def qa(
    project_dir: Path = typer.Argument(Path("."), help="Project directory."),
    json_out: Path = typer.Option(
        None, "--json", help="Also write the full issue list to this JSON file."
    ),
) -> None:
    """[Phase 4] Run QA checks and report. Exits non-zero on defects (CI gate).

    Defects = lost variables, a source handed back untranslated, overlong lines,
    un-encodable characters, Simplified residue. Line-count differences are
    warnings. Untranslated / unreviewed counts are progress, not defects.
    """
    project = Project.load(project_dir)
    try:
        report = run_qa(project, json_out)
    except AppError as exc:
        _exit_app_error(exc)
    progress = ", ".join(
        f"{status}: {report.status_counts[status]}" for status in Status
    )
    typer.echo(f"Progress ({report.total} units) — {progress}")

    if json_out:
        typer.echo(f"Full report: {json_out}")

    if not report.blocking_issues:
        typer.secho("QA passed: no blocking defects in translated units.", fg=typer.colors.GREEN)
        for issue in report.issues:
            typer.secho(
                f"[{issue.check}] warning: {issue.file} {issue.unit_id}: {issue.message}",
                fg=typer.colors.YELLOW,
            )
        return

    by_check = defaultdict(list)
    for issue in report.issues:
        by_check[issue.check].append(issue)
    for check, group in sorted(by_check.items()):
        warning = all(issue.severity == "warning" for issue in group)
        typer.secho(
            f"\n[{check}] {len(group)} {'warning(s)' if warning else 'issue(s)'}",
            fg=typer.colors.YELLOW if warning else typer.colors.RED,
            bold=True,
        )
        for issue in group[:10]:
            typer.echo(f"  {issue.file} {issue.unit_id}: {issue.message}")
        if len(group) > 10:
            typer.echo(f"  ... and {len(group) - 10} more (use --json for the full list)")
    raise typer.Exit(code=1)


@app.command()
def verify(
    project_dir: Path = typer.Argument(Path("."), help="Project directory."),
    samples: int = typer.Option(3, "--samples", help="Failing units to show per property."),
) -> None:
    """Measure how a translation run behaved, over what is already on disk.

    Reports properties rather than defects: how often the layout came back
    intact, whether tags survived, how much source script is left, whether the
    glossary was used. Nothing here blocks a release — `bokucho qa` does that.
    """
    project = Project.load(project_dir)
    map_path = project.root / project.config.engine.proxy_map
    proxy_chars = (
        frozenset(ProxyMap.load(map_path).real2proxy) if map_path.is_file() else frozenset()
    )
    try:
        report = verify_project(project, proxy_chars)
    except AppError as exc:
        _exit_app_error(exc)

    typer.echo(f"Measured {report.examined} translated unit(s)")
    for prop in report.properties:
        held = report.examined - len(prop.failures)
        colour = typer.colors.GREEN if prop.ok else typer.colors.YELLOW
        typer.secho(
            f"  {prop.key:<9} {held}/{report.examined}  {prop.description}",
            fg=colour,
        )
        for unit_id, message in prop.failures[:samples]:
            typer.echo(f"      {unit_id}: {message}")
        if len(prop.failures) > samples:
            typer.echo(f"      ... and {len(prop.failures) - samples} more")


@app.command()
def patch_runtime(
    project_dir: Path = typer.Argument(Path("."), help="Project directory."),
) -> None:
    """[Phase 5] Build/refresh the CP932 font-proxy map from current translations.

    Every non-CP932 character in the targets gets a rare CP932 donor character;
    inject/extract then use the map automatically. Existing pairs stay stable so
    fonts built from an older map remain valid, except a pair whose donor the
    text now writes literally, which moves to a new donor.
    """
    project = Project.load(project_dir)
    try:
        result = build_proxy_map(project)
    except AppError as exc:
        _exit_app_error(exc)
    typer.secho(
        f"Proxy map: {result.total} char(s) mapped ({result.added} new) -> "
        f"{result.path.name}",
        fg=typer.colors.GREEN,
    )
    if result.repaired:
        typer.secho(
            f"Repaired {result.repaired} character(s) that had been sharing a donor "
            "with another character or with the text. Re-run `bokucho inject` and "
            "rebuild the font: "
            "anything built from the old map drew them wrong.",
            fg=typer.colors.YELLOW,
        )
    typer.echo(
        "Note: correct on-screen display needs a proxy font built from this map "
        "(see FONT_PROXY.md); until then proxied chars render as donor glyphs."
    )


@app.command()
def patch_font(
    project_dir: Path = typer.Argument(Path("."), help="Project directory."),
) -> None:
    """[Phase 5] Draw the proxied characters into the game's own font and rebuild it.

    The game rasterises from its embedded pixel font itself, so the player
    installs nothing and picks nothing: this redraws the donor code points from
    the `[[font.source]]` faces, points every script's font slot at that font,
    and writes a rebuilt game into `dist/`.
    """
    project = Project.load(project_dir)
    try:
        result = patch_game_font(project, make_engine_adapter)
    except AppError as exc:
        _exit_app_error(exc)
    except FileNotFoundError as exc:
        _fail(str(exc), exc)
    typer.secho(
        f"Drew {result.drawn} character(s) into {result.font.name} "
        f"({result.widened} given a bigger cell); {result.slots} font slot(s) "
        f"across {result.scripts} script(s) now name it.",
        fg=typer.colors.GREEN,
    )
    if result.undrawable:
        typer.secho(
            f"No configured face draws {result.undrawable} — those stay blank. "
            "Add a face that covers them, or take them out of the translation.",
            fg=typer.colors.YELLOW,
        )
    typer.secho(f"Rebuilt game: {result.archive}", fg=typer.colors.GREEN)


@app.command()
def build_patch(
    project_dir: Path = typer.Argument(Path("."), help="Project directory."),
    out: Path = typer.Option(
        None, "--out", help="Output directory (default: <project>/dist)."
    ),
) -> None:
    """[Phase 5] Build <project>-<source>2<target>-patcher.exe (no game files)."""
    project = Project.load(project_dir)
    try:
        result = build_patch_executable(
            project,
            out,
            adapter_factory=make_engine_adapter,
            uif=bundled_uif(),
        )
    except AppError as exc:
        _exit_app_error(exc)
    typer.secho(f"Patch built: {result.executable}", fg=typer.colors.GREEN)
    typer.echo("Contains no game files — players apply it to their own copy and can restore it.")


if __name__ == "__main__":
    app()
