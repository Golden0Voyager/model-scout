"""Basic tests for model_scout backend."""


def test_app_imports():
    """Test that the app module can be imported."""
    from app import app
    assert app is not None


def test_config_imports():
    """Test that the config module can be imported."""
    from core.config import settings
    assert settings is not None
