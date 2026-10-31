"""向量语义层测试（不联网：只测数学与聚类，不测 Ollama）。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from todoseine.core.vector import cosine, cluster, best_match  # noqa: E402


class TestCosine(unittest.TestCase):
    def test_identical_is_one(self) -> None:
        v = [0.1, 0.2, 0.3]
        self.assertAlmostEqual(cosine(v, v), 1.0, places=6)

    def test_orthogonal_is_zero(self) -> None:
        self.assertAlmostEqual(cosine([1, 0], [0, 1]), 0.0, places=6)

    def test_opposite_is_minus_one(self) -> None:
        self.assertAlmostEqual(cosine([1, 0], [-1, 0]), -1.0, places=6)

    def test_empty_or_mismatched_returns_zero(self) -> None:
        self.assertEqual(cosine([], [1.0]), 0.0)
        self.assertEqual(cosine([1.0, 2.0], [1.0]), 0.0)


class TestCluster(unittest.TestCase):
    def test_near_duplicates_merge(self) -> None:
        items = [
            ("改天把材料发我", [1.0, 0.0, 0.0]),
            ("材料改天发我一下", [0.99, 0.01, 0.0]),
            ("今晚吃什么", [0.0, 1.0, 0.0]),
        ]
        groups = cluster(items, threshold=0.9)
        self.assertEqual(len(groups), 2)
        self.assertEqual(sorted(groups[0]), [0, 1])

    def test_all_distinct(self) -> None:
        items = [
            ("a", [1.0, 0.0]),
            ("b", [0.0, 1.0]),
        ]
        self.assertEqual(len(cluster(items, 0.9)), 2)

    def test_empty(self) -> None:
        self.assertEqual(cluster([], 0.9), [])


class TestBestMatch(unittest.TestCase):
    def test_picks_max(self) -> None:
        seeds = [[1.0, 0.0], [0.0, 1.0]]
        self.assertAlmostEqual(best_match([0.9, 0.1], seeds), 0.9938837, places=5)

    def test_no_seeds(self) -> None:
        self.assertEqual(best_match([1.0], []), 0.0)


if __name__ == "__main__":
    unittest.main()
