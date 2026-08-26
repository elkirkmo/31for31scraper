import pytest

import app as app_module


@pytest.fixture
def api_key(monkeypatch):
    key = "test-secret-key"
    monkeypatch.setenv("ADMIN_API_KEY", key)
    return key


@pytest.fixture
def auth_headers(api_key):
    return {"X-API-Key": api_key}


@pytest.fixture
def client():
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()
