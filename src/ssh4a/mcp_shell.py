import select
import threading
import time
from base64 import b64encode
from contextlib import ExitStack
from uuid import uuid4

import paramiko

from .errors import AppError
from .profiles import Connection, Profiles
from .shell import open_pty, secret_bytes, send_bytes
from .ssh import CHUNK_SIZE, connect
from .tokens import Tokens

BUFFER_LIMIT = 256 * 1024
MAX_SESSIONS = 16
IDLE_SECONDS = 900


class ShellSession:
    def __init__(self, owner: str, resources: ExitStack, channel):
        self.owner = owner
        self.resources = resources
        self.channel = channel
        self.condition = threading.Condition()
        self.write_lock = threading.Lock()
        self.last_access = time.monotonic()
        self.buffer = bytearray()
        self.offset = 0
        self.closed = False
        self.status = None
        self.stop = threading.Event()
        self.reader = threading.Thread(target=self.read_remote, daemon=True)
        self.reader.start()

    def read_remote(self):
        try:
            while not self.stop.is_set():
                if self.channel.recv_ready():
                    data = self.channel.recv(CHUNK_SIZE)
                    with self.condition:
                        self.buffer.extend(data)
                        discarded = max(0, len(self.buffer) - BUFFER_LIMIT)
                        del self.buffer[:discarded]
                        self.offset += discarded
                        self.condition.notify_all()
                    continue
                if self.channel.closed:
                    status = self.channel.recv_exit_status()
                    self.status = status if status >= 0 else None
                    break
                select.select([self.channel], [], [], 0.1)
        except (OSError, EOFError, paramiko.SSHException):
            pass
        finally:
            with self.condition:
                self.closed = True
                self.condition.notify_all()
            self.resources.close()

    def read(self, cursor: int, limit: int, wait: float) -> dict:
        with self.condition:
            end = self.offset + len(self.buffer)
            if cursor > end:
                raise AppError(
                    "session", "The output cursor is beyond the available stream"
                )
            self.condition.wait_for(
                lambda: self.closed or cursor < self.offset + len(self.buffer),
                timeout=wait,
            )
            start = max(cursor, self.offset)
            index = start - self.offset
            data = bytes(self.buffer[index : index + limit])
            next_cursor = start + len(data)
            return {
                "text": data.decode(errors="replace"),
                "data_base64": b64encode(data).decode("ascii"),
                "cursor": next_cursor,
                "truncated": cursor < self.offset,
                "closed": self.closed,
                "exit_code": self.status,
            }

    def write(self, data: bytes):
        with self.write_lock:
            if self.closed or self.channel.closed:
                raise AppError("session", "The SSH session is closed")
            send_bytes(self.channel, data)

    def resize(self, width: int, height: int):
        with self.write_lock:
            if self.closed or self.channel.closed:
                raise AppError("session", "The SSH session is closed")
            try:
                self.channel.resize_pty(width, height)
            except (OSError, EOFError, paramiko.SSHException) as exc:
                raise AppError("session", "Failed to resize the remote PTY") from exc

    def close(self):
        self.stop.set()
        self.channel.close()
        self.reader.join(timeout=2)


class ShellSessions:
    def __init__(self, profiles: Profiles, tokens: Tokens):
        self.profiles = profiles
        self.tokens = tokens
        self.sessions: dict[str, ShellSession] = {}
        self.guard = threading.Lock()
        self.pending = 0
        self.stop = threading.Event()
        self.reaper = threading.Thread(target=self.clean_up, daemon=True)

    def __enter__(self):
        self.reaper.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.reaper.join(timeout=3)
        with self.guard:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.close()

    def create(
        self, owner: str, options: Connection, term: str, width: int, height: int
    ) -> str:
        with self.guard:
            if len(self.sessions) + self.pending >= MAX_SESSIONS:
                raise AppError(
                    "session", "The MCP service has reached its SSH shell limit"
                )
            self.pending += 1

        try:
            with ExitStack() as resources:
                target = self.profiles.resolve(options)
                transport = resources.enter_context(connect(target))
                channel = resources.enter_context(
                    open_pty(transport, term, width, height)
                )
                session_id = uuid4().hex
                with self.guard:
                    if self.stop.is_set() or owner not in self.tokens.active_ids():
                        raise AppError(
                            "mcp_token", "The MCP client credential is no longer active"
                        )
                    self.sessions[session_id] = ShellSession(
                        owner, resources.pop_all(), channel
                    )
                return session_id
        finally:
            with self.guard:
                self.pending -= 1

    def lookup(
        self, owner: str, session_id: str, *, remove: bool = False
    ) -> ShellSession:
        with self.guard:
            session = self.sessions.get(session_id)
            if session is None or session.owner != owner:
                raise AppError(
                    "session", "No active shell with that ID for this MCP client"
                )
            session.last_access = time.monotonic()
            if remove:
                del self.sessions[session_id]
            return session

    def close(self, owner: str, session_id: str):
        self.lookup(owner, session_id, remove=True).close()

    def send_secret(self, owner: str, session_id: str, reference: str, enter: bool):
        session = self.lookup(owner, session_id)
        session.write(secret_bytes(self.profiles, reference, enter))

    def clean_up(self):
        while not self.stop.wait(1):
            try:
                active = self.tokens.active_ids()
            except AppError:
                continue
            now = time.monotonic()
            with self.guard:
                expired = [
                    key
                    for key, session in self.sessions.items()
                    if now - session.last_access > IDLE_SECONDS
                    or session.owner not in active
                ]
                sessions = [self.sessions.pop(key) for key in expired]
            for session in sessions:
                session.close()
