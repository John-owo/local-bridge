import sys, unittest
sys.path.insert(0, sys.argv.pop() if len(sys.argv) > 1 else ".")
from interval import merge_intervals


class Hidden(unittest.TestCase):
    def test_unsorted(self):
        self.assertEqual(merge_intervals([[5, 6], [1, 3], [2, 4]]), [[1, 4], [5, 6]])

    def test_touching(self):
        self.assertEqual(merge_intervals([[1, 2], [2, 3]]), [[1, 3]])

    def test_contained(self):
        self.assertEqual(merge_intervals([[1, 10], [2, 3]]), [[1, 10]])

    def test_empty(self):
        self.assertEqual(merge_intervals([]), [])

    def test_does_not_mutate_input(self):
        data = [[1, 3], [2, 4]]
        merge_intervals(data)
        self.assertEqual(data, [[1, 3], [2, 4]])


if __name__ == "__main__":
    unittest.main()
