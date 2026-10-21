"""The constellation's directory layout, its stored form, and its live copy in Redis."""

from __future__ import annotations

import math
import tarfile
import uuid
from pathlib import Path

import pytest
from fakeredis import FakeAsyncRedis

from sensei_engine.ingest.constellation import (
    LIVE_BATCH,
    LIVE_KEEP_SECONDS,
    SHELL_JITTER,
    Constellation,
    CorruptConstellationError,
    DirectoryLayout,
    LiveConstellation,
    Point,
    layer_for,
    live_keys,
    read_live,
    shell,
    squarify,
)
from sensei_engine.ingest.discover import discover

FIXTURE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"

PATHS = [
    "README.md",
    "pyproject.toml",
    "src/app.py",
    "src/api/routes.py",
    "src/api/users.py",
    "src/db/models.py",
    "src/db/migrations/0001_init.py",
    "web/src/App.tsx",
    "web/src/components/Button.tsx",
    "web/src/components/Card.tsx",
    "web/package.json",
]


def _points(layout: DirectoryLayout, paths: list[str]) -> list[Point]:
    return [
        layout.point(
            file_id=uuid.uuid5(uuid.NAMESPACE_URL, path),
            path=path,
            role="service",
            language="python",
            size=len(path),
            loc=1,
        )
        for path in paths
    ]


def test_the_same_paths_give_the_same_picture_in_any_order() -> None:
    shuffled = [*PATHS[5:], *reversed(PATHS[:5])]
    first, second = DirectoryLayout(PATHS), DirectoryLayout(shuffled)
    assert first.dirs == second.dirs
    assert _points(first, PATHS) == _points(second, PATHS)


def test_directories_are_listed_parents_first_in_name_order() -> None:
    layout = DirectoryLayout(PATHS)
    paths = [d.path for d in layout.dirs]
    assert paths[0] == ""
    assert set(paths) == {
        "",
        "src",
        "src/api",
        "src/db",
        "src/db/migrations",
        "web",
        "web/src",
        "web/src/components",
    }
    for index, directory in enumerate(layout.dirs):
        if directory.parent is None:
            assert directory.path == ""
            continue
        parent = layout.dirs[directory.parent]
        assert directory.parent < index
        assert directory.path.rsplit("/", 1)[0] == parent.path or parent.path == ""
        assert directory.depth == parent.depth + 1
    assert {d.path: d.files for d in layout.dirs}["src/api"] == 2
    assert layout.dirs[0].position == (0.0, 0.0, 0.0)  # the root sits at the centre


def test_files_sit_on_their_directory_shell_inside_the_unit_sphere() -> None:
    layout = DirectoryLayout(PATHS)
    for point in _points(layout, PATHS):
        depth = layout.dirs[point.directory].depth
        assert layout.dirs[point.directory].path == point.path.rpartition("/")[0]
        distance = math.dist((0, 0, 0), point.position)
        assert abs(distance - shell(depth)) <= SHELL_JITTER + 1e-3
        assert distance < 1
    # Deeper directories are further out.
    assert shell(0) < shell(1) < shell(2) < 1


def test_a_cluster_is_tighter_than_the_distance_between_clusters() -> None:
    paths = [f"{d}/f{i}.py" for d in ("a", "b", "c", "d") for i in range(30)]
    layout = DirectoryLayout(paths)
    points = _points(layout, paths)
    centre: dict[int, tuple[float, float, float]] = {}
    for directory in {p.directory for p in points}:
        members = [p.position for p in points if p.directory == directory]
        xs, ys, zs = zip(*members, strict=True)
        centre[directory] = (sum(xs) / len(xs), sum(ys) / len(ys), sum(zs) / len(zs))
    nearest_other = min(
        math.dist(a, b) for i, a in centre.items() for j, b in centre.items() if i != j
    )
    spread = max(math.dist(p.position, centre[p.directory]) for p in points)
    assert spread < nearest_other


