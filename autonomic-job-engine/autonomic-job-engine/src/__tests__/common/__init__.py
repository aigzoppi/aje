"""Code shared by unit and integration tests, one module per responsibility.

specs       builders for job / group / worker specifications (API bodies and models)
polling     wait_for: poll a sync or async condition
fakes       in-memory stand-ins for the ports the services and the runtime depend on
kubernetes  render a worker manifest and read its container environment
handlers    worker handlers run by the in-process backend
engine      settings and a running engine for integration tests
probe       EngineProbe: drive and observe a running engine
api         ApiClient: one object per HTTP resource, plus ``expect`` for status checks
schema      SchemaAdmin: migrate and inspect the test database
containers  Testcontainers wrappers (PostgreSQL, RabbitMQ)
"""
