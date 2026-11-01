"""The Codebase Galaxy's semantic layout (6.9): each file a star in 3D, files about the same
thing near each other.

A file's place comes from what its summaries mean: the mean of its summary chunks' embeddings
(2.7, 2.8), laid out in three dimensions by UMAP (cosine, seeded: the same each time), then
scaled into [-1, 1]³. The stars are grouped into clusters (k-means on those places, about one
per twenty files, 2 to 12), each named by the glossary terms (2.8) most particular to its files:
said often in it, seldom elsewhere.

UMAP is imported when a layout is made (its import compiles code: seconds), in the worker that
makes it (:mod:`sensei_workers.tasks.galaxy`); the API only reads the result.
"""

from __future__ import annotations

import math
import warnings
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import numpy as np

FORMAT = 1  # bump when the layout changes: a kept one is made again
SEED = 7
MIN_CLUSTERS = 2
MAX_CLUSTERS = 12
FILES_PER_CLUSTER = 20
LABEL_TERMS = 3
PARTICULAR = 1.5  # how much likelier inside a cluster a term must be to name it

Vector = Sequence[float]


@dataclass(frozen=True, slots=True)
class Star:
    path: str
    x: float
    y: float
    z: float
    cluster: int


@dataclass(frozen=True, slots=True)
class Cluster:
    id: int
    label: str
    x: float
    y: float
    z: float
    files: int


@dataclass(frozen=True, slots=True)
class Galaxy:
    stars: list[Star]
    clusters: list[Cluster]

    def as_json(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "stars": [[s.path, s.x, s.y, s.z, s.cluster] for s in self.stars],
            "clusters": [[c.id, c.label, c.x, c.y, c.z, c.files] for c in self.clusters],
        }

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> Galaxy | None:
        """The kept layout, or None for one made by other code (or unreadable)."""
        if data.get("format") != FORMAT:
            return None
        try:
            stars = [
                Star(str(p), float(x), float(y), float(z), int(c))
                for p, x, y, z, c in data["stars"]
            ]
            clusters = [
                Cluster(int(i), str(label), float(x), float(y), float(z), int(n))
                for i, label, x, y, z, n in data["clusters"]
            ]
        except (KeyError, TypeError, ValueError):
            return None
        return Galaxy(stars, clusters)


def file_vectors(rows: Sequence[tuple[str, Vector]]) -> dict[str, np.ndarray]:
    """Each file's meaning: the mean of its summary chunks' (unit) embeddings, made unit again."""
    sums: dict[str, np.ndarray] = {}
    for path, vector in rows:
        v = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(v))
        if norm == 0:
            continue
        sums[path] = sums.get(path, np.zeros_like(v)) + v / norm
    out: dict[str, np.ndarray] = {}
    for path, total in sums.items():
        norm = float(np.linalg.norm(total))
        if norm > 0:
            out[path] = total / norm
    return out


def _scaled(points: np.ndarray) -> np.ndarray:
    """Centered and scaled into [-1, 1]³, its proportions kept."""
    low, high = points.min(axis=0), points.max(axis=0)
    span = float((high - low).max()) or 1.0
    return (points - (low + high) / 2) * (2 / span)


def semantic_positions(
    vectors: Mapping[str, np.ndarray], *, seed: int = SEED
) -> dict[str, tuple[float, float, float]]:
    """The files' places in 3D (UMAP of their meanings); a handful of files on a small circle."""
    paths = sorted(vectors)
    if not paths:
        return {}
    if len(paths) < 5:  # too few to lay out: evenly around
        return {
            p: (math.cos(i * 2 * math.pi / len(paths)), math.sin(i * 2 * math.pi / len(paths)), 0.0)
            for i, p in enumerate(paths)
        }
    with warnings.catch_warnings():  # "TensorFlow not installed": its parametric UMAP isn't used
        warnings.simplefilter("ignore", ImportWarning)
        import umap  # noqa: PLC0415 - slow to import: only where a layout is made

    matrix = np.stack([vectors[p] for p in paths])
    reducer = umap.UMAP(
        n_components=3,
        n_neighbors=min(15, len(paths) - 1),
        min_dist=0.1,
        metric="cosine",
        random_state=seed,
        init="random" if len(paths) < 50 else "spectral",
        n_jobs=1,  # seeded: the same each time
    )
    fit = cast("Callable[[np.ndarray], np.ndarray]", reducer.fit_transform)  # pyright: ignore[reportUnknownMemberType]
    placed = _scaled(np.asarray(fit(matrix), dtype=np.float64))
    return {p: (float(x), float(y), float(z)) for p, (x, y, z) in zip(paths, placed, strict=True)}


