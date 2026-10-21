"""Write the web mocks' constellation (apps/web/src/mocks/fixtures/constellation.json).

The mock index run replays a real run of fastapi/full-stack-fastapi-template at b1125d8
(fixtures/index-run.json); this lays out the same commit's files, from the engine's fixture of
that commit, exactly as discovery does, in the shape ``GET …/constellation`` returns. File ids
are made up (stable per path). An engine test checks the file still matches the layout, so
re-run this after changing it::

    uv run python scripts/mock_constellation.py
"""

from __future__ import annotations

import tarfile
import tempfile
import uuid
from pathlib import Path

import orjson

from sensei_api.routers.constellation import ConstellationOut, dirs_out, points_out
from sensei_engine.ingest.constellation import DirectoryLayout
from sensei_engine.ingest.discover import discover

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "packages" / "engine" / "tests" / "fixtures" / "fastapi-template.tar.gz"
OUT = ROOT / "apps" / "web" / "src" / "mocks" / "fixtures" / "constellation.json"
COMMIT = "b1125d8839482cb7bbae603ddc6d0081bf87532e"
GENERATION = "fixture-b1125d8"
NOTE = (
    "The directory layout of fastapi/full-stack-fastapi-template at b1125d8 (the commit of "
    "index-run.json), from the engine's fixture of that commit by scripts/mock_constellation.py, "
    "as GET /constellation returns it. File ids are made up."
)


def mock_file_id(path: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"repo-sensei-mock:{path}")


def build() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as scratch:
        with tarfile.open(FIXTURE) as tar:
            tar.extractall(scratch, filter="data")
        found = discover(Path(scratch) / "fastapi-template", max_files=10_000)
    layout = DirectoryLayout(file.path for file in found.files)
    points = [
        layout.point(
            file_id=mock_file_id(file.path),
            path=file.path,
            role=file.role.value,
            language=file.language,
            size=file.size,
            loc=file.loc,
        )
        for file in found.files
    ]
    page = ConstellationOut(
        status="complete",
        commit_sha=COMMIT,
        generation=GENERATION,
        total=len(points),
        available=len(points),
        since=0,
        next=len(points),
        dirs=dirs_out(layout.dirs),
        points=points_out(points),
    )
    return {"note": NOTE, **page.model_dump(mode="json")}


def main() -> None:
    OUT.write_bytes(orjson.dumps(build(), option=orjson.OPT_INDENT_2) + b"\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
