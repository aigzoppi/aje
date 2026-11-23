"""Mandelbrot and Julia sets, one image tile per task (farm pattern).

    python -m aje_worker aje_worker.examples.fractal:tile

Payload (one tile of a larger image):
    {
      "kind": "mandelbrot" | "julia",
      "c": [re, im],                    # julia only: the constant of z -> z^2 + c
      "view": [x_min, y_min, x_max, y_max],   # region of the complex plane of the WHOLE image
      "image": [width, height],          # whole image size in pixels
      "tile": [x, y, w, h],              # this tile, in pixels
      "max_iter": 256
    }

Result:
    {"tile": [x, y, w, h], "max_iter": n, "data": "<base64 of w*h little-endian uint16>"}
    where each value is the escape iteration (max_iter = inside the set). Base64 keeps a
    64x64 tile at ~11 KB of JSON instead of ~20 KB as a list of numbers.

Pure Python on purpose: tiles take long enough (tenths of a second) for autoscaling to
be visible, and the worker image needs no numpy.
"""

from __future__ import annotations

import base64
import os
import struct
import time

_DELAY = float(os.environ.get("AJE_FRACTAL_DELAY", "0"))  # extra seconds per tile, to slow the demo down

MAX_PIXELS = 256 * 256
MAX_ITER = 5000


def _escape(zr: float, zi: float, cr: float, ci: float, max_iter: int) -> int:
    for n in range(max_iter):
        zr2, zi2 = zr * zr, zi * zi
        if zr2 + zi2 > 4.0:
            return n
        zi = 2.0 * zr * zi + ci
        zr = zr2 - zi2 + cr
    return max_iter


def compute_tile(payload: dict) -> dict:
    """Computes the escape-time iterations of one tile of a Mandelbrot or Julia image.

    Args:
        payload: ``kind``, ``view`` [x0, y0, x1, y1], ``image`` [w, h], ``tile`` [x, y, w, h],
            ``max_iter`` and, for Julia sets, ``c`` [re, im].

    Returns:
        The tile and its iteration counts (little-endian uint16, base64).

    Raises:
        ValueError: Unknown kind or a tile outside the image.
    """
    kind = payload.get("kind", "mandelbrot")
    if kind not in ("mandelbrot", "julia"):
        raise ValueError(f"unknown fractal kind {kind!r}")
    x_min, y_min, x_max, y_max = (float(v) for v in payload["view"])
    width, height = (int(v) for v in payload["image"])
    tx, ty, tw, th = (int(v) for v in payload["tile"])
    max_iter = max(1, min(int(payload.get("max_iter", 256)), MAX_ITER))
    if tw * th > MAX_PIXELS or tw <= 0 or th <= 0:
        raise ValueError(f"tile must have between 1 and {MAX_PIXELS} pixels")
    jr, ji = (float(v) for v in payload.get("c", (-0.8, 0.156)))

    dx = (x_max - x_min) / width
    dy = (y_max - y_min) / height
    values = []
    for py in range(ty, ty + th):
        im = y_max - (py + 0.5) * dy  # row 0 is the top of the image
        for px in range(tx, tx + tw):
            re = x_min + (px + 0.5) * dx
            if kind == "mandelbrot":
                values.append(_escape(0.0, 0.0, re, im, max_iter))
            else:
                values.append(_escape(re, im, jr, ji, max_iter))
    if _DELAY:
        time.sleep(_DELAY)
    data = base64.b64encode(struct.pack(f"<{len(values)}H", *values)).decode("ascii")
    return {"tile": [tx, ty, tw, th], "max_iter": max_iter, "data": data}


def tile(payload: dict) -> dict:
    """Worker handler."""
    return compute_tile(payload)


def split_image(width: int, height: int, tile_size: int) -> list[list[int]]:
    """Tiles [x, y, w, h] covering the image, row by row (handy for clients and tests)."""
    return [
        [x, y, min(tile_size, width - x), min(tile_size, height - y)]
        for y in range(0, height, tile_size)
        for x in range(0, width, tile_size)
    ]
