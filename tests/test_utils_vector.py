"""Unit tests for services/utils/vector.py — pure deterministic cosine math."""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.utils.vector import cosine_similarity  # noqa: E402


def test_identical_vectors_return_one():
    assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0


def test_orthogonal_vectors_return_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_opposite_vectors_return_minus_one():
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == -1.0


def test_known_60_degree_angle_returns_half():
    a, b = [1.0, 0.0], [0.5, math.sqrt(3) / 2]
    assert math.isclose(cosine_similarity(a, b), 0.5, abs_tol=1e-9)


def test_magnitude_invariance():
    a = cosine_similarity([1.0, 2.0, 3.0], [2.0, 4.0, 6.0])
    assert math.isclose(a, 1.0, abs_tol=1e-9)


def test_none_inputs_return_zero():
    assert cosine_similarity(None, [1.0]) == 0.0
    assert cosine_similarity([1.0], None) == 0.0
    assert cosine_similarity(None, None) == 0.0


def test_empty_inputs_return_zero():
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([1.0], []) == 0.0
    assert cosine_similarity([], []) == 0.0


def test_mismatched_lengths_return_zero():
    assert cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


def test_zero_magnitude_returns_zero():
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine_similarity([1.0, 1.0], [0.0, 0.0]) == 0.0
