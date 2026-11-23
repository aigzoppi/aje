"""Example handlers; run one with ``python -m aje_worker <module>:<function>``.

Farm handlers (payload -> result):
  aje_worker.examples:square                  a number
  aje_worker.examples:flaky_square            a number (fails ~20% of the time)
  aje_worker.examples.fractal:tile            one Mandelbrot/Julia tile (see fractal.py)
  aje_worker.examples.datastore:power_rows    an id range; the data stays in PostgreSQL

Data-parallel handlers (one partition -> partial result, with the gather mode to use):
  aje_worker.examples:sum_squares             a list of numbers   (gather "sum")
  aje_worker.examples:word_count              a list of lines     (gather "merge")
  aje_worker.examples:upper                   a list of strings   (gather "concat")

AJE_EXAMPLE_DELAY (seconds, default 0.2) simulates work per item.
"""

from __future__ import annotations

import collections
import os
import random
import time

_DELAY = float(os.environ.get("AJE_EXAMPLE_DELAY", "0.2"))


def square(x):
    time.sleep(_DELAY)
    return x * x


def flaky_square(x):
    time.sleep(_DELAY)
    if random.random() < 0.2:
        raise RuntimeError("simulated transient failure")
    return x * x


def sum_squares(items):
    time.sleep(_DELAY * len(items))
    return sum(v * v for v in items)


def word_count(lines):
    time.sleep(_DELAY * len(lines))
    return dict(collections.Counter(w.lower() for line in lines for w in line.split()))


def upper(items):
    time.sleep(_DELAY * len(items))
    return [str(s).upper() for s in items]
