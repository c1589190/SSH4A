import io
import socket
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

from . import VERSION
from .errors import AppError
from .mcp_shell import ShellSessions
from .profiles import Connection, Profiles, port_number
from .ssh import connect, run_command
from .tokens import Tokens
from .transfer import copy_file

OUTPUT_LIMIT = 256 * 1024
Dimension = Annotated[int, Field(ge=1, le=65535)]
Seconds = Annotated[float, Field(gt=0, le=300)]
ProfileField = Literal["host", "user", "port", "password"]


class ConnectionOptions(BaseModel):
    profile: str | None = Field(default=None, description="Saved connection alias")
    host: str | None = None
    host_ref: str | None = Field(
        default=None, description="Use the host from this alias"
    )
    user: str | None = None
    user_ref: str | None = Field(
        default=None, description="Use the username from this alias"
    )
    port: int | None = Field(default=None, ge=1, le=65535)
    password_ref: str | None = Field(
        default=None, description="Use the SSH password from this alias"
    )

    def connection(self) -> Connection:
        return Connection(**self.model_dump())


class CapturedOutput(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.truncated = False

    def write(self, data):
        remaining = OUTPUT_LIMIT - self.tell()
        if len(data) > remaining:
            self.truncated = True
        super().write(data[:remaining])
        return len(data)

    def text(self):
        return self.getvalue().decode(errors="replace")


class AuthenticatedHTTP:
    def __init__(self, app, tokens: Tokens):
        self.app = app
        self.tokens = tokens

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            values = Headers(scope=scope).getlist("authorization")
            try:
                owner = (
                    await anyio.to_thread.run_sync(self.tokens.authenticate, values[0])
                    if len(values) == 1
                    else None
                )
            except (AppError, OSError):
                await PlainTextResponse("MCP token store unavailable", 503)(
                    scope, receive, send
                )
                return
            if owner is None:
                response = PlainTextResponse(
                    "Valid ssh4a MCP token required",
                    401,
                    headers={"WWW-Authenticate": 'Bearer realm="ssh4a"'},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(profiles: Profiles, tokens: Tokens, sessions: ShellSessions):
    server = MCPServer(
        name="ssh4a",
        version=VERSION,
        instructions=(
            "Use profile_list to see saved SSH aliases, and pass connection.profile for a saved "
            "connection. References can use a different profile for host, user, or password. "
            "Shell tools maintain an interactive SSH PTY across calls."
        ),
    )
    worker_limit = anyio.CapacityLimiter(16)

    async def worker(callback, *args):
        try:
            return await anyio.to_thread.run_sync(
                partial(callback, *args), limiter=worker_limit
            )
        except AppError as exc:
            return exc.status()
        except OSError:
            return AppError("io", "Could not complete a local file operation").status()

    def owner_for(header: str) -> str:
        owner = tokens.authenticate(header)
        if owner is None:
            raise AppError("mcp_token", "MCP client credential is not active")
        return owner

    async def own_shell(ctx, callback, *args):
        headers = ctx.headers
        authorization = headers.get("authorization", "") if headers is not None else ""

        def dispatch():
            return callback(owner_for(authorization), *args)

        return await worker(dispatch)

    @server.tool(structured_output=True)
    async def profile_list() -> dict[str, Any]:
        """List saved SSH connections with secret values hidden."""
        return await worker(lambda: {"profiles": profiles.list()})

    @server.tool(structured_output=True)
    async def profile_show(name: str) -> dict[str, Any]:
        """Show metadata for one saved SSH connection."""
        return await worker(profiles.show, name)

    @server.tool(structured_output=True)
    async def profile_reveal(name: str, field: ProfileField) -> dict[str, Any]:
        """Explicitly reveal the selected field in a saved profile, including a plaintext password."""
        return await worker(
            lambda: {
                "name": name,
                "field": field,
                "value": profiles.reveal(name, field),
            }
        )

    @server.tool(name="exec", structured_output=True)
    async def execute(
        connection: ConnectionOptions,
        command: str,
        stdin: str | None = None,
        timeout: Seconds = 45,
    ) -> dict[str, Any]:
        """Run a command without a PTY. Pass one command string for the remote shell."""

        def call():
            stdout, stderr = CapturedOutput(), CapturedOutput()
            source = io.BytesIO(stdin.encode()) if stdin is not None else None
            with connect(profiles.resolve(connection.connection())) as transport:
                status = run_command(
                    transport, command, source, stdout, stderr, timeout
                )
            return {
                "kind": "remote_exit",
                "exit_code": status,
                "stdout": stdout.text(),
                "stderr": stderr.text(),
                "stdout_truncated": stdout.truncated,
                "stderr_truncated": stderr.truncated,
            }

        return await worker(call)

    def copy(operation, connection, local, remote, overwrite, timeout):
        with connect(profiles.resolve(connection.connection())) as transport:
            copy_file(
                transport,
                operation,
                Path(local).expanduser(),
                remote,
                overwrite,
                timeout,
            )
        return {"ok": True}

    @server.tool(structured_output=True)
    async def file_put(
        connection: ConnectionOptions,
        local: str,
        remote: str,
        overwrite: bool = False,
        timeout: Seconds = 45,
    ) -> dict[str, Any]:
        """Upload a single file. Local paths refer to the host running the ssh4a service."""
        return await worker(copy, "put", connection, local, remote, overwrite, timeout)

    @server.tool(structured_output=True)
    async def file_get(
        connection: ConnectionOptions,
        remote: str,
        local: str,
        overwrite: bool = False,
        timeout: Seconds = 45,
    ) -> dict[str, Any]:
        """Download a single file to a path on the host running the ssh4a service."""
        return await worker(copy, "get", connection, local, remote, overwrite, timeout)

    @server.tool(structured_output=True)
    async def shell_open(
        ctx: Context,
        connection: ConnectionOptions,
        term: Annotated[str, Field(min_length=1, max_length=128)] = "xterm",
        width: Dimension = 80,
        height: Dimension = 24,
    ) -> dict[str, Any]:
        """Open a persistent SSH PTY. Use its session ID and cursor in subsequent calls."""

        def open_session(owner):
            return {
                "session_id": sessions.create(
                    owner, connection.connection(), term, width, height
                ),
                "cursor": 0,
            }

        return await own_shell(ctx, open_session)

    @server.tool(structured_output=True)
    async def shell_read(
        ctx: Context,
        session_id: str,
        cursor: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=65536)] = 16384,
        wait: Annotated[float, Field(ge=0, le=2)] = 0.2,
    ) -> dict[str, Any]:
        """Read output, then pass the returned cursor to continue without repeating output."""
        return await own_shell(
            ctx,
            lambda owner: sessions.lookup(owner, session_id).read(cursor, limit, wait),
        )

    @server.tool(structured_output=True)
    async def shell_write(
        ctx: Context, session_id: str, text: Annotated[str, Field(max_length=32768)]
    ) -> dict[str, Any]:
        """Write text and control characters to the PTY. Include a newline to submit a command."""

        def write(owner):
            sessions.lookup(owner, session_id).write(text.encode())
            return {"ok": True}

        return await own_shell(ctx, write)

    @server.tool(structured_output=True)
    async def shell_resize(
        ctx: Context, session_id: str, width: Dimension, height: Dimension
    ) -> dict[str, Any]:
        """Update the terminal dimensions of the selected remote PTY."""

        def resize(owner):
            sessions.lookup(owner, session_id).resize(width, height)
            return {"ok": True}

        return await own_shell(ctx, resize)

    @server.tool(structured_output=True)
    async def shell_send_secret(
        ctx: Context, session_id: str, password_ref: str, enter: bool = False
    ) -> dict[str, Any]:
        """Send a saved SSH password to a ready remote prompt, optionally followed by Enter."""

        def send(owner):
            sessions.send_secret(owner, session_id, password_ref, enter)
            return {"ok": True}

        return await own_shell(ctx, send)

    @server.tool(structured_output=True)
    async def shell_close(ctx: Context, session_id: str) -> dict[str, Any]:
        """Close the SSH connection and remove the selected interactive session."""

        def close(owner):
            sessions.close(owner, session_id)
            return {"ok": True}

        return await own_shell(ctx, close)

    app = server.streamable_http_app(
        stateless_http=True, json_response=True, max_request_body_size=1024 * 1024
    )
    return AuthenticatedHTTP(app, tokens)


def serve(profiles: Profiles, port: int):
    port_number(port)
    tokens = Tokens(profiles.directory.path)
    with socket.socket() as listener, ShellSessions(profiles, tokens) as sessions:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen()
        except OSError as exc:
            raise AppError(
                "service", f"Could not listen on MCP port {port}: {exc.strerror}"
            ) from exc
        app = create_app(profiles, tokens, sessions)
        config = uvicorn.Config(
            app, access_log=False, log_level="warning", lifespan="on"
        )
        server = uvicorn.Server(config)
        server.run(sockets=[listener])
        if not server.started:
            raise AppError("service", "MCP server failed to start")
