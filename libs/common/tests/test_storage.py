import io
from pathlib import Path

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from filings_common.config import ObjectStoreSettings
from filings_common.storage import (
    LocalObjectStore,
    ObjectNotFoundError,
    S3ObjectStore,
    object_store_from_settings,
)

KEY = "filings/320193/0000320193-24-000123/doc.xml"


def test_local_store_round_trip_and_overwrite(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    assert not store.exists(KEY)
    store.put(KEY, b"first")
    store.put(KEY, b"second")
    assert store.exists(KEY)
    assert store.get(KEY) == b"second"
    # No temp files are left behind next to the object.
    assert [p.name for p in (tmp_path / KEY).parent.iterdir()] == ["doc.xml"]


def test_local_store_missing_object(tmp_path: Path) -> None:
    with pytest.raises(ObjectNotFoundError):
        LocalObjectStore(tmp_path).get(KEY)


@pytest.mark.parametrize("key", ["", "/abs", "a//b", "../x", "a/../b", "a/./b", "a\\b", "dir/"])
def test_rejects_keys_that_could_escape_or_collide(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError, match="invalid object key"):
        LocalObjectStore(tmp_path).put(key, b"x")


@pytest.fixture
def s3() -> tuple[S3ObjectStore, Stubber]:
    client = boto3.client(
        "s3",
        endpoint_url="https://account.r2.cloudflarestorage.com",
        region_name="auto",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    return S3ObjectStore(client, "raw-filings"), Stubber(client)


def test_s3_store_put_and_get(s3: tuple[S3ObjectStore, Stubber]) -> None:
    store, stub = s3
    stub.add_response(
        "put_object",
        {},
        {"Bucket": "raw-filings", "Key": KEY, "Body": b"<xml/>", "ContentType": "application/xml"},
    )
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(b"<xml/>"), 6)},
        {"Bucket": "raw-filings", "Key": KEY},
    )
    with stub:
        store.put(KEY, b"<xml/>", content_type="application/xml")
        assert store.get(KEY) == b"<xml/>"


def test_s3_store_missing_object(s3: tuple[S3ObjectStore, Stubber]) -> None:
    store, stub = s3
    stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
    stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
    with stub:
        with pytest.raises(ObjectNotFoundError):
            store.get(KEY)
        assert not store.exists(KEY)


def test_settings_choose_the_backend(tmp_path: Path) -> None:
    local = ObjectStoreSettings(_env_file=None, backend="local", local_root=tmp_path)
    assert isinstance(object_store_from_settings(local), LocalObjectStore)
    with pytest.raises(ValueError, match="OBJECT_STORE_S3_BUCKET"):
        object_store_from_settings(ObjectStoreSettings(_env_file=None, backend="s3"))
