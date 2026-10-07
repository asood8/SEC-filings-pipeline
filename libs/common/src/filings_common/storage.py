"""Object storage for raw filings, behind an interface so the provider can change.

`LocalObjectStore` is for development and tests; `S3ObjectStore` talks to any S3-compatible
service, which in production is Cloudflare R2. Reads and writes are traced (keys are public paths
like filings/<cik>/<accession>/<document>).
"""

import os
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import boto3
from botocore.exceptions import ClientError
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind

from filings_common.config import ObjectStoreSettings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

tracer = trace.get_tracer(__name__)


class ObjectNotFoundError(LookupError):
    pass


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


def _span(operation: str, backend: str, key: str) -> AbstractContextManager[Span]:
    return tracer.start_as_current_span(
        f"object_store {operation}",
        kind=SpanKind.CLIENT,
        attributes={"filings.object_store.backend": backend, "filings.object_store.key": key},
    )


def validate_key(key: str) -> str:
    """Keys are relative, slash-separated paths with no empty, '.' or '..' segments."""
    segments = key.split("/")
    if not key or "\\" in key or any(s in ("", ".", "..") for s in segments):
        raise ValueError(f"invalid object key: {key!r}")
    return key


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        return self._root / validate_key(key)

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        path = self._path(key)
        with _span("put", "local", key):
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file and rename, so readers never see a partial object.
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise

    def get(self, key: str) -> bytes:
        path = self._path(key)
        with _span("get", "local", key):
            try:
                return path.read_bytes()
            except FileNotFoundError:
                raise ObjectNotFoundError(key) from None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class S3ObjectStore:
    def __init__(self, client: "S3Client", bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        validate_key(key)
        with _span("put", "s3", key):
            if content_type is None:
                self._client.put_object(Bucket=self._bucket, Key=key, Body=data)
            else:
                self._client.put_object(
                    Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
                )

    def get(self, key: str) -> bytes:
        validate_key(key)
        with _span("get", "s3", key):
            try:
                return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
            except self._client.exceptions.NoSuchKey:
                raise ObjectNotFoundError(key) from None

    def exists(self, key: str) -> bool:
        validate_key(key)
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True


def object_store_from_settings(settings: ObjectStoreSettings) -> ObjectStore:
    if settings.backend == "local":
        return LocalObjectStore(settings.local_root)
    if not (settings.s3_bucket and settings.s3_access_key_id and settings.s3_secret_access_key):
        raise ValueError("the s3 backend needs OBJECT_STORE_S3_BUCKET and access keys")
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
    )
    return S3ObjectStore(client, settings.s3_bucket)
