"""Backend registry: built-in backends and plug-in registration."""

import pytest

from aje import config
from aje.infrastructure.backends import registry


@pytest.fixture(name="fake_backend")
def fake_backend_fixture():
    """A backend registered under "test-fake" for the duration of the test."""
    sentinel = object()
    registry.register_backend("test-fake")(lambda settings: sentinel)
    yield sentinel
    registry._FACTORIES.pop("test-fake", None)  # pylint: disable=protected-access  # undo registration


class BackendRegistryTest:
    def test_builtin_backends_are_registered(self):
        assert {"local", "kubernetes"} <= set(registry.available_backends())

    def test_local_backend_is_built_from_settings(self, tmp_path):
        assert (
            registry.create_backend(config.Settings(backend="local", local_log_dir=str(tmp_path))).name
            == "local"
        )

    def test_new_backends_plug_in_without_changing_the_registry(self, fake_backend):
        assert registry.create_backend(config.Settings(backend="test-fake")) is fake_backend

    def test_unknown_backend(self):
        with pytest.raises(ValueError, match="available"):
            registry.create_backend(config.Settings(backend="nope"))
