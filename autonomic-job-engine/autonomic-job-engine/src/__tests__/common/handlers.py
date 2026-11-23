"""Worker handlers used by in-process workers (``worker.command[0]`` is the key)."""

import asyncio
import random

from aje_worker.examples import datastore
from aje_worker.examples import fractal


async def square(x):
    await asyncio.sleep(0.02)
    return x * x


async def slow_square(x):
    await asyncio.sleep(0.15)
    return x * x


async def sum_squares(items):
    await asyncio.sleep(0.01 * len(items))
    return sum(v * v for v in items)


async def upper(items):
    await asyncio.sleep(0.005 * len(items))
    return [s.upper() for s in items]


async def word_count(lines):
    out = {}
    for line in lines:
        for w in line.split():
            out[w] = out.get(w, 0) + 1
    return out


_flaky_state = {}


async def flaky(x):
    """Fails the first attempt of every odd payload."""
    await asyncio.sleep(0.01)
    if x % 2 and not _flaky_state.get(x):
        _flaky_state[x] = True
        raise RuntimeError("transient")
    return x


async def always_fail(x):
    raise ValueError(f"bad input {x}")


async def random_sleep_square(x):
    await asyncio.sleep(random.uniform(0, 0.02))
    return x * x


HANDLERS = {
    "square": square,
    "slow_square": slow_square,
    "sum_squares": sum_squares,
    "upper": upper,
    "word_count": word_count,
    "flaky": flaky,
    "always_fail": always_fail,
    "random_sleep_square": random_sleep_square,
    "fractal": fractal.tile,
    "power_rows": datastore.power_rows,
}
