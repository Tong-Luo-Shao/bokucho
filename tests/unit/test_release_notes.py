"""A tagged GitHub release lists what users get since the tag before it, and its checksums."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
from pathlib import Path

import pytest

RELEASE_TOOL = Path(__file__).resolve().parents[2] / "tools" / "release.py"


@pytest.fixture(scope="module")
def release():
    spec = importlib.util.spec_from_file_location("bokucho_release_script", RELEASE_TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    settings = (
        "user.name=Bokucho",
        "user.email=bokucho@example.invalid",
        "commit.gpgsign=false",
        "tag.gpgsign=false",
    )

    def git(*arguments: str) -> None:
        options = [part for setting in settings for part in ("-c", setting)]
        subprocess.run(["git", *options, *arguments], cwd=tmp_path, check=True, capture_output=True)

    def commit(subject: str, body: str = "") -> None:
        git("commit", "--allow-empty", "-m", subject, *(("-m", body) if body else ()))

    git("init")
    commit("feat(rpgmaker): extract script text for XP, VX and VX Ace")
    commit("docs: describe the release workflow")
    git("tag", "v0.1.0")
    commit("fix(web): keep the queue scrolled to the line under review (#12)")
    commit("feat: export the glossary as CSV")
    commit("refactor(images): split the inventory into its own module")
    commit("perf(ocr): batch the detector across pictures")
    git("tag", "-a", "v0.2.0", "-m", "Bokucho v0.2.0")
    commit(
        "feat: conversion choices and counts",
        "Moves these changes:\n\n"
        "- feat(transform): keep a conversion choice in project.toml\n"
        "- fix(web): count a conversion before it runs\n"
        "- docs: describe the transform dialog\n"
        "- Update README.md",
    )
    commit("feat(images): letter vertical text", "- top to bottom\n- right to left")
    git("tag", "-a", "v0.3.0", "-m", "Bokucho v0.3.0")
    return tmp_path


def test_notes_list_added_and_fixed_work_since_the_previous_tag_by_area(release, repository):
    notes = release.release_notes(release.commit_subjects("v0.2.0", repository))

    assert notes == (
        "Core\n"
        "+ Export the glossary as CSV\n"
        "\n"
        "Images\n"
        "* [ocr] Batch the detector across pictures\n"
        "\n"
        "GUI\n"
        "* [web] Keep the queue scrolled to the line under review (#12)\n"
        "\n"
    )


def test_a_squashed_commit_gives_the_subjects_its_body_lists(release, repository):
    # A move to the public repository lands as one commit whose body lists
    # the commits it squashes; a body listed in prose keeps its own subject.
    notes = release.release_notes(release.commit_subjects("v0.3.0", repository))

    assert notes == (
        "Core\n"
        "+ [transform] Keep a conversion choice in project.toml\n"
        "\n"
        "Images\n"
        "+ [images] Letter vertical text\n"
        "\n"
        "GUI\n"
        "* [web] Count a conversion before it runs\n"
        "\n"
    )


def test_the_first_release_lists_everything_before_its_tag(release, repository):
    notes = release.release_notes(release.commit_subjects("v0.1.0", repository))

    assert notes == "Engines\n+ [rpgmaker] Extract script text for XP, VX and VX Ace\n\n"


def test_checksums_verify_with_sha256sum_and_sha512sum(release, tmp_path: Path):
    archive = tmp_path / "Bokucho-Windows-x64.zip"
    archive.write_bytes(b"portable build")

    release.write_checksums([archive], tmp_path)

    assert (tmp_path / "SHA2-256SUMS").read_bytes() == (
        f"{hashlib.sha256(b'portable build').hexdigest()}  Bokucho-Windows-x64.zip\n".encode()
    )
    assert (tmp_path / "SHA2-512SUMS").read_bytes() == (
        f"{hashlib.sha512(b'portable build').hexdigest()}  Bokucho-Windows-x64.zip\n".encode()
    )
