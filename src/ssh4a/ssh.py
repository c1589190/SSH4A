import hmac
import math
import os
import select
import socket
import threading
import time
from contextlib import contextmanager
from typing import BinaryIO

import paramiko

from .errors import AppError
from .profiles import Target, fingerprint

CHUNK_SIZE = 65536


def positive_timeout(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise AppError("input", "Timeout must be a finite positive number of seconds")
    return value


@contextmanager
def deadline(transport: paramiko.Transport, seconds: float, message: str):
    expired = threading.Event()

    def expire():
        expired.set()
        transport.close()

    timer = threading.Timer(positive_timeout(seconds), expire)
    timer.daemon = True
    timer.start()
    try:
        yield
    except Exception as error:
        if expired.is_set() and not isinstance(error, AppError):
            raise AppError("timeout", message) from error
        if (
            expired.is_set()
            and isinstance(error, AppError)
            and error.kind == "connection"
        ):
            raise AppError("timeout", message) from error
        raise
    finally:
        timer.cancel()
    if expired.is_set():
        raise AppError("timeout", message)


@contextmanager
def connect(target: Target, timeout: float = 10, *, allow_first_use: bool = False):
    timeout = positive_timeout(timeout)
    if target.fingerprint is None and not allow_first_use:
        raise AppError(
            "host_key", f"No trusted SSH fingerprint for {target.host}:{target.port}"
        )
    try:
        sock = socket.create_connection((target.host, target.port), timeout=timeout)
    except TimeoutError as exc:
        raise AppError("timeout", "SSH connection timed out") from exc
    except OSError as exc:
        raise AppError(
            "connection", f"Could not connect to {target.host}:{target.port}"
        ) from exc

    with sock, paramiko.Transport(sock) as transport:
        transport.banner_timeout = timeout
        transport.auth_timeout = timeout
        try:
            with deadline(
                transport, timeout, "SSH negotiation or authentication timed out"
            ):
                transport.start_client()
                observed = fingerprint(transport.get_remote_server_key().asbytes())
                if target.fingerprint is not None and not hmac.compare_digest(
                    observed, target.fingerprint
                ):
                    raise AppError(
                        "host_key",
                        f"Unexpected SSH fingerprint for {target.host}:{target.port} ({observed})",
                    )
                transport.auth_password(target.user, target.password, fallback=False)
                if not transport.is_authenticated():
                    raise AppError(
                        "authentication", "Server requires additional authentication"
                    )
        except paramiko.AuthenticationException as exc:
            raise AppError(
                "authentication", "SSH password authentication failed"
            ) from exc
        except (OSError, EOFError, paramiko.SSHException) as exc:
            raise AppError("connection", "SSH negotiation failed") from exc
        yield transport


def input_chunk(source: BinaryIO) -> bytes | None:
    try:
        fd = source.fileno()
    except (AttributeError, OSError):
        return source.read(CHUNK_SIZE)
    if select.select([fd], [], [], 0)[0]:
        return os.read(fd, CHUNK_SIZE)
    return None


def run_command(
    transport: paramiko.Transport,
    command: str,
    source: BinaryIO | None,
    stdout: BinaryIO,
    stderr: BinaryIO,
    timeout: float = 60,
) -> int:
    if not command:
        raise AppError("input", "A remote command is required")
    with deadline(transport, timeout, "Remote command timed out"):
        try:
            with transport.open_session(timeout=timeout) as channel:
                channel.exec_command(command)
                channel.settimeout(0)
                pending = b""
                eof = False
                while True:
                    progressed = False
                    for available, receive, sink in (
                        (channel.recv_ready, channel.recv, stdout),
                        (channel.recv_stderr_ready, channel.recv_stderr, stderr),
                    ):
                        if available():
                            sink.write(receive(CHUNK_SIZE))
                            sink.flush()
                            progressed = True
                    if channel.closed and not (
                        channel.recv_ready() or channel.recv_stderr_ready()
                    ):
                        status = channel.recv_exit_status()
                        if status == -1:
                            raise AppError(
                                "connection",
                                "SSH channel closed without an exit status",
                            )
                        return status
                    if not eof:
                        if not pending:
                            data = input_chunk(source) if source is not None else b""
                            if data == b"":
                                channel.shutdown_write()
                                eof = True
                            elif data is not None:
                                pending = data
                        if pending and channel.send_ready():
                            size = channel.send(pending)
                            pending = pending[size:]
                            progressed = progressed or size > 0
                    if not progressed:
                        time.sleep(0.01)
        except (OSError, EOFError, paramiko.SSHException) as exc:
            raise AppError("connection", "Remote command channel failed") from exc
