import fcntl
import hashlib
import json
import os
import select
import signal
import socket
import stat
import struct
import sys
import termios
import tty
from contextlib import contextmanager
from pathlib import Path

import paramiko

from .errors import AppError
from .profiles import Profiles, alias
from .ssh import CHUNK_SIZE, deadline
from .storage import PrivateDirectory

MAX_CONTROL_REQUEST = 512


def open_pty(transport, term: str, width: int, height: int, timeout: float = 10):
    if (
        not term
        or len(term) > 128
        or not 1 <= width <= 65535
        or not 1 <= height <= 65535
    ):
        raise AppError("input", "Invalid terminal type or size")
    with deadline(transport, timeout, "Opening the remote PTY timed out"):
        try:
            channel = transport.open_session(timeout=timeout)
            channel.settimeout(timeout)
            channel.get_pty(term=term, width=width, height=height)
            channel.invoke_shell()
            return channel
        except (OSError, EOFError, paramiko.SSHException) as exc:
            raise AppError("connection", "Could not open the remote PTY") from exc


def send_bytes(channel, data: bytes) -> None:
    try:
        channel.sendall(data)
    except (OSError, EOFError, paramiko.SSHException) as exc:
        raise AppError("session", "Could not send input to the remote PTY") from exc


def secret_bytes(store: Profiles, reference: str, enter: bool) -> bytes:
    return store.reveal(reference, "password").encode() + (b"\r" if enter else b"")


def control_path(store: Profiles, name: str) -> Path:
    alias(name)
    config_dir = store.directory.path
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
    folder = runtime / "ssh4a" if runtime.is_absolute() else config_dir / "run"
    digest = hashlib.sha256(f"{os.geteuid()}:{config_dir}:{name}".encode()).hexdigest()[
        :32
    ]
    path = folder / f"{digest}.sock"
    if len(os.fsencode(path)) >= 108:
        raise AppError(
            "session",
            "Socket path is too long; use a shorter XDG_RUNTIME_DIR or configuration path",
        )
    return path


@contextmanager
def control_socket(store: Profiles, name: str | None):
    if name is None:
        yield None
        return
    path = control_path(store, name)
    with PrivateDirectory(path.parent).open(write=True) as folder:
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        lock = os.open(path.with_suffix(".lock").name, flags, 0o600, dir_fd=folder)
        try:
            PrivateDirectory.check_file(folder, lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AppError(
                    "session", f"An active session already uses {name}"
                ) from exc
            try:
                existing = path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (
                    not stat.S_ISSOCK(existing.st_mode)
                    or existing.st_uid != os.geteuid()
                ):
                    raise AppError("session", "Unsafe session socket path")
                path.unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                try:
                    path.chmod(0o600)
                    listener.listen(8)
                    yield listener
                finally:
                    path.unlink(missing_ok=True)
        finally:
            os.close(lock)


def peer_uid(sock: socket.socket) -> int:
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)[1]


def read_frame(sock: socket.socket) -> dict:
    with sock.makefile("rb") as incoming:
        frame = incoming.readline(MAX_CONTROL_REQUEST + 1)
    if len(frame) > MAX_CONTROL_REQUEST or not frame.endswith(b"\n"):
        raise AppError("session", "Invalid session control message")
    try:
        result = json.loads(frame)
    except (ValueError, UnicodeError) as exc:
        raise AppError("session", "Invalid session control message") from exc
    if not isinstance(result, dict):
        raise AppError("session", "Invalid session control message")
    return result


def send_frame(sock: socket.socket, result: dict) -> None:
    sock.sendall(json.dumps(result).encode() + b"\n")


def serve_control(listener, store, channel):
    client, _ = listener.accept()
    with client:
        client.settimeout(0.5)
        try:
            if peer_uid(client) != os.geteuid():
                raise AppError(
                    "session", "This terminal belongs to a different local user"
                )
            request = read_frame(client)
            reference = request.get("reference")
            enter = request.get("enter")
            if not isinstance(reference, str) or type(enter) is not bool:
                raise AppError("session", "Invalid session control message")
            send_bytes(channel, secret_bytes(store, reference, enter))
            send_frame(client, {"ok": True})
        except AppError as exc:
            try:
                send_frame(client, {"error": exc.kind, "message": str(exc)})
            except OSError:
                pass
        except OSError:
            pass


def send_secret(store: Profiles, name: str, reference: str, enter: bool):
    alias(reference)
    path = control_path(store, name)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(12)
            client.connect(str(path))
            if peer_uid(client) != os.geteuid():
                raise AppError(
                    "session", "This terminal belongs to a different local user"
                )
            send_frame(client, {"reference": reference, "enter": enter})
            result = read_frame(client)
    except OSError as exc:
        raise AppError("session", f"Session is unavailable: {name}") from exc
    if not result.get("ok"):
        raise AppError(
            result.get("error", "session"),
            result.get("message", "Session request failed"),
        )


class TerminalSignal(Exception):
    def __init__(self, number: int):
        self.number = number


@contextmanager
def terminal_mode(fd: int):
    original = termios.tcgetattr(fd)
    previous = {}
    resize = False

    def handle(number, _frame):
        nonlocal resize
        if number == signal.SIGWINCH:
            resize = True
        else:
            raise TerminalSignal(number)

    def take_resize():
        nonlocal resize
        value, resize = resize, False
        return value

    try:
        for number in (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, handle)
        tty.setraw(fd)
        yield take_resize
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)


def terminal_size(fd: int) -> tuple[int, int]:
    size = os.get_terminal_size(fd)
    return size.columns or 80, size.lines or 24


def require_terminal():
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise AppError("input", "An interactive local terminal is required")


def run_terminal(transport, store: Profiles, name: str | None, timeout: float) -> int:
    require_terminal()
    fd = sys.stdin.fileno()
    width, height = terminal_size(fd)
    try:
        with (
            control_socket(store, name) as listener,
            open_pty(
                transport, os.environ.get("TERM") or "xterm", width, height, timeout
            ) as channel,
            terminal_mode(fd) as take_resize,
        ):
            return relay_terminal(
                fd, channel, listener, store, take_resize, (width, height)
            )
    except TerminalSignal as exc:
        return 128 + exc.number
    except (EOFError, paramiko.SSHException) as exc:
        raise AppError("connection", "Interactive SSH channel failed") from exc


def relay_terminal(fd, channel, listener, store, take_resize, current_size) -> int:
    readers = [fd, channel]
    if listener is not None:
        readers.append(listener)

    def sync_size():
        nonlocal current_size
        new_size = terminal_size(fd)
        if new_size != current_size and not channel.closed:
            channel.resize_pty(*new_size)
            current_size = new_size

    sync_size()
    while True:
        if take_resize():
            sync_size()
        had_output = channel.recv_ready()
        if had_output:
            sys.stdout.buffer.write(channel.recv(CHUNK_SIZE))
            sys.stdout.buffer.flush()
        if channel.closed and not channel.recv_ready():
            status = channel.recv_exit_status()
            if status < 0:
                raise AppError("connection", "Interactive SSH channel disconnected")
            return status
        ready, _, _ = select.select(readers, [], [], 0 if had_output else 0.1)
        if take_resize():
            sync_size()
        if fd in ready:
            data = os.read(fd, CHUNK_SIZE)
            if not data:
                channel.shutdown_write()
                readers.remove(fd)
            else:
                send_bytes(channel, data)
        if listener in ready:
            serve_control(listener, store, channel)
