"""Read and write individual keys in the project's .env file.

Reads the file line-by-line, replaces matching ``KEY=value`` lines in-place,
appends new keys at the end, and publishes the complete result through a
unique owner-only sibling file.  Comments and blank lines are preserved
verbatim.

Thread-safety: a module-level lock serialises concurrent writes.
"""
from __future__ import annotations

import errno
import os
import secrets
import stat
import threading
from pathlib import Path

_LOCK = threading.Lock()

# Local development retains the repository .env fallback. Production always
# sets VITALS_ENV_FILE to the allowlisted file inside its directory bind.
_ENV_PATH = Path(__file__).parent.parent.parent / ".env"


def _find_env_path() -> Path:
    """Return the configured application env path."""
    override = os.getenv("VITALS_ENV_FILE")
    return Path(override) if override else _ENV_PATH


def read_key(key: str) -> str:
    """Return the value for *key* from the .env file, or an empty string if
    the key is absent or the file does not exist."""
    path = _find_env_path()
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        if k.strip() == key:
            return v.strip()
    return ""


def _open_parent_directory(path: Path) -> int:
    """Open and anchor the real directory that owns the runtime env file."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.parent, flags)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError(
            errno.ENOTDIR,
            f"Runtime env parent is not a real directory: {path.parent}",
            path.parent,
        )
    return descriptor


def _read_existing_lines(path: Path, *, parent_descriptor: int) -> list[str]:
    """Read an existing regular env file without following a symlink.

    The writer is allowed to create a missing file, but it must never turn an
    attacker-controlled link into a source of configuration or replace a
    special file.  ``O_NOFOLLOW`` closes the check/open race on the production
    POSIX platform; the descriptor check also rejects non-regular files.
    """

    try:
        path_mode = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        ).st_mode
    except FileNotFoundError:
        return []
    if not stat.S_ISREG(path_mode):
        raise OSError(
            errno.EINVAL,
            f"Refusing to rewrite non-regular env file: {path}",
            path,
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    # If a file observed above disappears here, fail rather than rebuilding it
    # from only ``updates`` and silently dropping every other secret.
    descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)

    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"Refusing to rewrite non-regular env file: {path}")
        with os.fdopen(
            descriptor,
            mode="r",
            encoding="utf-8",
            newline="",
        ) as env_file:
            descriptor = -1  # ownership moved to ``env_file``
            return env_file.readlines()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_owner_only_write(
    path: Path,
    content: str,
    *,
    parent_descriptor: int,
) -> None:
    """Atomically publish *content* from a unique mode-0600 sibling file.

    A predictable ``.env.tmp`` path can be pre-created as a symlink, and
    ``Path.write_text`` follows it.  A cryptographically random sibling is
    instead opened relative to the already validated parent descriptor with
    ``O_EXCL`` semantics.  The descriptor is explicitly restricted before any
    secret is written, flushed to disk, and then renamed over the destination.
    The directory is synced after publication so the rename is durable.  Any
    pre-publication failure leaves the old destination intact and removes the
    unpublished temporary file.

    Replacing a path that is itself a single-file bind mount fails with
    ``EBUSY`` on Linux.  That failure is deliberate: silently falling back to a
    truncate/write cycle would expose a partial secret file to other processes.
    Production therefore mounts the containing runtime-config directory.
    """

    descriptor = -1
    temporary_name: str | None = None
    try:
        temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(
            descriptor,
            mode="w",
            encoding="utf-8",
            newline="",
        ) as env_file:
            descriptor = -1  # ownership moved to ``env_file``
            env_file.write(content)
            env_file.flush()
            os.fsync(env_file.fileno())
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_name = None
        try:
            os.fsync(parent_descriptor)
        except OSError as exc:
            unsupported = {errno.EINVAL, errno.EBADF}
            if hasattr(errno, "ENOTSUP"):
                unsupported.add(errno.ENOTSUP)
            if exc.errno not in unsupported:
                raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass


def write_keys(updates: dict[str, str]) -> None:
    """Write *updates* (``{KEY: value}``) into the .env file.

    Existing keys are updated in-place; new keys are appended.  The complete
    file is replaced atomically from an owner-only sibling temporary file.

    Rejects values containing ``\\n``/``\\r``: unescaped, they'd break out of
    their ``KEY=value`` line and let a saved field inject or overwrite an
    arbitrary env var (e.g. ``VITALS_SESSION_SECRET``) on the next write.
    """
    for key, value in updates.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"Value for {key!r} contains a newline character")

    path = _find_env_path()
    with _LOCK:
        parent_descriptor = _open_parent_directory(path)
        try:
            lines = _read_existing_lines(
                path,
                parent_descriptor=parent_descriptor,
            )

            remaining = set(updates.keys())
            new_lines: list[str] = []
            for line in lines:
                stripped = line.strip()
                if not stripped.startswith("#") and "=" in stripped:
                    k, _, _ = stripped.partition("=")
                    k = k.strip()
                    if k in remaining:
                        # Replace value, preserve trailing newline style.
                        nl = "\n" if not line.endswith("\r\n") else "\r\n"
                        new_lines.append(f"{k}={updates[k]}{nl}")
                        remaining.discard(k)
                        continue
                new_lines.append(line)

            # Append genuinely new keys.
            for key in remaining:
                new_lines.append(f"{key}={updates[key]}\n")

            content = "".join(new_lines)
            _atomic_owner_only_write(
                path,
                content,
                parent_descriptor=parent_descriptor,
            )
        finally:
            os.close(parent_descriptor)