def test_squarified_patches_tile_the_box_in_proportion() -> None:
    sizes = [40.0, 20.0, 10.0, 10.0, 5.0, 1.0]
    rects = squarify(sizes, 6.0, 4.0)
    areas = [(r.u1 - r.u0) * (r.v1 - r.v0) for r in rects]
    assert sum(areas) == pytest.approx(24.0)
    for area, size in zip(areas, sizes, strict=True):
        assert area == pytest.approx(24.0 * size / sum(sizes))
    for i, a in enumerate(rects):  # no two overlap
        for b in rects[i + 1 :]:
            overlap_u = min(a.u1, b.u1) - max(a.u0, b.u0)
            overlap_v = min(a.v1, b.v1) - max(a.v0, b.v0)
            assert overlap_u <= 1e-9 or overlap_v <= 1e-9


def test_layers_follow_roles() -> None:
    assert [layer_for(r) for r in ("ui", "api", "service", "db", "test", "docs", "x")] == [
        "ui",
        "api",
        "svc",
        "db",
        "other",
        "other",
        "other",
    ]


def test_stored_constellations_round_trip_and_refuse_damage() -> None:
    layout = DirectoryLayout(PATHS)
    found = Constellation.build("g1", layout, _points(layout, PATHS))
    back = Constellation.from_bytes(found.to_bytes())
    assert (back.generation, back.dirs, back.points) == ("g1", found.dirs, found.points)
    with pytest.raises(CorruptConstellationError):
        Constellation.from_bytes(b"not zstd")
    with pytest.raises(CorruptConstellationError):
        Constellation.from_bytes(found.to_bytes()[:-5])


@pytest.mark.slow
def test_the_fixture_repository_lays_out(tmp_path: Path) -> None:
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(tmp_path, filter="data")
    found = discover(tmp_path / "fastapi-template", max_files=10_000)
    layout = DirectoryLayout(file.path for file in found.files)
    assert len(layout) == 245
    assert sum(d.files for d in layout.dirs) == 245
    points = _points(layout, [file.path for file in found.files])
    assert len({p.position for p in points}) == 245  # no two files on one spot


async def test_live_points_page_by_cursor(fake_redis: FakeAsyncRedis) -> None:
    paths = [f"pkg/m{i:04}.py" for i in range(LIVE_BATCH * 2 + 17)]
    layout = DirectoryLayout(paths)
    points = _points(layout, paths)
    job = uuid.uuid4()
    assert await read_live(fake_redis, job, 0, 100) is None  # before the walk is done

    live = LiveConstellation(fake_redis, job, layout, "gen-1")
    await live.begin()
    empty = await read_live(fake_redis, job, 0, 100)
    assert empty is not None
    assert (empty.total, empty.available, empty.points, len(empty.dirs)) == (
        len(paths),
        0,
        [],
        len(layout.dirs),
    )

    assert await live.add(points[:300]) == 0  # a batch goes out once it is full
    assert await live.add(points[300 : LIVE_BATCH + 300]) == LIVE_BATCH
    assert await live.add(points[LIVE_BATCH + 300 :]) == LIVE_BATCH * 2
    assert await live.finish() == len(paths)

    page = await read_live(fake_redis, job, 0, 700)
    assert page is not None
    assert (page.generation, page.available, page.points) == ("gen-1", len(paths), points[:700])
    middle = await read_live(fake_redis, job, 700, 10_000)
    assert middle is not None
    assert (middle.points, middle.dirs) == (points[700:], [])  # directories on the first page only

    await live.done()
    head, batches = live_keys(job)
    assert 0 < await fake_redis.ttl(head) <= LIVE_KEEP_SECONDS
    assert 0 < await fake_redis.ttl(batches) <= LIVE_KEEP_SECONDS

    # Discovery starting over replaces it all, with a new generation.
    again = LiveConstellation(fake_redis, job, layout, "gen-2")
    await again.begin()
    restarted = await read_live(fake_redis, job, 0, 10)
    assert restarted is not None
    assert (restarted.generation, restarted.available) == ("gen-2", 0)