def cluster_count(files: int) -> int:
    return max(MIN_CLUSTERS, min(MAX_CLUSTERS, round(files / FILES_PER_CLUSTER)))


def clusters_of(
    positions: Mapping[str, tuple[float, float, float]], *, seed: int = SEED
) -> dict[str, int]:
    """Each file's cluster: k-means on the places (k from the count), numbered biggest first."""
    paths = sorted(positions)
    if len(paths) <= MIN_CLUSTERS:
        return dict.fromkeys(paths, 0)
    from sklearn.cluster import KMeans  # noqa: PLC0415 - with UMAP, where a layout is made

    matrix = np.array([positions[p] for p in paths])
    k = min(cluster_count(len(paths)), len(paths))
    means = KMeans(n_clusters=k, n_init=4, random_state=seed)  # pyright: ignore[reportArgumentType]
    predict = cast("Callable[[np.ndarray], np.ndarray]", means.fit_predict)  # pyright: ignore[reportUnknownMemberType]
    labels = [int(label) for label in predict(matrix)]
    sizes = Counter(labels)
    order = {
        label: i for i, (label, _) in enumerate(sorted(sizes.items(), key=lambda x: (-x[1], x[0])))
    }
    return {p: order[label] for p, label in zip(paths, labels, strict=True)}


def cluster_labels(
    members: Mapping[int, Sequence[str]],
    terms: Mapping[str, Sequence[str]],
    *,
    top: int = LABEL_TERMS,
) -> dict[int, str]:
    """Each cluster's name: the glossary terms most particular to its files (said in many of its
    files and few others: the share inside over the share outside, at least :data:`PARTICULAR`),
    else its files' commonest folder."""
    total = sum(len(paths) for paths in members.values()) or 1
    overall: Counter[str] = Counter()
    for paths in members.values():
        for path in paths:
            overall.update(set(terms.get(path, ())))
    names: dict[int, str] = {}
    for cluster, paths in members.items():
        inside: Counter[str] = Counter()
        for path in paths:
            inside.update(set(terms.get(path, ())))
        size = len(paths) or 1
        scored: list[tuple[float, str]] = []
        for term, n in inside.items():
            if n < 2 and size >= 4:
                continue  # one file's word
            particular = (n / size) / ((overall[term] - n + 1) / (total - size + 1))
            if particular >= PARTICULAR:
                scored.append((-particular, term))
        scored.sort()
        picked = [term for _, term in scored[:top]]
        if picked:
            names[cluster] = ", ".join(picked)
        else:
            folders = Counter(p.rsplit("/", 1)[0] if "/" in p else "." for p in paths)
            names[cluster] = folders.most_common(1)[0][0] if folders else f"cluster {cluster + 1}"
    return names


def layout_galaxy(
    vectors: Mapping[str, np.ndarray],
    terms: Mapping[str, Sequence[str]],
    *,
    seed: int = SEED,
) -> Galaxy:
    """The semantic galaxy of the files with a meaning (see the module)."""
    positions = semantic_positions(vectors, seed=seed)
    clusters = clusters_of(positions, seed=seed)
    members: dict[int, list[str]] = {}
    for path, cluster in sorted(clusters.items()):
        members.setdefault(cluster, []).append(path)
    names = cluster_labels(members, terms)
    stars = [Star(p, *positions[p], clusters[p]) for p in sorted(positions)]
    out: list[Cluster] = []
    for cluster, paths in sorted(members.items()):
        at = np.array([positions[p] for p in paths]).mean(axis=0)
        out.append(
            Cluster(cluster, names[cluster], float(at[0]), float(at[1]), float(at[2]), len(paths))
        )
    return Galaxy(stars, out)
