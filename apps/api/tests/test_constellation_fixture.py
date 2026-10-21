"""The web mocks' constellation is the engine's layout of the same commit, as the API sends it
(regenerate it with ``uv run python scripts/mock_constellation.py`` after changing the layout)."""

from __future__ import annotations

import tarfile
import uuid
from pathlib import Path
from typing import Any

import orjson

from sensei_api.routers.constellation import ConstellationOut, dirs_out, points_out
from sensei_engine.ingest.constellation import DirectoryLayout
from sensei_engine.ingest.discover import discover

ROOT = Path(__file__).resolve().parents[3]
TARBALL = ROOT / "packages" / "engine" / "tests" / "fixtures" / "fastapi-template.tar.gz"
MOCK = ROOT / "apps" / "web" / "src" / "mocks" / "fixtures" / "constellation.json"


def _without_ids(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in point.items() if k != "file_id"} for point in points]


def test_the_web_mock_is_the_layout_of_its_commit(tmp_path: Path) -> None:
    with tarfile.open(TARBALL) as tar:
        tar.extractall(tmp_path, filter="data")
    found = discover(tmp_path / "fastapi-template", max_files=10_000)
    layout = DirectoryLayout(file.path for file in found.files)
    points = [
        layout.point(
            file_id=uuid.uuid4(),
            path=file.path,
            role=file.role.value,
            language=file.language,
            size=file.size,
            loc=file.loc,
        )
        for file in found.files
    ]
    mock = orjson.loads(MOCK.read_bytes())
    ConstellationOut.model_validate(mock)  # the API's shape
    expected_dirs = [d.model_dump(mode="json") for d in dirs_out(layout.dirs)]
    expected_points = [p.model_dump(mode="json") for p in points_out(points)]
    assert mock["dirs"] == expected_dirs
    assert _without_ids(mock["points"]) == _without_ids(expected_points)
    assert (mock["total"], mock["available"], mock["next"]) == (245, 245, 245)
