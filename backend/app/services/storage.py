"""Attachment storage.

Local disk in development, Azure Blob Storage in production. The rest of the
system only ever holds a `blob_uri` string, so swapping the backend is a
config change.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Protocol

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def content_hash(data: bytes) -> str:
    """SHA-256 of the bytes — the key for duplicate-RFQ detection."""
    return hashlib.sha256(data).hexdigest()


def safe_filename(filename: str) -> str:
    """Strip anything that could escape the storage root or confuse a URI."""
    cleaned = _UNSAFE.sub("_", Path(filename).name).strip("._")
    return cleaned or "attachment"


class Storage(Protocol):
    def put(self, key: str, data: bytes, content_type: str | None = None) -> str: ...
    def get(self, key: str) -> bytes: ...


class LocalStorage:
    """Files under `storage_root`. Keys are relative paths."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path_for(self, key: str) -> Path:
        path = (self.root / key).resolve()
        # Refuse anything that resolves outside the root.
        if not path.is_relative_to(self.root):
            raise ValueError(f"Storage key '{key}' escapes the storage root")
        return path

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.as_uri()

    def get(self, key: str) -> bytes:
        return self._path_for(key).read_bytes()

    def get_by_uri(self, uri: str) -> bytes:
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise ValueError(f"Not a local storage URI: {uri}")
        path = Path(unquote(parsed.path)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"URI '{uri}' escapes the storage root")
        return path.read_bytes()


class AzureBlobStorage:  # pragma: no cover - requires Azure credentials
    """Azure Blob Storage backend."""

    def __init__(self, connection_string: str, container: str) -> None:
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError as exc:
            raise RuntimeError(
                "azure-storage-blob is not installed; add it to requirements "
                "or set AQM_STORAGE_BACKEND=local"
            ) from exc
        self._service = BlobServiceClient.from_connection_string(connection_string)
        self._container = container
        try:
            self._service.create_container(container)
        except Exception:
            logger.debug("Container %s already exists", container)

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        blob = self._service.get_blob_client(container=self._container, blob=key)
        kwargs = {}
        if content_type:
            from azure.storage.blob import ContentSettings

            kwargs["content_settings"] = ContentSettings(content_type=content_type)
        blob.upload_blob(data, overwrite=True, **kwargs)
        return blob.url

    def get(self, key: str) -> bytes:
        blob = self._service.get_blob_client(container=self._container, blob=key)
        return blob.download_blob().readall()


def get_storage(settings: Settings | None = None) -> Storage:
    settings = settings or get_settings()
    if settings.storage_backend == "azure":
        if not settings.azure_storage_connection_string:
            raise RuntimeError("AQM_STORAGE_BACKEND=azure but no connection string is set")
        return AzureBlobStorage(
            settings.azure_storage_connection_string,
            settings.azure_storage_container,
        )
    return LocalStorage(settings.storage_root)


def attachment_key(enquiry_id: int, filename: str, digest: str) -> str:
    """Stable, collision-proof storage key for one attachment."""
    return f"enquiries/{enquiry_id}/{digest[:12]}-{safe_filename(filename)}"
