"""Auto-unpacking of a LiveMaker archive when game_path points at a file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bokucho.application.contracts import CancellationFlag, NullProgressSink
from bokucho.application.errors import InvalidProject, JobCancelled
from bokucho.patcher.core import RECORD, sha256_of

livemaker = pytest.importorskip("livemaker")
from livemaker.archive import LMArchive, LMCompressType  # noqa: E402

from bokucho.engines.livemaker import LiveMakerAdapter  # noqa: E402


def _write_archive(path: Path, files: dict[str, bytes]) -> None:
    with LMArchive(str(path), mode="w") as archive:
        for name, data in files.items():
            archive.writebytes(name, data, compress_type=LMCompressType.NONE)


def test_game_root_unpacks_archive_file_into_sibling_directory(tmp_path: Path) -> None:
    archive = tmp_path / "MyGame.dat"
    _write_archive(archive, {"a.lsb": b"lsb-a", "sub/b.lsb": b"lsb-b"})

    root = LiveMakerAdapter()._game_root(archive, NullProgressSink(), CancellationFlag())

    assert root == tmp_path / "MyGame"
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*.lsb")) == [
        "a.lsb",
        "sub/b.lsb",
    ]
    assert (root / "a.lsb").read_bytes() == b"lsb-a"


def test_unpack_honors_cancellation_and_leaves_no_cache(tmp_path: Path) -> None:
    archive = tmp_path / "MyGame.dat"
    _write_archive(archive, {f"file{i}.lsb": b"x" for i in range(5)})

    token = CancellationFlag()
    token.cancel()

    with pytest.raises(JobCancelled):
        LiveMakerAdapter()._game_root(archive, NullProgressSink(), token)

    assert not (tmp_path / "MyGame").exists()


def test_an_updated_archive_is_unpacked_again(tmp_path: Path) -> None:
    archive = tmp_path / "MyGame.dat"
    _write_archive(archive, {"a.lsb": b"version-1"})
    adapter = LiveMakerAdapter(workspace=tmp_path / "unpacked")
    adapter._game_root(archive, NullProgressSink(), CancellationFlag())
    _write_archive(archive, {"a.lsb": b"version-2"})

    root = adapter._game_root(archive, NullProgressSink(), CancellationFlag())

    assert (root / "a.lsb").read_bytes() == b"version-2"


def _patched(archive: Path, translated: dict[str, bytes]) -> None:
    """Apply a patch as the patcher does: the original moves to ``.bak``, the result is recorded."""
    archive.replace(archive.with_name(f"{archive.name}.bak"))
    _write_archive(archive, translated)
    record = {"added": [], "recipe": "recipe", "compiled": {archive.name: sha256_of(archive)}}
    (archive.parent / RECORD).write_text(json.dumps(record), encoding="utf-8")


def test_a_game_the_patch_was_applied_to_is_read_from_its_original(tmp_path: Path) -> None:
    archive = tmp_path / "MyGame.dat"
    _write_archive(archive, {"a.lsb": b"original"})
    adapter = LiveMakerAdapter(workspace=tmp_path / "unpacked")
    adapter._game_root(archive, NullProgressSink(), CancellationFlag())
    _patched(archive, {"a.lsb": b"translated"})

    root = adapter._game_root(archive, NullProgressSink(), CancellationFlag())

    assert (root / "a.lsb").read_bytes() == b"original"


def test_a_patched_game_whose_original_the_cache_cannot_show_is_refused(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "MyGame.dat"
    _write_archive(archive, {"a.lsb": b"original"})
    adapter = LiveMakerAdapter(workspace=tmp_path / "unpacked")
    adapter._game_root(archive, NullProgressSink(), CancellationFlag())
    (tmp_path / "unpacked" / "MyGame.sha256").unlink()
    _patched(archive, {"a.lsb": b"translated"})

    with pytest.raises(InvalidProject, match="Restore the original"):
        adapter._game_root(archive, NullProgressSink(), CancellationFlag())
    assert (tmp_path / "unpacked" / "MyGame" / "a.lsb").read_bytes() == b"original"


def test_a_game_the_patch_was_applied_to_without_its_backup_is_refused(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "MyGame.dat"
    _write_archive(archive, {"a.lsb": b"original"})
    adapter = LiveMakerAdapter(workspace=tmp_path / "unpacked")
    adapter._game_root(archive, NullProgressSink(), CancellationFlag())
    _patched(archive, {"a.lsb": b"translated"})
    (tmp_path / "MyGame.dat.bak").unlink()

    with pytest.raises(InvalidProject, match="Restore the original"):
        adapter._game_root(archive, NullProgressSink(), CancellationFlag())


def test_a_first_unpack_of_a_game_the_patch_was_applied_to_is_refused(tmp_path: Path) -> None:
    archive = tmp_path / "MyGame.dat"
    _write_archive(archive, {"a.lsb": b"original"})
    _patched(archive, {"a.lsb": b"translated"})

    with pytest.raises(InvalidProject, match="Restore the original"):
        LiveMakerAdapter(workspace=tmp_path / "unpacked")._game_root(
            archive, NullProgressSink(), CancellationFlag()
        )
    assert not (tmp_path / "unpacked" / "MyGame").exists()


def test_a_game_this_project_patched_and_then_updated_is_unpacked_again(tmp_path: Path) -> None:
    archive = tmp_path / "MyGame.dat"
    _write_archive(archive, {"a.lsb": b"version-1"})
    adapter = LiveMakerAdapter(workspace=tmp_path / "unpacked")
    adapter._game_root(archive, NullProgressSink(), CancellationFlag())
    _patched(archive, {"a.lsb": b"translated"})
    _write_archive(archive, {"a.lsb": b"version-2"})

    root = adapter._game_root(archive, NullProgressSink(), CancellationFlag())

    assert (root / "a.lsb").read_bytes() == b"version-2"


@pytest.mark.parametrize("stamped", [True, False])
def test_a_game_patched_without_a_record_is_refused(tmp_path: Path, stamped: bool) -> None:
    archive = tmp_path / "MyGame.dat"
    _write_archive(archive, {"a.lsb": b"original"})
    adapter = LiveMakerAdapter(workspace=tmp_path / "unpacked")
    adapter._game_root(archive, NullProgressSink(), CancellationFlag())
    if not stamped:
        (tmp_path / "unpacked" / "MyGame.sha256").unlink()
    archive.replace(tmp_path / "MyGame.dat.bak")
    _write_archive(archive, {"a.lsb": b"translated"})

    with pytest.raises(InvalidProject, match="move MyGame.dat.bak away"):
        adapter._game_root(archive, NullProgressSink(), CancellationFlag())
    assert (tmp_path / "unpacked" / "MyGame" / "a.lsb").read_bytes() == b"original"


def test_a_patched_game_whose_unpacked_original_was_deleted_is_refused(tmp_path: Path) -> None:
    archive = tmp_path / "MyGame.dat"
    _write_archive(archive, {"a.lsb": b"original"})
    adapter = LiveMakerAdapter(workspace=tmp_path / "unpacked")
    adapter._game_root(archive, NullProgressSink(), CancellationFlag())
    _patched(archive, {"a.lsb": b"translated"})
    for unpacked in (tmp_path / "unpacked" / "MyGame").iterdir():
        unpacked.unlink()
    (tmp_path / "unpacked" / "MyGame").rmdir()

    with pytest.raises(InvalidProject, match="Restore the original"):
        adapter._game_root(archive, NullProgressSink(), CancellationFlag())


def test_a_backup_that_is_the_game_itself_does_not_stop_an_update(tmp_path: Path) -> None:
    archive = tmp_path / "MyGame.dat"
    _write_archive(archive, {"a.lsb": b"version-1"})
    adapter = LiveMakerAdapter(workspace=tmp_path / "unpacked")
    adapter._game_root(archive, NullProgressSink(), CancellationFlag())
    _write_archive(archive, {"a.lsb": b"version-2"})
    _write_archive(tmp_path / "MyGame.dat.bak", {"a.lsb": b"version-2"})

    root = adapter._game_root(archive, NullProgressSink(), CancellationFlag())

    assert (root / "a.lsb").read_bytes() == b"version-2"


def test_a_first_unpack_ignores_a_backup_beside_the_archive(tmp_path: Path) -> None:
    archive = tmp_path / "MyGame.dat"
    _write_archive(archive, {"a.lsb": b"original"})
    _write_archive(tmp_path / "MyGame.dat.bak", {"a.lsb": b"original"})

    root = LiveMakerAdapter(workspace=tmp_path / "unpacked")._game_root(
        archive, NullProgressSink(), CancellationFlag()
    )

    assert (root / "a.lsb").read_bytes() == b"original"
