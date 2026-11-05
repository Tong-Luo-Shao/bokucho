"""Opening a completed release stays inside the current project's dist folder."""

from pathlib import Path

from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.webapp.api import Api


def test_open_release_folder_opens_only_a_published_artifact(
    app_state, event_bus, registry, tmp_path: Path
) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "Release", "ja", "zh-Hant", "renpy")
    )
    app_state.open(project.root)
    artifact = project.root / "dist" / "20261014-201206"
    artifact.mkdir(parents=True)
    opened = []
    api = Api(app_state, event_bus, registry=registry, folder_opener=opened.append)

    assert api.open_release_folder(str(artifact)) == {"opened": str(artifact.resolve())}
    assert opened == [artifact.resolve()]

    outside = tmp_path / "outside"
    outside.mkdir()
    refused = api.open_release_folder(str(outside))
    assert refused["error"]["kind"] == "validation"
    assert opened == [artifact.resolve()]
