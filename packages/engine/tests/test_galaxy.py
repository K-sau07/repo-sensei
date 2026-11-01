"""The Codebase Galaxy's semantic layout (6.9): files about the same thing near each other, the
same each time, in clusters named by their own terms."""

from __future__ import annotations

import math

import numpy as np

from sensei_engine.galaxy import (
    FORMAT,
    Galaxy,
    cluster_count,
    cluster_labels,
    file_vectors,
    layout_galaxy,
    semantic_positions,
)

DIMS = 32


def _group(prefix: str, axis: int, n: int, rng: np.random.Generator) -> dict[str, np.ndarray]:
    """``n`` files whose meanings point mostly along ``axis``."""
    out: dict[str, np.ndarray] = {}
    for i in range(n):
        v = rng.normal(0, 0.15, DIMS)
        v[axis] += 1.0
        out[f"{prefix}/file{i:02d}.py"] = v / np.linalg.norm(v)
    return out


def _two_topics() -> tuple[dict[str, np.ndarray], dict[str, list[str]]]:
    rng = np.random.default_rng(3)
    vectors = {**_group("billing", 0, 30, rng), **_group("auth", 1, 30, rng)}
    billing, auth = ["invoice", "payment", "user"], ["login", "token", "user"]
    terms = {path: billing if path.startswith("billing") else auth for path in vectors}
    return vectors, terms


def test_a_files_meaning_is_the_mean_of_its_summaries() -> None:
    found = file_vectors([("a.py", [1.0, 0.0]), ("a.py", [0.0, 2.0]), ("b.py", [0.0, 0.0])])
    assert set(found) == {"a.py"}  # a zero vector says nothing
    assert np.allclose(found["a.py"], [math.sqrt(0.5), math.sqrt(0.5)])


def test_files_about_one_thing_are_near_each_other_the_same_each_time() -> None:
    vectors, _ = _two_topics()
    placed = semantic_positions(vectors)
    again = semantic_positions(vectors)
    assert placed == again
    assert all(-1 - 1e-9 <= c <= 1 + 1e-9 for p in placed.values() for c in p)

    def mean_distance(a: str, b: str) -> float:
        pa = [placed[p] for p in placed if p.startswith(a)]
        pb = [placed[p] for p in placed if p.startswith(b)]
        return float(np.mean([math.dist(x, y) for x in pa for y in pb if x is not y]))

    assert mean_distance("billing", "billing") < mean_distance("billing", "auth") / 2
    assert mean_distance("auth", "auth") < mean_distance("billing", "auth") / 2


def test_the_galaxy_is_clustered_and_each_cluster_named_by_its_own_terms() -> None:
    vectors, terms = _two_topics()
    galaxy = layout_galaxy(vectors, terms)
    assert len(galaxy.stars) == 60
    assert len(galaxy.clusters) == cluster_count(60) == 3
    by_path = {s.path: s.cluster for s in galaxy.stars}
    # No cluster mixes the two topics.
    for cluster in galaxy.clusters:
        topics = {p.split("/")[0] for p, c in by_path.items() if c == cluster.id}
        assert len(topics) == 1
        assert cluster.files == sum(1 for c in by_path.values() if c == cluster.id)
        named = set(cluster.label.split(", "))
        assert "user" not in named  # said everywhere: names nothing
        assert named <= {"invoice", "payment", "login", "token"}
    # Biggest first.
    assert [c.files for c in galaxy.clusters] == sorted(
        (c.files for c in galaxy.clusters), reverse=True
    )
    # Kept and read back; one kept by other code is made again.
    assert Galaxy.from_json(galaxy.as_json()) == galaxy
    assert Galaxy.from_json({**galaxy.as_json(), "format": FORMAT + 1}) is None
    assert Galaxy.from_json({"format": FORMAT, "stars": [["x"]], "clusters": []}) is None


def test_a_cluster_without_terms_is_named_by_its_folder() -> None:
    names = cluster_labels({0: ["web/src/a.ts", "web/src/b.ts", "api/c.py"]}, {})
    assert names == {0: "web/src"}


def test_a_handful_of_files_sit_around_a_circle() -> None:
    placed = semantic_positions({"a.py": np.ones(4), "b.py": np.ones(4)})
    assert placed["a.py"] == (1.0, 0.0, 0.0)
    assert math.isclose(placed["b.py"][0], -1.0)
    assert semantic_positions({}) == {}
