"""Blob store: where bundles, audio and stage artifacts live (Section 2). One interface, two backends."""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path

_SAFE_KEY = re.compile(r"^[A-Za-z0-9_\-./]+$")


def check_key(key: str) -> str:
    """Keys look like `stories/{id}/v1/bundle/audio/s01_l01.mp3`: relative, no `..`, no backslashes."""
    if not key or not _SAFE_KEY.match(key) or key.startswith("/") or ".." in key.split("/") or "//" in key:
        raise ValueError(f"bad blob key: {key!r}")
    return key


class BlobStore(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def list(self, prefix: str) -> list[str]: ...

    def url_for(self, key: str) -> str:  # not used by the API (the play endpoints stream the bytes)
        raise NotImplementedError


class LocalBlobStore(BlobStore):
    """Files under one directory (`BLOB_DIR`). The default backend, also used by the tests."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)  # never leave a half-written file behind

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str) -> list[str]:
        base = self.root
        return sorted(
            str(p.relative_to(base)).replace(os.sep, "/")
            for p in base.rglob("*")
            if p.is_file()
            and not p.name.endswith(".part")
            and str(p.relative_to(base)).replace(os.sep, "/").startswith(prefix)
        )

    def url_for(self, key: str) -> str:
        return self._path(key).as_uri()


class MemoryBlobStore(BlobStore):
    """In-memory store for unit tests."""

    def __init__(self):
        self.data: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self.data[check_key(key)] = bytes(data)

    def get(self, key: str) -> bytes:
        return self.data[check_key(key)]

    def exists(self, key: str) -> bool:
        return check_key(key) in self.data

    def list(self, prefix: str) -> list[str]:
        return sorted(k for k in self.data if k.startswith(prefix))


class ScopedBlobStore(BlobStore):
    """A view of another store under a key prefix, e.g. `stories/{story_id}/v{version}/` (Section 4, blob keys).
    Stages write relative keys (`audio/s01_l01.mp3`); the prefix keeps stories and versions apart."""

    def __init__(self, inner: BlobStore, prefix: str):
        self.inner, self.prefix = inner, check_key(prefix.rstrip("/")) + "/"

    def put(self, key: str, data: bytes) -> None:
        self.inner.put(self.prefix + check_key(key), data)

    def get(self, key: str) -> bytes:
        return self.inner.get(self.prefix + check_key(key))

    def exists(self, key: str) -> bool:
        return self.inner.exists(self.prefix + check_key(key))

    def list(self, prefix: str) -> list[str]:
        return [k[len(self.prefix) :] for k in self.inner.list(self.prefix + prefix)]


def version_prefix(story_id: str, version: int) -> str:
    """`stories/{story_id}/v{version}` (Section 4)."""
    return f"stories/{story_id}/v{version}"
