import json
from pathlib import Path

import pytest

from omega3 import db
from omega3.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg(tmp_path):
    """Real config.yaml, with data/ and reports/ relocated to a temp dir."""
    return load_config(root=tmp_path)


@pytest.fixture
def conn(cfg):
    connection = db.connect(cfg.db_path)
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def pdf_manifest() -> dict:
    items = json.loads((FIXTURES / "pdf" / "MANIFEST.json").read_text(encoding="utf-8"))
    return {item["fixture"]: item for item in items}


class FakeResponse:
    def __init__(self, content=b"", status=200, headers=None, json_data=None):
        self.content = content if json_data is None else json.dumps(json_data).encode()
        self.status_code = status
        self.headers = headers or {}
        self._json = json_data

    def json(self):
        return self._json if self._json is not None else json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    """Stands in for PoliteSession. `routes` maps a predicate to a responder."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict | None]] = []
        self.request_count = 0

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        self.request_count += 1
        resp = self.handler(url, params or {})
        resp.raise_for_status()
        return resp


@pytest.fixture
def fake_session_cls():
    return FakeSession


@pytest.fixture
def fake_response_cls():
    return FakeResponse
