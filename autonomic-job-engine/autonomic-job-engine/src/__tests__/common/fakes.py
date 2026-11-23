"""Stand-ins for collaborators that involve no database or broker state.

Anything touching PostgreSQL or RabbitMQ is tested against the real thing
(Testcontainers); only pure in-process seams are faked here.
"""


class FakeResultTransport:
    """Worker transport that keeps published results in memory."""

    def __init__(self):
        self.results: list[dict] = []

    async def publish_result(self, message: dict) -> None:
        self.results.append(message)
