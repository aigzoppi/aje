"""Data-parallel scatter (split) and gather."""

import pytest

from aje.application.runtime import data_parallel


class SplitTest:
    @pytest.mark.parametrize("parts", range(1, 8))
    def test_is_contiguous_complete_and_balanced(self, parts):
        for n in range(30):
            pieces = data_parallel.split(list(range(n)), parts)
            assert [x for piece in pieces for x in piece] == list(range(n))
            if n:
                assert max(map(len, pieces)) - min(map(len, pieces)) <= 1


class GatherTest:
    @pytest.mark.parametrize(
        "mode, partials, expected",
        [
            ("list", [1, [2]], [1, [2]]),
            ("concat", [[1], 2, [3, 4]], [1, 2, 3, 4]),
            ("merge", [{"a": 1, "s": "x"}, {"a": 2, "s": "y"}], {"a": 3, "s": "y"}),
        ],
    )
    def test_modes(self, mode, partials, expected):
        assert data_parallel.gather(mode, partials) == expected
