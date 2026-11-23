"""Server-Sent Events in tests: parse HTTP responses, read the services' stream generators."""

import asyncio
from collections.abc import AsyncIterator, Callable
import json
import re
from typing import Any

import pydantic


class Message(pydantic.BaseModel):
    """One SSE message, with its ``data`` decoded from JSON."""

    event: str = "message"
    data: Any = None
    id: str | None = None

    def kind(self) -> str | None:
        """The event kind of an ``event`` or ``task`` message (``job.started``, ``task.done``, ...)."""
        return self.data.get("kind") if isinstance(self.data, dict) else None


def parse(text: str) -> list[Message]:
    """Parses a ``text/event-stream`` body; comments (keep-alive pings) are skipped."""
    out = []
    for block in re.split(r"\r?\n\r?\n", text):
        fields: dict[str, str] = {}
        data: list[str] = []
        for line in block.splitlines():
            if not line or line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if name == "data":
                data.append(value)
            elif name in ("event", "id"):
                fields[name] = value
        if data or fields:
            out.append(Message(data=json.loads("\n".join(data)) if data else None, **fields))
    return out


async def connected() -> bool:
    """A ``disconnected`` callback for streams read in-process: the client never leaves."""
    return False


async def read(
    stream: AsyncIterator[dict[str, str]], until: Callable[[list[Message]], bool], timeout: float = 20.0
) -> list[Message]:
    """Reads a stream generator until ``until(messages)`` holds or the stream ends.

    Raises:
        AssertionError: Neither happened within ``timeout`` seconds.
    """
    out: list[Message] = []

    async def run() -> None:
        async for raw in stream:
            out.append(Message(event=raw["event"], data=json.loads(raw["data"]), id=raw.get("id")))
            if until(out):
                return

    try:
        await asyncio.wait_for(run(), timeout)
    except TimeoutError:
        raise AssertionError(f"stream condition not met within {timeout}s; got {out}") from None
    return out


def ended(messages: list[Message]) -> bool:
    return bool(messages) and messages[-1].event in ("end", "reset")


def event_ids(messages: list[Message]) -> list[int]:
    return [int(m.id) for m in messages if m.id is not None]
