import fcntl
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

from .errors import AppError


def config_path(override: str | None = None) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not base.is_absolute():
        base = Path.home() / ".config"
    path = Path(override).expanduser() if override is not None else base / "ssh4a"
    return Path(os.path.abspath(path))


def atomic_write(directory: int, name: str, data: bytes) -> None:
    temporary = ".tmp-" + secrets.token_hex(12)
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory
    )
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def write_status(path: Path, data: dict) -> None:
    try:
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            atomic_write(fd, path.name, json_data(data))
        finally:
            os.close(fd)
    except OSError as exc:
        raise AppError(
            "status_file", f"Could not write execution status: {exc.strerror}"
        ) from exc


def json_data(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()


class PrivateDirectory:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def open(self, *, write: bool = False):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        except FileNotFoundError:
            if not write:
                yield None
                return
            try:
                self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
            except OSError as exc:
                raise AppError(
                    "storage", "Could not create the private directory"
                ) from exc
        except OSError as exc:
            raise AppError("storage", "Could not open the private directory") from exc

        try:
            info = os.fstat(fd)
            uid = os.geteuid()
            if info.st_mode & 0o077 or (info.st_uid != uid and (uid != 0 or write)):
                raise AppError(
                    "storage",
                    "Directory must be private and owned by the selected user",
                )
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def check_file(directory: int, fd: int) -> None:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.fstat(directory).st_uid
        ):
            raise AppError(
                "storage",
                "Configuration files must be private, regular files owned by their directory owner",
            )

    @classmethod
    def read(cls, directory: int | None, name: str) -> bytes | None:
        if directory is None:
            return None
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(name, flags, dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AppError("storage", f"Could not open {name}") from exc
        with os.fdopen(fd, "rb") as stream:
            cls.check_file(directory, fd)
            return stream.read()

    @contextmanager
    def locked(self):
        with self.open(write=True) as directory:
            try:
                flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
                lock = os.open(".lock", flags, 0o600, dir_fd=directory)
                try:
                    self.check_file(directory, lock)
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    yield directory
                finally:
                    os.close(lock)
            except OSError as exc:
                raise AppError(
                    "storage", "Could not update private configuration"
                ) from exc
