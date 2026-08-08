import json

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


@pytest.fixture
def data_file(tmp_path, monkeypatch):
    """Point app.DATA_FILE at a throwaway file so tests never touch the real data.json."""

    def _write(data):
        path = tmp_path / "data.json"
        path.write_text(json.dumps(data))
        monkeypatch.setattr(app_module, "DATA_FILE", str(path))
        return path

    return _write
