import errno
import os
import posixpath
import secrets
import shutil
import stat
import tempfile
from pathlib import Path

import paramiko

from .errors import AppError
from .ssh import CHUNK_SIZE, deadline


def remote_temporary(target: str) -> str:
    if not posixpath.basename(target) or posixpath.basename(target) in (".", ".."):
        raise AppError("input", "Remote path must name a file")
    parent = posixpath.dirname(target)
    return posixpath.join(parent, f".ssh4a-{secrets.token_hex(12)}.part")


def transfer_error(error: Exception, phase: str) -> AppError:
    if isinstance(error, AppError):
        return error
    if phase == "publishing" and (
        isinstance(error, (EOFError, TimeoutError, paramiko.SSHException))
    ):
        return AppError(
            "transfer_unconfirmed",
            "The connection ended before publication could be confirmed",
        )
    if isinstance(error, OSError) and error.errno == errno.EEXIST:
        return AppError(
            "file_exists", "Target already exists; use --overwrite to replace it"
        )
    return AppError(
        "transfer", str(error) if isinstance(error, OSError) else "SFTP channel failed"
    )


def upload(client, source: Path, target: str, overwrite: bool) -> None:
    stage = remote_temporary(target)
    created = False
    phase = "copying"
    try:
        with source.open("rb") as incoming:
            if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                raise AppError("transfer", "Upload source must be a regular file")
            with client.open(stage, "wx") as outgoing:
                created = True
                client.chmod(stage, 0o600)
                shutil.copyfileobj(incoming, outgoing, CHUNK_SIZE)
        phase = "publishing"
        if overwrite:
            client.posix_rename(stage, target)
        else:
            try:
                client.rename(stage, target)
            except OSError as error:
                try:
                    client.lstat(target)
                except OSError:
                    raise error from None
                raise AppError(
                    "file_exists",
                    "Target already exists; use --overwrite to replace it",
                ) from error
    except (AppError, OSError, EOFError, paramiko.SSHException) as error:
        failure = transfer_error(error, phase)
        if created:
            try:
                client.remove(stage)
            except (OSError, EOFError, paramiko.SSHException) as cleanup_error:
                if getattr(cleanup_error, "errno", None) != errno.ENOENT:
                    failure = AppError(
                        failure.kind, f"{failure}. Could not confirm cleanup of {stage}"
                    )
        raise failure from error


def download(client, source: str, target: Path, overwrite: bool) -> None:
    if not stat.S_ISREG(client.stat(source).st_mode or 0):
        raise AppError("transfer", "Download source must be a regular file")
    fd, stage = tempfile.mkstemp(prefix=".ssh4a-", suffix=".part", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as outgoing, client.open(source, "rb") as incoming:
            shutil.copyfileobj(incoming, outgoing, CHUNK_SIZE)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if overwrite:
            os.replace(stage, target)
        else:
            os.link(stage, target)
    finally:
        try:
            os.unlink(stage)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise AppError(
                "transfer", f"Could not clean up temporary file {stage}"
            ) from exc


def copy_file(
    transport, operation: str, local: Path, remote: str, overwrite: bool, timeout: float
):
    with deadline(transport, timeout, "SFTP operation timed out"):
        try:
            client = transport.open_sftp_client()
        except (OSError, EOFError, paramiko.SSHException) as exc:
            raise AppError(
                "sftp_unavailable", "Could not open the server's SFTP subsystem"
            ) from exc
        with client:
            try:
                if operation == "put":
                    upload(client, local, remote, overwrite)
                else:
                    download(client, remote, local, overwrite)
            except (OSError, EOFError, paramiko.SSHException) as exc:
                raise transfer_error(exc, "copying") from exc
