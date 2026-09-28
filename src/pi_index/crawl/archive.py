from __future__ import annotations

from dataclasses import dataclass
import gzip
from pathlib import Path
from threading import Lock
from uuid import uuid4

from ..models import content_hash


@dataclass(frozen=True)
class ArchiveEntry:
    archive_key: str
    body_sha256: str
    uncompressed_bytes: int
    compressed_bytes: int
    created: bool


class ContentArchive:
    """Content-addressed gzip archive for replayable HTTP responses."""

    def __init__(self, root: str | Path, *, read_only: bool = False):
        self.root = Path(root).resolve()
        self.read_only = bool(read_only)
        if not self.read_only:
            self.root.mkdir(parents=True, exist_ok=True)
        self._write_lock = Lock()

    def _path(self, archive_key: str) -> Path:
        path = (self.root / archive_key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"Archive key escapes root: {archive_key}")
        return path

    def store(self, body: bytes, *, force: bool = False) -> ArchiveEntry:
        digest = content_hash(body)
        archive_key = (Path("blobs") / "sha256" / digest[:2] / f"{digest}.gz").as_posix()
        target = self._path(archive_key)
        if self.read_only:
            # Preview callers may still need Fetcher's normal archive metadata
            # and access to already captured blobs.  Compute any missing size
            # in memory, but never create a directory or file.
            compressed_bytes = (
                target.stat().st_size
                if target.is_file()
                else len(gzip.compress(body, compresslevel=6, mtime=0))
            )
            return ArchiveEntry(
                archive_key=archive_key,
                body_sha256=digest,
                uncompressed_bytes=len(body),
                compressed_bytes=compressed_bytes,
                created=False,
            )
        created = False
        with self._write_lock:
            if force or not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
                try:
                    temporary.write_bytes(gzip.compress(body, compresslevel=6, mtime=0))
                    temporary.replace(target)
                    created = True
                finally:
                    temporary.unlink(missing_ok=True)
            compressed_bytes = target.stat().st_size
        return ArchiveEntry(
            archive_key=archive_key,
            body_sha256=digest,
            uncompressed_bytes=len(body),
            compressed_bytes=compressed_bytes,
            created=created,
        )

    def read(self, archive_key: str) -> bytes:
        body = gzip.decompress(self._path(archive_key).read_bytes())
        expected = Path(archive_key).stem
        if content_hash(body) != expected:
            raise ValueError(f"Archive content hash mismatch: {archive_key}")
        return body

    def exists(self, archive_key: str | None) -> bool:
        return bool(archive_key) and self._path(str(archive_key)).is_file()
