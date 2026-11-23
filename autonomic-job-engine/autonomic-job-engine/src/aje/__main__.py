"""Runs the API server: ``python -m aje``."""

# pylint: disable=invalid-name  # Python requires this module name.

import uvicorn

from aje import config


def main() -> None:
    s = config.get_settings()
    uvicorn.run("aje.main:app", host="0.0.0.0", port=8000, log_level=s.log_level.lower())


if __name__ == "__main__":
    main()
