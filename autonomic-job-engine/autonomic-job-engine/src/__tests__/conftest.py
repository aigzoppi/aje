"""Test layout.

src/__tests__/common/       shared handlers, helpers, Testcontainers wrappers
src/__tests__/unit/         pure logic and services with fakes: no Docker, no network
src/__tests__/integration/  the real engine against PostgreSQL + RabbitMQ (Testcontainers)

pytest src/__tests__/unit          # fast feedback
pytest -m integration               # needs Docker
"""
