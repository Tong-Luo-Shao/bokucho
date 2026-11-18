"""Reporting a problem opens a prefilled GitHub issue form and shows where the log is."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import bokucho
from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.webapp.api import Api
from bokucho.webapp.report import ISSUE_FORM, issue_url

ROOT = Path(__file__).resolve().parents[3]
HOME = Path(r"C:\Users\Translator")


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def test_the_url_fills_the_form_fields() -> None:
    url = issue_url(version="1.0", system="Windows-11", engine="livemaker", error="Boom", home=HOME)

    assert url.startswith(ISSUE_FORM + "?")
    assert _query(url) == {
        "template": ["bug.yml"],
        "version": ["1.0"],
        "system": ["Windows-11"],
        "engine": ["livemaker"],
        "error": ["Boom"],
    }


def test_no_error_leaves_the_field_out() -> None:
    url = issue_url(version="1.0", system="W", engine="", error="", home=HOME)

    assert "error" not in _query(url)


def test_the_home_directory_never_reaches_the_url() -> None:
    error = r"Cannot read c:\users\TRANSLATOR\game\a.lsb or C:/Users/Translator/b.txt"

    url = issue_url(version="1.0", system="W", engine="", error=error, home=HOME)

    assert "translator" not in url.lower()
    assert _query(url)["error"] == [r"Cannot read ~\game\a.lsb or ~/b.txt"]


def test_a_quoted_path_or_mixed_separators_still_hide_the_home() -> None:
    # An OSError shows its path through repr(), doubling every backslash.
    quoted = str(FileNotFoundError(2, "No such file", r"C:\Users\Translator\game\a.lsb"))

    for error in (quoted, r"C:/Users\Translator\x.txt"):
        url = issue_url(version="1.0", system="W", engine="", error=error, home=HOME)
        assert "translator" not in url.lower()
        assert "~" in _query(url)["error"][0]


def test_the_projects_folder_and_the_game_folder_never_reach_the_url() -> None:
    error = r"No project.toml in D:\Work\Secret-Game\strings or D:/Games/Hidden Title/game.exe"

    url = issue_url(
        version="1.0",
        system="W",
        engine="",
        error=error,
        home=HOME,
        project_roots=[Path(r"D:\Work\Secret-Game")],
        game_path=Path(r"D:\Games\Hidden Title"),
    )

    assert "secret" not in url.lower()
    assert "hidden" not in url.lower()
    assert _query(url)["error"] == [r"No project.toml in <project>\strings or <game>/game.exe"]


def test_a_folder_right_before_chinese_text_is_still_hidden() -> None:
    url = issue_url(
        version="1.0",
        system="W",
        engine="",
        error=r"路徑 D:\Work\夏日物語不存在",
        home=HOME,
        project_roots=[Path(r"D:\Work\夏日物語")],
    )

    assert _query(url)["error"] == ["路徑 <project>不存在"]


def test_the_projects_name_never_reaches_the_url() -> None:
    error = "Cannot export 夏日物語的字串; Secret Game failed, Secret Gamer is fine"

    url = issue_url(
        version="1.0", system="W", engine="", error=error, home=HOME, project_name="夏日物語"
    )
    assert _query(url)["error"] == [
        "Cannot export <project>的字串; Secret Game failed, Secret Gamer is fine"
    ]
    url = issue_url(
        version="1.0", system="W", engine="", error=error, home=HOME, project_name="Secret Game"
    )
    assert _query(url)["error"] == [
        "Cannot export 夏日物語的字串; <project> failed, Secret Gamer is fine"
    ]


def test_a_folder_that_only_starts_like_the_home_is_left_alone() -> None:
    error = r"Cannot read C:\Users\TranslatorTwo\a.txt"

    url = issue_url(version="1.0", system="W", engine="", error=error, home=HOME)

    assert _query(url)["error"] == [error]


def test_a_full_stop_ends_a_path_but_a_dotted_name_goes_on() -> None:
    error = r"Missing C:\Users\Translator. Also C:\Users\Translator.Old\a and C:\Users\Translator-2"

    url = issue_url(version="1.0", system="W", engine="", error=error, home=HOME)

    assert _query(url)["error"] == [
        r"Missing ~. Also C:\Users\Translator.Old\a and C:\Users\Translator-2"
    ]


def test_a_lone_surrogate_does_not_stop_the_report() -> None:
    # Windows file names can hold one; quoting it as UTF-8 would raise.
    url = issue_url(version="1.0", system="W", engine="", error="bad \udc80 name", home=HOME)

    assert _query(url)["error"] == ["bad ? name"]


def test_punctuation_and_newlines_arrive_intact() -> None:
    error = "a & b # c = d\nsecond line"

    url = issue_url(version="1", system="W", engine="", error=error, home=HOME)

    assert _query(url)["error"] == [error]


def test_a_long_error_is_cut_and_says_so() -> None:
    url = issue_url(
        version="1.0", system="Windows-11", engine="livemaker", error="錯" * 5000, home=HOME
    )

    assert len(url) < 8000
    assert _query(url)["error"][0].endswith("[truncated]")


def test_every_field_the_url_fills_is_in_the_form() -> None:
    form = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug.yml").read_text(encoding="utf-8")
    ids = set(re.findall(r"^\s+id:\s*(\S+)\s*$", form, re.M))

    url = issue_url(version="1", system="W", engine="e", error="x", home=HOME)

    assert set(_query(url)) - {"template"} <= ids


def test_explorer_is_asked_to_select_the_log_with_the_path_quoted_after_the_comma() -> None:
    # Explorer misreads "/select,C:\a b\c" quoted as one argument and opens
    # Documents instead; it wants the path quoted on its own.
    from bokucho.webapp.__main__ import reveal_command

    path = Path(r"C:\Users\Ann Lee\AppData\Local\Bokucho, test\bokucho.log")

    assert reveal_command(path) == f'explorer /select,"{path}"'


def _api(app_state, event_bus, registry, log_file: Path, opened: list, revealed: list) -> Api:
    return Api(
        app_state,
        event_bus,
        registry=registry,
        url_opener=opened.append,
        file_revealer=revealed.append,
        log_file=log_file,
    )


def test_a_report_names_the_open_projects_engine_and_reveals_the_log(
    app_state, event_bus, registry, tmp_path: Path
) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path / "Secret-Game", "Secret-Game", "ja", "zh-Hant", "renpy")
    )
    app_state.open(project.root)
    log_file = tmp_path / "bokucho.log"
    log_file.write_text("log\n", encoding="utf-8")
    opened: list[str] = []
    revealed: list[Path] = []
    api = _api(app_state, event_bus, registry, log_file, opened, revealed)

    result = api.report_problem(f"Boom in {project.root / 'strings'}")

    [url] = opened
    query = _query(url)
    assert query["engine"] == ["renpy"]
    assert query["version"] == [bokucho.__version__]
    assert query["error"] == [f"Boom in <project>{os.sep}strings"]
    assert "Secret-Game" not in url
    assert revealed == [log_file]
    assert result == {"url": url, "log": str(log_file)}


def test_a_game_chosen_by_its_executable_is_hidden_by_its_folder(
    app_state, event_bus, registry, tmp_path: Path
) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path / "Project", "Project", "ja", "zh-Hant", "renpy")
    )
    app_state.open(project.root)
    game = tmp_path / "Hidden Title"
    app_state.project.config.engine.game_path = str(game / "Game.exe")
    opened: list[str] = []
    api = _api(app_state, event_bus, registry, tmp_path / "bokucho.log", opened, [])

    api.report_problem(f"Cannot read {game / 'data.rpa'}")

    [url] = opened
    assert "hidden" not in url.lower()
    assert _query(url)["error"] == [f"Cannot read <game>{os.sep}data.rpa"]


def test_a_report_hides_the_open_projects_name(
    app_state, event_bus, registry, tmp_path: Path
) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path / "folder", "Hidden Title", "ja", "zh-Hant", "renpy")
    )
    app_state.open(project.root)
    opened: list[str] = []
    api = _api(app_state, event_bus, registry, tmp_path / "bokucho.log", opened, [])

    api.report_problem("Hidden Title has no strings")

    [url] = opened
    assert _query(url)["error"] == ["<project> has no strings"]


def test_a_recent_project_that_is_gone_is_hidden_with_no_project_open(
    app_state, event_bus, registry, tmp_path: Path
) -> None:
    # Opening a recent project that was moved away fails before any project is open.
    gone = tmp_path / "Gone-Game"
    app_state.settings.record_project(gone)
    opened: list[str] = []
    api = _api(app_state, event_bus, registry, tmp_path / "bokucho.log", opened, [])

    api.report_problem(f"Path does not exist: {gone}")

    [url] = opened
    assert "gone-game" not in url.lower()
    assert _query(url)["error"] == ["Path does not exist: <project>"]


def test_a_report_with_no_project_leaves_the_engine_blank(
    app_state, event_bus, registry, tmp_path: Path
) -> None:
    opened: list[str] = []
    api = _api(app_state, event_bus, registry, tmp_path / "bokucho.log", opened, [])

    api.report_problem()

    [url] = opened
    assert _query(url)["engine"] == [""]
    assert "error" not in _query(url)


def test_a_missing_log_still_opens_the_report(
    app_state, event_bus, registry, tmp_path: Path
) -> None:
    opened: list[str] = []
    revealed: list[Path] = []
    api = _api(app_state, event_bus, registry, tmp_path / "bokucho.log", opened, revealed)

    result = api.report_problem("Boom")

    assert len(opened) == 1
    assert revealed == []
    assert result["log"] is None


def test_a_log_that_cannot_be_shown_does_not_fail_the_report(
    app_state, event_bus, registry, tmp_path: Path
) -> None:
    # The issue page is already open by then; the report has done its job.
    log_file = tmp_path / "bokucho.log"
    log_file.write_text("log\n", encoding="utf-8")
    opened: list[str] = []

    def refuse(path: Path) -> None:
        raise OSError("Explorer is unavailable")

    api = Api(
        app_state,
        event_bus,
        registry=registry,
        url_opener=opened.append,
        file_revealer=refuse,
        log_file=log_file,
    )

    result = api.report_problem("Boom")

    assert len(opened) == 1
    assert result == {"url": opened[0], "log": None}


def test_a_report_needs_text(app_state, event_bus, registry, tmp_path: Path) -> None:
    opened: list[str] = []
    api = _api(app_state, event_bus, registry, tmp_path / "bokucho.log", opened, [])

    for error in (None, ["x"]):
        assert api.report_problem(error)["error"]["kind"] == "validation"
    assert opened == []
