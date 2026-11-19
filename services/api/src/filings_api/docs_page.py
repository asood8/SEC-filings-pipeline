"""The interactive API docs at /docs: Swagger UI, pinned to one version and checked with
Subresource Integrity, under a Content-Security-Policy of its own.

FastAPI's built-in page loads `swagger-ui-dist@5` from jsDelivr, whatever 5.x is newest, with no
integrity check. The page runs on the API's own origin, where a signed-in user's session cookie
works, so a compromised release or CDN could act as that user. Here the browser refuses any file
whose hash differs from the one below, and the policy lets the page run only that script and its
own inline start-up script.

Updating Swagger UI: pick a release at least a week old, download the two files from the npm
registry, check the tarball against the registry's `dist.integrity`, and put each file's
`sha384-` hash here (`openssl dgst -sha384 -binary FILE | base64 -w0`), after checking that
jsDelivr serves the same bytes (runbook, "Updating Swagger UI").
"""

import base64
import hashlib
import html
import json

from fastapi.openapi.docs import swagger_ui_default_parameters
from fastapi.responses import HTMLResponse

SWAGGER_UI_VERSION = "5.34.1"
_CDN = f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}"
SWAGGER_UI_JS = f"{_CDN}/swagger-ui-bundle.js"
SWAGGER_UI_JS_INTEGRITY = "sha384-XFYN47VzPiDY6qPtxYKg0nqUeW/upo20xf1j2SUE7gT/i35FJB4cuS/71Iegzk8Z"
SWAGGER_UI_CSS = f"{_CDN}/swagger-ui.css"
SWAGGER_UI_CSS_INTEGRITY = "sha384-4c3vsE5f+trhFyHrFrLM1fFGUn531NIaC7dcqEWIJdoRQJ5CkJbSuSCwXLpaS1KF"


def _start_script(openapi_url: str) -> str:
    parameters = {
        **swagger_ui_default_parameters,
        "url": openapi_url,
        # Swagger UI otherwise sends the schema's address to validator.swagger.io for a badge.
        "validatorUrl": None,
    }
    # json.dumps output is valid JavaScript; escaping "<" keeps "</script>" out of the page.
    config = json.dumps(parameters, sort_keys=True).replace("<", "\\u003c")
    return (
        f"const config = {config};\n"
        "config.presets = [\n"
        "  SwaggerUIBundle.presets.apis,\n"
        "  SwaggerUIBundle.SwaggerUIStandalonePreset,\n"
        "];\n"
        "window.ui = SwaggerUIBundle(config);\n"
    )


def docs_page(title: str, openapi_url: str) -> HTMLResponse:
    script = _start_script(openapi_url)
    script_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    policy = "; ".join(
        [
            "default-src 'none'",
            f"script-src {SWAGGER_UI_JS} 'sha256-{script_hash}'",
            # Swagger UI sets style attributes as it renders; styles can't run code.
            f"style-src {SWAGGER_UI_CSS} 'unsafe-inline'",
            "img-src 'self' data:",
            "connect-src 'self'",  # the schema, and "Try it out" requests
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        ]
    )
    stylesheet = (
        f'<link rel="stylesheet" href="{SWAGGER_UI_CSS}" '
        f'integrity="{SWAGGER_UI_CSS_INTEGRITY}" crossorigin="anonymous">'
    )
    bundle = (
        f'<script src="{SWAGGER_UI_JS}" integrity="{SWAGGER_UI_JS_INTEGRITY}" '
        'crossorigin="anonymous"></script>'
    )
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
{stylesheet}
</head>
<body>
<div id="swagger-ui"></div>
{bundle}
<script>{script}</script>
</body>
</html>
"""
    return HTMLResponse(page, headers={"Content-Security-Policy": policy})
