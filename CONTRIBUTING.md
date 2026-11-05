# Contributing to Bokucho

Thanks for your interest! Bokucho is designed to be extended without touching the
core. The two extension points:

- **Add an engine** → implement [`EngineAdapter`](src/bokucho/engines/base.py)
  (`extract` / `inject`) in `src/bokucho/engines/<engine>.py`.
- **Add an LLM** → implement [`Translator`](src/bokucho/translators/base.py)
  (`translate`) in `src/bokucho/translators/<provider>.py`.

Everything flows through the neutral [`TextUnit`](src/bokucho/model.py); keep
engine- and provider-specific details out of the rest of the pipeline (engines
hide theirs inside `engine_ref`).

## Dev setup

```bash
uv sync --locked --extra dev --extra web --extra images
```

`images` pulls torch, so it is the heavy one; CI installs it so the tests that
use torch run there. [Bun](https://bun.sh) runs `tests/web_assets/*.test.js`;
install it to run those tests too.

## Before opening a PR

```bash
uv run --locked --extra dev --extra web --extra images ruff check .
uv run --locked --extra dev --extra web --extra images pytest
bun test tests/web_assets
```

These are the commands CI runs, on Linux for Python 3.12–3.14 and on Windows,
where the portable executable is also built and verified.

- Match the style of the surrounding code.
- Add tests for new behavior.
- Keep public APIs typed (Pydantic models / explicit signatures).

## Project layout

```
src/bokucho/
  model.py          # TextUnit + status lifecycle (the backbone)
  project.py        # project.toml + on-disk layout
  cli.py            # Typer CLI
  detect.py         # engine detection
  qa.py             # QA checks (variables, echo, length, encoding, residue)
  images.py         # picture inventory + OCR entry points
  render.py         # LaMa erase + typeset
  application/      # UI-independent use cases shared by the CLI and the GUI
  engines/base.py   # EngineAdapter ABC (plugin seam #1)
  translators/base.py  # Translator ABC (plugin seam #2)
  translate/        # glossary + prompts
  transform/        # localization transforms (OpenCC, punctuation)
  infrastructure/   # credentials, rate limits, tokens, PP-OCR, OCR runtime
  sync/             # JSON / TOML round-trip, atomic writes
  webapp/           # pywebview host and Python bridge
  web/              # buildless HTML, CSS, and JavaScript frontend
  resources/        # translation catalogs and bundled assets
tests/
  unit/             # unit tests
  integration/      # real-game round-trips (skip without the game)
  web_assets/       # frontend tests, run under bun
tools/              # build, OCR runtime/model staging, sidecar runners
```

## License of contributions

By contributing you agree your work is licensed under the project's GPLv3.

## Desktop development

GUI behavior is covered by `tests/unit/webapp/` and `tests/web_assets/`. The
GUI reads the translation catalogs under `src/bokucho/resources/translations/`
as they are, at runtime.

From the `src` directory, run the desktop application with:

```bash
uv run main.py
```

From the repository root, the installed console script remains available:

```bash
uv run --extra web bokucho-gui
```

Set `BOKUCHO_DEV=1` while developing: it opens the web inspector and watches
`src/bokucho/`.

Saving an `.html`, `.css` or `.js` file reloads the window. The project lives
on the Python side of the bridge, so the reload keeps the open project and the
current screen.

Saving a `.py` file restarts the process instead. The bridge object is
introspected into JavaScript stubs when the window is created and holds the
project, the event bus and the job runner, so re-importing a module would leave
half of that graph pointing at the old classes. The restart repeats the command
that started the app and lands back on the project list. While a job is running
the restart is skipped, so the batch keeps its process; save again once the job
is done. A burst of saves that touched a module restarts even when an asset
landed last, so after format-on-save or a branch switch the window runs the code
on disk.

```bash
BOKUCHO_DEV=1 uv run --extra web bokucho-gui
```

Building the portable release locally:

```bash
uv run --locked --extra build python tools/build.py            # with the OCR bootstrap
uv run --locked --extra build python tools/build.py --app-only # the application alone
```
