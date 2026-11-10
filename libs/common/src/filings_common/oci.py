"""A small Oracle Cloud (OCI) API client: signed requests, and Resource Search.

Oracle's APIs take HTTP requests signed with the user's API key (RSA-SHA256 over the date, the
request line and the host, plus the body's length, type and SHA-256 for requests that have one):
https://docs.oracle.com/en-us/iaas/Content/API/Concepts/signingrequests.htm. Signing is a few
lines with `cryptography`, so the project doesn't carry Oracle's SDK for the two tools that call
Oracle: the daily inventory check (filings_common.oci_inventory) and the operator's off switch
(filings_common.oci_switch).
"""

import base64
import configparser
import hashlib
import json
from collections.abc import Callable, Mapping
from email.utils import formatdate
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlencode

import httpx2
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

SEARCH_PAGE = 1000
MAX_PAGES = 50


class OciError(Exception):
    """Oracle's API refused a request or couldn't be reached."""


class OciClient:
    def __init__(
        self,
        *,
        tenancy: str,
        user: str,
        fingerprint: str,
        private_key_pem: bytes,
        region: str,
        transport: httpx2.BaseTransport | None = None,
        date: Callable[[], str] = lambda: formatdate(usegmt=True),
    ) -> None:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise OciError("the API key must be an RSA private key")
        self._key = key
        self._key_id = f"{tenancy}/{user}/{fingerprint}"
        self.tenancy = tenancy
        self.region = region
        self._date = date
        self._http = httpx2.Client(timeout=30.0, transport=transport, follow_redirects=False)

    @classmethod
    def from_config_file(
        cls, path: Path = Path("~/.oci/config"), profile: str = "DEFAULT", **more: Any
    ) -> Self:
        """The operator's own key, as Oracle's CLI and Terraform read it."""
        parser = configparser.ConfigParser()
        if not parser.read(path.expanduser()) or profile not in parser:
            raise OciError(f"no profile {profile} in {path}")
        section = parser[profile]
        try:
            return cls(
                tenancy=section["tenancy"],
                user=section["user"],
                fingerprint=section["fingerprint"],
                private_key_pem=Path(section["key_file"]).expanduser().read_bytes(),
                region=section["region"],
                **more,
            )
        except KeyError as exc:
            raise OciError(f"{path} lacks {exc}") from exc

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self._http.close()

    def host(self, service: str) -> str:
        """Each service's endpoint in the region; Core Services (compute, volumes) is "iaas"."""
        if service == "iaas":
            return f"iaas.{self.region}.oraclecloud.com"
        return f"{service}.{self.region}.oci.oraclecloud.com"

    def request(
        self,
        method: str,
        service: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        body: Any = None,
    ) -> httpx2.Response:
        host = self.host(service)
        target = path + (f"?{urlencode(params)}" if params else "")
        headers = {"date": self._date(), "host": host}
        signed = ["date", "(request-target)", "host"]
        content = None
        if method in ("POST", "PUT", "PATCH"):
            content = b"" if body is None else json.dumps(body).encode()
            headers |= {
                "content-length": str(len(content)),
                "content-type": "application/json",
                "x-content-sha256": base64.b64encode(hashlib.sha256(content).digest()).decode(),
            }
            signed += ["content-length", "content-type", "x-content-sha256"]
        request_line = f"(request-target): {method.lower()} {target}"
        lines = [
            request_line if name == "(request-target)" else f"{name}: {headers[name]}"
            for name in signed
        ]
        signature = self._key.sign("\n".join(lines).encode(), padding.PKCS1v15(), hashes.SHA256())
        headers["authorization"] = (
            f'Signature version="1",keyId="{self._key_id}",algorithm="rsa-sha256",'
            f'headers="{" ".join(signed)}",signature="{base64.b64encode(signature).decode()}"'
        )
        try:
            return self._http.request(
                method, f"https://{host}{target}", headers=headers, content=content
            )
        except httpx2.HTTPError as exc:
            raise OciError(f"{method} {host}{path}: {exc!r}") from exc

    def get(self, service: str, path: str, **params: str | int) -> Any:
        response = self.request("GET", service, path, params=params or None)
        if response.status_code != 200:
            raise OciError(f"GET {service}{path}: HTTP {response.status_code}")
        return response.json()

    def search(self, query: str) -> list[dict[str, Any]]:
        """Every resource a Resource Search query matches, in this region."""
        items: list[dict[str, Any]] = []
        params: dict[str, str | int] = {"limit": SEARCH_PAGE}
        for _ in range(MAX_PAGES):
            response = self.request(
                "POST",
                "query",
                "/20180409/resources",
                params=params,
                body={"type": "Structured", "query": query},
            )
            if response.status_code != 200:
                raise OciError(f"resource search: HTTP {response.status_code}")
            items += response.json()["items"]
            page = response.headers.get("opc-next-page")
            if not page:
                return items
            params = {"limit": SEARCH_PAGE, "page": page}
        raise OciError(f"resource search: more than {MAX_PAGES} pages")
