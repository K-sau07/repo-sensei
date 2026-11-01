"""Write the web mocks' semantic galaxy (apps/web/src/mocks/fixtures/galaxy.json).

The mock has no summaries or embeddings, so a file's meaning here is its content's TF-IDF
(over the files whose content the mock serves: fixtures/browse-content.json), reduced to 64
dimensions, and its terms are its most particular words; the layout and clusters are then the
engine's (:func:`sensei_engine.galaxy.layout_galaxy`), as ``GET …/galaxy?layout=semantic``
sends them, each star with its layer and language from the mock's constellation. Re-run after
changing the galaxy's layout::

    uv run python scripts/mock_galaxy.py
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import orjson

from sensei_engine.galaxy import layout_galaxy

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "apps" / "web" / "src" / "mocks" / "fixtures"
CONTENT = FIXTURES / "browse-content.json"
CONSTELLATION = FIXTURES / "constellation.json"
OUT = FIXTURES / "galaxy.json"
DIMS = 64
TERMS = 4
WORD = re.compile(r"[A-Za-z][a-z]{3,}")
NOTE = (
    "The mock's semantic galaxy (scripts/mock_galaxy.py): its files placed by their content's "
    "TF-IDF (the mock has no summary embeddings) with the engine's layout and clusters."
)


def build() -> dict[str, Any]:
    from sklearn.decomposition import TruncatedSVD  # noqa: PLC0415
    from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: PLC0415

    content: dict[str, str] = orjson.loads(CONTENT.read_bytes())["files"]
    paths = sorted(p for p, text in content.items() if len(WORD.findall(text)) >= 5)
    texts = [" ".join(w.lower() for w in WORD.findall(content[p])) for p in paths]
    tfidf: Any = TfidfVectorizer(min_df=2, max_df=0.5, sublinear_tf=True)
    sparse: Any = tfidf.fit_transform(texts)
    weights = np.asarray(sparse.toarray(), dtype=np.float64)
    words = [str(w) for w in tfidf.get_feature_names_out()]
    dims = min(DIMS, weights.shape[1] - 1, len(paths) - 1)
    svd: Any = TruncatedSVD(n_components=dims, random_state=7)
    reduced = np.asarray(svd.fit_transform(sparse), dtype=np.float64)
    vectors: dict[str, np.ndarray] = {}
    for i, path in enumerate(paths):
        row: np.ndarray = reduced[i]
        vectors[path] = row / (float(np.linalg.norm(row)) or 1.0)
    terms: dict[str, list[str]] = {}
    for i, path in enumerate(paths):
        row = weights[i]
        top = [int(j) for j in row.argsort()[::-1][:TERMS]]
        terms[path] = [words[j] for j in top if row[j] > 0]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # UMAP's: n_jobs with a seed, spectral init on few points
        galaxy = layout_galaxy(vectors, terms)
    files = {p["path"]: p for p in orjson.loads(CONSTELLATION.read_bytes())["points"]}
    return {
        "note": NOTE,
        "layout": "semantic",
        "stars": [
            {
                "path": s.path,
                "x": round(s.x, 5),
                "y": round(s.y, 5),
                "z": round(s.z, 5),
                "cluster": s.cluster,
                "layer": files.get(s.path, {}).get("layer", "other"),
                "language": files.get(s.path, {}).get("language"),
            }
            for s in galaxy.stars
        ],
        "clusters": [
            {
                "id": c.id,
                "label": c.label,
                "x": round(c.x, 5),
                "y": round(c.y, 5),
                "z": round(c.z, 5),
                "files": c.files,
            }
            for c in galaxy.clusters
        ],
    }


def main() -> None:
    OUT.write_bytes(orjson.dumps(build(), option=orjson.OPT_INDENT_2) + b"\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
