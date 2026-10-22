import sys, unittest
sys.path.insert(0, sys.argv.pop() if len(sys.argv) > 1 else ".")
from lru import LRUCache


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Hidden(unittest.TestCase):
    def test_evicts_least_recent(self):
        c = LRUCache(2)
        c.put("a", 1); c.put("b", 2); c.put("c", 3)
        self.assertIsNone(c.get("a"))
        self.assertEqual(c.get("b"), 2)

    def test_get_refreshes_recency(self):
        c = LRUCache(2)
        c.put("a", 1); c.put("b", 2); c.get("a"); c.put("c", 3)
        self.assertEqual(c.get("a"), 1)
        self.assertIsNone(c.get("b"))

    def test_update_does_not_evict(self):
        c = LRUCache(2)
        c.put("a", 1); c.put("b", 2); c.put("a", 10)
        self.assertEqual(c.get("b"), 2)
        self.assertEqual(c.get("a"), 10)

    def test_zero_capacity(self):
        c = LRUCache(0)
        c.put("a", 1)
        self.assertIsNone(c.get("a"))
        self.assertEqual(c.stats()["size"], 0)

    def test_expired_entry_removed(self):
        clock = Clock()
        c = LRUCache(2, clock=clock)
        c.put("a", 1, ttl=5)
        clock.t = 6
        self.assertIsNone(c.get("a"))
        self.assertEqual(c.stats(), {"hits": 0, "misses": 1, "size": 0})

    def test_ttl_zero_point_five(self):
        clock = Clock()
        c = LRUCache(2, clock=clock)
        c.put("a", 1, ttl=0.5)
        clock.t = 1
        self.assertIsNone(c.get("a"))


if __name__ == "__main__":
    unittest.main()
