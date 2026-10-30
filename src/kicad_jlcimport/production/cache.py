"""A small thread-safe memoizing cache for per-part network lookups."""

import threading
from typing import Callable, Dict, Generic, TypeVar

T = TypeVar("T")


class KeyedCache(Generic[T]):
    """Memoize ``fetcher(key)``.  Exceptions are not cached, so failures retry."""

    def __init__(self, fetcher: Callable[[str], T]):
        self._fetcher = fetcher
        self._cache: Dict[str, T] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> T:
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        value = self._fetcher(key)
        with self._lock:
            self._cache[key] = value
        return value

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def discard_where(self, predicate: Callable[[T], bool]) -> None:
        """Drop cached values for which *predicate* is true, so they are fetched again."""
        with self._lock:
            for key in [k for k, v in self._cache.items() if predicate(v)]:
                del self._cache[key]
