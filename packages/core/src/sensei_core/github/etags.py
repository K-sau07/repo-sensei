"""Redis-backed ETag cache for :class:`~sensei_core.github.client.GitHubClient`.

Bodies can hold private repository data, so they are stored encrypted, with the cache key bound as
associated data. Entries expire after a day; an unreadable entry is treated as a miss.
"""

from __future__ import annotations

import base64
import json
from typing import Any, cast

from redis.asyncio import Redis

from sensei_core.security.crypto import DecryptionError, TokenCipher

ETAG_TTL_SECONDS = 86_400
_KEY = "github:etag:{}"


class RedisETagCache:
    def __init__(self, redis: Redis, cipher: TokenCipher, *, ttl: int = ETAG_TTL_SECONDS) -> None:
        self._redis = redis
        self._cipher = cipher
        self._ttl = ttl

    async def get(self, key: str) -> tuple[str, Any] | None:
        redis_key = _KEY.format(key)
        raw = cast("str | None", await self._redis.get(redis_key))
        if raw is None:
            return None
        try:
            plain = self._cipher.decrypt_text(base64.b64decode(raw), context=redis_key)
            etag, body = json.loads(plain)
        except (DecryptionError, ValueError, TypeError):
            await self._redis.delete(redis_key)
            return None
        return str(etag), body

    async def set(self, key: str, etag: str, body: Any) -> None:
        redis_key = _KEY.format(key)
        sealed = self._cipher.encrypt_text(json.dumps([etag, body]), context=redis_key)
        await self._redis.set(redis_key, base64.b64encode(sealed).decode(), ex=self._ttl)
