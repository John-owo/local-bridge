"""A small LRU cache with hit/miss statistics and optional per-entry TTL."""
import time


class LRUCache:
    """Least-recently-used cache.

    Contract:
    - get(key) returns the value or None; a hit makes the key most recently used.
    - put(key, value, ttl=None) inserts or updates; updating an existing key never evicts another key
      and makes it most recently used.
    - When inserting a new key would exceed capacity, the least recently used key is evicted first.
    - capacity == 0 means nothing is ever stored.
    - Entries with a ttl expire ttl seconds after their last put; expired entries count as misses
      and are removed when seen.
    - stats() returns {"hits": int, "misses": int, "size": int}.
    """

    def __init__(self, capacity, clock=time.monotonic):
        self.capacity = capacity
        self.clock = clock
        self._data = {}
        self._order = []
        self.hits = 0
        self.misses = 0

    def get(self, key):
        if key not in self._data:
            self.misses += 1
            return None
        value, expires = self._data[key]
        if expires is not None and self.clock() > expires:
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, key, value, ttl=None):
        expires = self.clock() + ttl if ttl else None
        if len(self._data) >= self.capacity:
            oldest = self._order.pop()
            del self._data[oldest]
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        self._data[key] = (value, expires)

    def stats(self):
        return {"hits": self.hits, "misses": self.misses, "size": len(self._data)}
