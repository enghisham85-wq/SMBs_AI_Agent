"""REST contract (T130): schemathesis generates requests from /api/v1/openapi.json and sends them, as
the seeded owner, to the real app (lifespan, migrations and the sample cafe). No request may produce a
server error; that includes the FR-046 guard, which turns figures without data_as_of into a 500.

Endpoints that move the clock, reset or re-seed the demo, inject Chaos faults or end the session are
left out: they are slow or undo the setup, and each has its own tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import create_app

EXCLUDED = ["/api/v1/clock/advance", "/api/v1/demo/reset", "/api/v1/chaos/inject", "/api/v1/auth/logout",
            "/api/v1/auth/login", "/api/v1/documents", "/api/v1/bank/statements", "/api/v1/sales/import",
            "/api/v1/harness/stream", "/api/v1/chat/stream", "/api/v1/business"]

schema = schemathesis.openapi.from_dict(create_app().openapi()).exclude(path=EXCLUDED)


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    from app.seed.sample_cafe import PASSWORDS, seed

    root: Path = tmp_path_factory.mktemp("contract")
    s = get_settings()
    saved = {k: getattr(s, k) for k in ("DATABASE_URL", "CHECKPOINT_DB_PATH", "FILES_DIR", "SAMPLE_INVOICES_DIR")}
    s.DATABASE_URL = f"sqlite+aiosqlite:///{(root / 'app.db').as_posix()}"
    s.CHECKPOINT_DB_PATH = (root / "checkpoints.db").as_posix()
    s.FILES_DIR = (root / "files").as_posix()
    s.SAMPLE_INVOICES_DIR = (root / "samples").as_posix()
    try:
        with TestClient(create_app(), base_url="http://test") as c:  # runs the lifespan: migrations, graphs
            assert c.portal is not None
            c.portal.call(lambda: seed(start_date=date(2026, 10, 4), history_days=42))
            r = c.post("/api/v1/auth/login", json={"username": "owner", "password": PASSWORDS["owner"]})
            assert r.status_code == 200, r.text
            c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
            yield c
    finally:
        for k, v in saved.items():
            setattr(s, k, v)


def _text(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode(errors="replace")
    return str(v).lower() if isinstance(v, bool) else str(v)


def _multipart(parts: list[Any]) -> tuple[dict[str, str], list[tuple[str, Any]]]:
    """Schemathesis lists form fields as file parts with no filename, and in negative mode may emit
    parts no HTTP client can encode; send what a browser could: text fields and real file parts."""
    data: dict[str, str] = {}
    files: list[tuple[str, Any]] = []
    for item in parts:
        if not (isinstance(item, tuple | list) and len(item) == 2):
            continue
        name, part = _text(item[0]), item[1]
        if isinstance(part, tuple | list) and len(part) >= 2:
            filename, content = part[0], part[1]
            if isinstance(filename, str) and filename:
                files.append((name, (filename, content if isinstance(content, bytes) else _text(content).encode())))
            else:
                data[name] = _text(content)
        else:
            data[name] = _text(part)
    return data, files


@schema.parametrize()
@settings(max_examples=8, deadline=None, suppress_health_check=list(HealthCheck), derandomize=True)
def test_no_request_causes_a_server_error(case: Any, client: TestClient) -> None:
    kwargs = case.as_transport_kwargs(base_url="http://test")
    kwargs.pop("cookies", None)  # the session cookie lives on the client
    send = {k: v for k, v in kwargs.items() if k in ("method", "url", "headers", "params", "json", "content", "data")}
    if isinstance(kwargs.get("files"), list):
        send["data"], files = _multipart(kwargs["files"])
        if files:
            send["files"] = files
    response = client.request(**send)
    case.validate_response(response, checks=[not_a_server_error])
