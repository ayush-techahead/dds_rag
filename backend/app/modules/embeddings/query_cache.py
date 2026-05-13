"""In-process LRU cache for single-string embedding lookups.

Designed for the chat/voice path where the same user query (the latest message or
a tool-call search string) often gets embedded multiple times within seconds —
e.g. the SPA retries a request, or two concurrent voice turns search the same
phrase. The cache is keyed on ``(model, normalised_text)`` and survives only for
the lifetime of the process; multi-instance deployments will see a per-pod hit
rate, which is the cheap-but-useful tier.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Final

_DEFAULT_MAX_ENTRIES: Final[int] = 512


class QueryEmbeddingCache:
    """Async-safe LRU for query embeddings."""

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._max = max(1, int(max_entries))
        self._store: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _normalise(text: str) -> str:
        return " ".join((text or "").strip().lower().split())

    async def get_or_compute(
        self,
        model: str,
        text: str,
        compute: Callable[[], Awaitable[list[float]]],
    ) -> tuple[list[float], bool]:
        """Return cached vector if present, else call ``compute`` and cache the result.

        Returns ``(vector, cache_hit)``.
        """
        key = (model, self._normalise(text))
        async with self._lock:
            cached = self._store.get(key)
            if cached is not None:
                self._store.move_to_end(key)
                self._hits += 1
                return list(cached), True

        # Compute outside the lock so concurrent misses overlap on the network
        # call instead of serialising. The duplicated work for a true thundering
        # herd is bounded and dwarfed by the latency gain in the common case.
        vector = await compute()

        async with self._lock:
            self._store[key] = list(vector)
            self._store.move_to_end(key)
            if len(self._store) > self._max:
                self._store.popitem(last=False)
            self._misses += 1
        return vector, False

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            return {
                "size": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "max_entries": self._max,
            }

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0


_singleton: QueryEmbeddingCache | None = None


def get_query_embedding_cache() -> QueryEmbeddingCache:
    """Module-level singleton; shared across requests in the same process."""
    global _singleton
    if _singleton is None:
        _singleton = QueryEmbeddingCache()
    return _singleton
