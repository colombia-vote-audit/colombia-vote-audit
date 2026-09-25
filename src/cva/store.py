"""Content-addressed file storage on local disk: gazette PDFs, and files
attached to comments."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class BlobStore:
    def __init__(self, root: Path, suffix: str = ".pdf"):
        self.root = root
        self.suffix = suffix
        root.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha256: str) -> Path:
        return self.root / sha256[:2] / f"{sha256}{self.suffix}"

    def put(self, data: bytes) -> str:
        """Store `data` and return its sha256.

        Safe to call from several threads. The file only appears under its
        final name once it is completely written and synced.
        """
        sha = hashlib.sha256(data).hexdigest()
        dest = self.path_for(sha)
        if dest.exists():
            return sha
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sha

    def delete(self, sha256: str):
        self.path_for(sha256).unlink(missing_ok=True)
