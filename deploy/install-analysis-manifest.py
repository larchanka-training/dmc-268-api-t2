"""Root-only, atomic staging manifest installation. No provider/network calls."""

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

MANIFEST_MODE = 0o644


def install(source: Path, digest: str) -> None:
    if os.geteuid() != 0:
        raise ValueError("root or noninteractive sudo is required")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid source digest")
    with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("source must be a regular file")
        body = stream.read()
    if hashlib.sha256(body).hexdigest() != digest:
        raise ValueError("source integrity mismatch")
    document = json.loads(body)
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("invalid manifest document")
    directory = Path("/opt/dmc-268-t2/analysis-runtime")
    for parent in (*reversed(directory.parents), directory):
        if not parent.exists() and not parent.is_symlink():
            parent.mkdir(mode=0o755)
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise ValueError(f"untrusted manifest directory: {parent}")
    target = directory / "analysis-runtime.v1.json"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("manifest destination must be a regular file")
    fd, name = tempfile.mkstemp(prefix=".manifest-", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchown(stream.fileno(), 0, 0)
            os.fchmod(stream.fileno(), MANIFEST_MODE)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        info = target.lstat()
        if (
            info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != MANIFEST_MODE
            or hashlib.sha256(target.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("installed manifest verification failed")
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        install(Path(sys.argv[1]), sys.argv[2])
    except (OSError, ValueError, IndexError) as error:
        raise SystemExit(f"Manifest installation failed: {error}") from None
    print("Manifest integrity and root:root 0644 verified")
