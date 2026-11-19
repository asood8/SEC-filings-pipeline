import base64
import hashlib
import re

import pytest
from fastapi.testclient import TestClient

from filings_api.docs_page import (
    SWAGGER_UI_CSS,
    SWAGGER_UI_CSS_INTEGRITY,
    SWAGGER_UI_JS,
    SWAGGER_UI_JS_INTEGRITY,
    SWAGGER_UI_VERSION,
    docs_page,
)

pytestmark = pytest.mark.integration


def test_the_docs_load_one_pinned_integrity_checked_swagger_ui(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert re.fullmatch(r"\d+\.\d+\.\d+", SWAGGER_UI_VERSION)  # an exact version, not "@5"
    assert (
        f'<script src="{SWAGGER_UI_JS}" integrity="{SWAGGER_UI_JS_INTEGRITY}" '
        'crossorigin="anonymous"></script>'
    ) in page
    assert (
        f'<link rel="stylesheet" href="{SWAGGER_UI_CSS}" integrity="{SWAGGER_UI_CSS_INTEGRITY}" '
        'crossorigin="anonymous">'
    ) in page
    # Nothing else from another site: no unpinned files, no favicon, no validator badge.
    assert set(re.findall(r'(?:src|href)="(https?://[^"]+)"', page)) == {
        SWAGGER_UI_JS,
        SWAGGER_UI_CSS,
    }
    assert '"validatorUrl": null' in page
    assert '"url": "/openapi.json"' in page
    assert client.get("/openapi.json").status_code == 200


def test_the_docs_policy_allows_only_the_pinned_script_and_its_start_up_script(
    client: TestClient,
) -> None:
    response = client.get("/docs")
    header = response.headers["content-security-policy"]
    policy = dict(directive.strip().split(" ", 1) for directive in header.split(";"))
    [inline] = re.findall(r"<script>(.*?)</script>", response.text, re.DOTALL)
    digest = base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode()
    assert policy["script-src"] == f"{SWAGGER_UI_JS} 'sha256-{digest}'"
    assert policy["default-src"] == "'none'"
    assert policy["connect-src"] == "'self'"
    assert policy["frame-ancestors"] == "'none'"
    assert response.headers["X-Frame-Options"] == "DENY"  # the API's usual headers still apply


def test_redoc_is_not_served(client: TestClient) -> None:
    assert client.get("/redoc").status_code == 404


def test_the_start_up_script_cannot_be_closed_early() -> None:
    page = bytes(docs_page("x", "/openapi.json?</script><script>alert(1)//").body).decode()
    assert page.count("</script>") == 2  # the bundle's and the start-up script's own
    assert "\\u003c/script>" in page
