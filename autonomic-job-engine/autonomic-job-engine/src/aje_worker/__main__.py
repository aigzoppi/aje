"""Runs a handler as a worker: ``python -m aje_worker package.module:function``."""

# pylint: disable=invalid-name  # Python requires this module name.

import importlib
import sys

import aje_worker


def main() -> None:
    if len(sys.argv) != 2 or ":" not in sys.argv[1]:
        sys.exit("usage: python -m aje_worker module:function")
    module_name, func_name = sys.argv[1].split(":", 1)
    handler = getattr(importlib.import_module(module_name), func_name)
    aje_worker.run(handler)


if __name__ == "__main__":
    main()
