"""
Storage abstraction so business logic never talks to the local filesystem
directly. Local disk (the current, only option) doesn't survive a redeploy
or a second replica on ephemeral hosting (e.g. Render's free tier) -- this
module lets a deployment opt into an S3-compatible backend (tested against
Backblaze B2) via STORAGE_PROVIDER=s3, with local disk remaining the default
for development and for any existing single-instance deployment.

Callers store whatever `StorageBackend.save()` returns (an absolute path for
the local backend, an object key for S3) in the DB and pass it back into
`read()` / `delete()` / `local_path()` later -- they never touch os.path
directly. This keeps existing local-mode behaviour byte-for-byte identical
(same absolute paths as before this module existed), so rows written before
this migration keep working unmodified.
"""
import io
import os
import threading
from typing import Optional


class StorageBackend:
    def save(self, key: str, data: bytes) -> str:
        """Persist `data` under `key`; return the reference to store in the DB."""
        raise NotImplementedError

    def read(self, ref: str) -> Optional[bytes]:
        raise NotImplementedError

    def delete(self, ref: str) -> None:
        raise NotImplementedError

    def exists(self, ref: str) -> bool:
        raise NotImplementedError

    def local_path(self, ref: str) -> Optional[str]:
        """A real filesystem path for `ref`, if this backend has one (local
        backend only) -- lets a route hand it straight to FastAPI's
        FileResponse instead of buffering the whole file through the app."""
        return None

    def size(self, ref: str) -> Optional[int]:
        return None


class LocalStorageBackend(StorageBackend):
    def __init__(self, base_dir: str):
        self._base_dir = base_dir

    def _resolve(self, ref: str) -> str:
        # `ref` is either a bare key (rows written by this backend) or an
        # absolute path (legacy rows from before this abstraction existed,
        # or any row written while STORAGE_PROVIDER=local) -- accept both.
        if os.path.isabs(ref):
            return ref
        return os.path.join(self._base_dir, ref)

    def save(self, key: str, data: bytes) -> str:
        path = os.path.join(self._base_dir, key)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def read(self, ref: str) -> Optional[bytes]:
        path = self._resolve(ref)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def delete(self, ref: str) -> None:
        try:
            os.remove(self._resolve(ref))
        except FileNotFoundError:
            pass

    def exists(self, ref: str) -> bool:
        return os.path.isfile(self._resolve(ref))

    def local_path(self, ref: str) -> Optional[str]:
        path = self._resolve(ref)
        return path if os.path.isfile(path) else None

    def size(self, ref: str) -> Optional[int]:
        path = self._resolve(ref)
        return os.path.getsize(path) if os.path.isfile(path) else None


class S3StorageBackend(StorageBackend):
    """S3-compatible backend. Verified against Backblaze B2's S3-compatible
    API; works with AWS S3 or any other S3-compatible endpoint the same way."""

    def __init__(
        self,
        bucket: str,
        endpoint_url: Optional[str],
        region: Optional[str],
        access_key: Optional[str],
        secret_key: Optional[str],
        prefix: str = "",
    ):
        import boto3  # local import: only required when STORAGE_PROVIDER=s3

        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
        )

    def _full_key(self, ref: str) -> str:
        # `ref` is always a bare key for this backend (S3 has no concept of
        # an absolute local path), namespaced under this backend's prefix.
        return f"{self._prefix}/{ref}" if self._prefix else ref

    def save(self, key: str, data: bytes) -> str:
        self._client.put_object(Bucket=self._bucket, Key=self._full_key(key), Body=data)
        return key

    def read(self, ref: str) -> Optional[bytes]:
        from botocore.exceptions import ClientError

        try:
            obj = self._client.get_object(Bucket=self._bucket, Key=self._full_key(ref))
            return obj["Body"].read()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise

    def delete(self, ref: str) -> None:
        from botocore.exceptions import ClientError

        try:
            self._client.delete_object(Bucket=self._bucket, Key=self._full_key(ref))
        except ClientError:
            pass

    def exists(self, ref: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=self._full_key(ref))
            return True
        except ClientError:
            return False

    def size(self, ref: str) -> Optional[int]:
        from botocore.exceptions import ClientError

        try:
            head = self._client.head_object(Bucket=self._bucket, Key=self._full_key(ref))
            return head.get("ContentLength")
        except ClientError:
            return None


_backends: dict[str, StorageBackend] = {}
_lock = threading.Lock()


def get_storage(namespace: str, local_env_var: str, local_default: str) -> StorageBackend:
    """
    `namespace` groups related files (e.g. "cv", "jd", "docs") -- under the
    local backend that's a subdirectory (whatever `local_env_var` already
    points at, unchanged), under S3 it's a key prefix in one shared bucket.
    """
    if namespace in _backends:
        return _backends[namespace]
    with _lock:
        if namespace in _backends:
            return _backends[namespace]
        provider = os.getenv("STORAGE_PROVIDER", "local").lower()
        if provider == "s3":
            backend: StorageBackend = S3StorageBackend(
                bucket=os.environ["S3_BUCKET"],
                endpoint_url=os.getenv("S3_ENDPOINT"),
                region=os.getenv("S3_REGION", "us-east-1"),
                access_key=os.getenv("S3_ACCESS_KEY"),
                secret_key=os.getenv("S3_SECRET_KEY"),
                prefix=namespace,
            )
        else:
            backend = LocalStorageBackend(os.getenv(local_env_var, local_default))
        _backends[namespace] = backend
        return backend


def storage_response(
    backend: StorageBackend,
    ref: str,
    filename: str,
    media_type: str,
    inline: bool = False,
):
    """FileResponse when the backend can hand back a real path (local),
    otherwise buffers the object into a StreamingResponse (S3). Returns
    None if the object doesn't exist so the caller can 404."""
    from fastapi.responses import FileResponse, StreamingResponse

    disposition = "inline" if inline else "attachment"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}

    local = backend.local_path(ref)
    if local:
        return FileResponse(local, media_type=media_type, headers=headers)

    data = backend.read(ref)
    if data is None:
        return None
    return StreamingResponse(io.BytesIO(data), media_type=media_type, headers=headers)
