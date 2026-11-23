"""Fractal example worker: tiling and pixel computation."""

import base64
import struct

import pytest

from aje_worker.examples import fractal

MANDELBROT = {
    "kind": "mandelbrot",
    "view": [-2.0, -1.5, 1.0, 1.5],
    "image": [8, 8],
    "tile": [0, 0, 8, 8],
    "max_iter": 50,
}


def render(**overrides) -> tuple[dict, tuple[int, ...]]:
    """Compute a tile (MANDELBROT plus overrides); return the result and its decoded iteration counts."""
    result = fractal.compute_tile({**MANDELBROT, **overrides})
    raw = base64.b64decode(result["data"])
    return result, struct.unpack(f"<{len(raw) // 2}H", raw)


class SplitImageTest:
    def test_covers_every_pixel_once(self):
        tiles = fractal.split_image(100, 70, 32)
        assert sum(w * h for _, _, w, h in tiles) == 100 * 70
        assert tiles[0] == [0, 0, 32, 32] and tiles[-1] == [96, 64, 4, 6]


class ComputeTileTest:
    def test_mandelbrot_values(self):
        result, values = render()
        assert result["tile"] == [0, 0, 8, 8] and len(values) == 64
        assert max(values) == 50  # some pixels are inside the set
        assert min(values) < 5  # the corners escape fast

    def test_tiles_are_consistent_with_the_whole_image(self):
        _, whole = render()
        pixels = {}
        for tile in fractal.split_image(8, 8, 4):
            _, values = render(tile=tile)
            for i, v in enumerate(values):
                pixels[(tile[0] + i % tile[2], tile[1] + i // tile[2])] = v
        assert [pixels[(i % 8, i // 8)] for i in range(64)] == list(whole)

    def test_julia_uses_the_constant(self):
        julia = {"kind": "julia", "view": [-1.5, -1.5, 1.5, 1.5]}
        assert render(**julia, c=[-0.8, 0.156])[1] != render(**julia, c=[0.285, 0.01])[1]

    @pytest.mark.parametrize(
        "bad", [{"kind": "sierpinski"}, {"tile": [0, 0, 0, 8]}, {"tile": [0, 0, 1000, 1000]}]
    )
    def test_invalid_payloads(self, bad):
        with pytest.raises(ValueError):
            fractal.compute_tile({**MANDELBROT, **bad})
