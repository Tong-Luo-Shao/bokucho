"""Start the patcher: the window, or headless ``--apply/--restore FOLDER``."""

from __future__ import annotations

import sys
from pathlib import Path

from bokucho.patcher.core import PatchError, apply, restore, restored_message
from bokucho.patcher.payload import NotAPatch, read_patch


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if getattr(sys, "frozen", False):
        source = Path(sys.executable)
    elif arguments:
        source = Path(arguments.pop(0))
    else:
        print("usage: python -m bokucho.patcher PATCH [--apply/--restore FOLDER]", file=sys.stderr)
        return 2
    headless = len(arguments) == 2 and arguments[0] in {"--apply", "--restore"}
    try:
        patch = read_patch(source.read_bytes())
    except (OSError, NotAPatch) as error:
        message = f"補丁檔不完整或已損壞，請重新下載。（{error}）"
        if headless:
            print(message, file=sys.stderr)
        else:
            from bokucho.patcher.window import show_error

            show_error(message)
        return 1
    if headless:
        try:
            if arguments[0] == "--restore":
                print(restored_message(restore(Path(arguments[1]), patch)))
            else:
                apply(Path(arguments[1]), patch)
        except PatchError as error:
            print(error, file=sys.stderr)
            return 1
        return 0
    from bokucho.patcher.window import run

    run(patch, source.parent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
