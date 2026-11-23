"""Polling for conditions that become true asynchronously."""

import asyncio
from collections.abc import Awaitable, Callable
import inspect
from typing import Any

Condition = Callable[[], Any | Awaitable[Any]]


async def wait_for(condition: Condition, timeout: float = 15.0, interval: float = 0.05) -> Any:
    """Poll ``condition`` (sync or async) until it returns a truthy value, and return that value."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        value = condition()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")
