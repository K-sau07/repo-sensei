"""The browse caches keep to their memory budgets, least recently used out first."""

from __future__ import annotations

import uuid
from typing import Any, cast

from sensei_api.browse import CONSTELLATION_BYTES_PER_POINT, MB, ConstellationCache
from sensei_engine.ingest.constellation import Constellation


def _sky(points: int) -> Constellation:
    # Only the number of points matters to the budget.
    return Constellation("g", [], cast("list[Any]", [None] * points))


async def test_the_constellation_cache_keeps_to_its_budget() -> None:
    cache = ConstellationCache(max_mb=1)
    fits = MB // CONSTELLATION_BYTES_PER_POINT
    first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    loads: list[uuid.UUID] = []

    def loader(snapshot: uuid.UUID, points: int):
        async def load() -> Constellation:
            loads.append(snapshot)
            return _sky(points)

        return load

    await cache.get(first, "g", loader(first, fits // 2))
    await cache.get(second, "g", loader(second, fits // 2))
    await cache.get(first, "g", loader(first, fits // 2))  # still there: not loaded again
    assert loads == [first, second]
    # A third pushes the total over the budget: the least recently used (second) goes.
    await cache.get(third, "g", loader(third, fits // 2))
    await cache.get(first, "g", loader(first, fits // 2))
    await cache.get(second, "g", loader(second, fits // 2))
    assert loads == [first, second, third, second]
    # One sky bigger than the whole budget is still served (and kept alone).
    huge = uuid.uuid4()
    assert len((await cache.get(huge, "g", loader(huge, fits * 3))).points) == fits * 3
