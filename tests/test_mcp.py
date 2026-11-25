import asyncio
import re
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager

import httpx2
import pytest
from conftest import PASSWORD, invoke
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


@pytest.fixture
def mcp_url(config):
    with socket.socket() as selected:
        selected.bind(("127.0.0.1", 0))
        port = selected.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    with subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ssh4a.cli",
            "--config-dir",
            str(config),
            "mcp",
            "serve",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail(process.stderr.read().decode())
                try:
                    if httpx2.get(url, timeout=0.2, trust_env=False).status_code == 401:
                        break
                except httpx2.HTTPError:
                    pass
                time.sleep(0.03)
            else:
                pytest.fail("MCP server failed to start")
            yield url
        finally:
            process.terminate()
            process.wait(timeout=5)
            logs = process.stderr.read()
            assert PASSWORD.encode() not in logs


@asynccontextmanager
async def authenticated(url, token, *, mode="auto"):
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, trust_env=False
        ) as http,
        Client(streamable_http_client(url, http_client=http), mode=mode) as client,
    ):
        yield client


async def call(client, name, /, **args):
    result = await client.call_tool(name, args)
    assert not result.is_error, result
    return result.structured_content


async def read_until(client, session, cursor, expected):
    collected = ""
    for _ in range(25):
        response = await call(
            client, "shell_read", session_id=session, cursor=cursor, wait=0.2
        )
        collected += response["text"]
        cursor = response["cursor"]
        if expected in collected:
            return cursor, collected
    pytest.fail(f"Expected {expected!r} in PTY output, got {collected[-512:]!r}")


def test_connections_tokens_transfer_and_shell(
    config, connection, server, mcp_url, tmp_path
):
    credentials = config / "mcp" / "tokens.json"
    first = invoke(config, "mcp", "token", "create", "agent-a").stdout.decode().strip()
    second = invoke(config, "mcp", "token", "create", "agent-b").stdout.decode().strip()
    assert first != second
    assert first not in credentials.read_text()
    assert first not in (config / "profiles.json").read_text()
    assert PASSWORD not in credentials.read_text()
    assert (credentials.stat().st_mode & 0o777) == 0o600
    assert httpx2.get(mcp_url, trust_env=False).status_code == 401
    assert (
        httpx2.get(
            mcp_url, trust_env=False, headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    assert (
        httpx2.post(
            mcp_url,
            json={},
            trust_env=False,
            headers={
                "Authorization": f"Bearer {first}",
                "Origin": "https://different-origin.example",
            },
        ).status_code
        == 403
    )
    busy = invoke(
        config,
        "mcp",
        "serve",
        "--port",
        mcp_url.split(":")[-1].split("/")[0],
        check=False,
    )
    assert b"Could not listen" in busy.stderr

    async def exercise():
        async with (
            authenticated(mcp_url, first) as agent,
            authenticated(mcp_url, second, mode="legacy") as other,
        ):
            assert {"exec", "shell_send_secret"} <= {
                tool.name for tool in (await agent.list_tools()).tools
            }
            assert {"exec", "shell_send_secret"} <= {
                tool.name for tool in (await other.list_tools()).tools
            }
            profiles = await call(agent, "profile_list")
            assert {profile["name"] for profile in profiles["profiles"]} == {"a", "b"}
            assert PASSWORD not in str(profiles)
            revealed = await call(agent, "profile_reveal", name="b", field="password")
            assert revealed["value"] == PASSWORD
            target = {"profile": "a", "password_ref": "b"}
            executed = await call(
                agent,
                "exec",
                connection=target,
                command="cat; echo error >&2; exit 7",
                stdin="input",
            )
            assert (executed["exit_code"], executed["stdout"], executed["stderr"]) == (
                7,
                "input",
                "error\n",
            )
            invoke(
                config,
                "profile",
                "update",
                "a",
                "--password-stdin",
                data=PASSWORD.encode(),
            )
            assert (
                await call(agent, "exec", connection={"profile": "a"}, command="true")
            )["exit_code"] == 0

            source, destination = tmp_path / "source", tmp_path / "download"
            remote = "/home/fixture/mcp-fixture"
            source.write_bytes(b"\x00\x01mcp test")
            assert (
                await call(
                    agent,
                    "file_put",
                    connection=target,
                    local=str(source),
                    remote=remote,
                )
            )["ok"]
            assert (
                await call(
                    other,
                    "file_get",
                    connection=target,
                    remote=remote,
                    local=str(destination),
                )
            )["ok"]
            assert source.read_bytes() == destination.read_bytes()

            opened, next_opened = await asyncio.gather(
                call(agent, "shell_open", connection=target),
                call(other, "shell_open", connection=target),
            )
            shell_id, other_id = opened["session_id"], next_opened["session_id"]
            cursor, _ = await read_until(agent, shell_id, 0, "fixture@")
            other_cursor, _ = await read_until(other, other_id, 0, "fixture@")
            assert (await call(other, "shell_write", session_id=shell_id, text="bad"))[
                "error"
            ] == "session"
            await call(
                agent,
                "shell_write",
                session_id=shell_id,
                text="cd /tmp; export SSH4A_VAR=yes\n",
            )
            await call(
                other,
                "shell_write",
                session_id=other_id,
                text="cd /; export SSH4A_VAR=no\n",
            )
            command = """printf 'PWD=%s:%s\\n' "$PWD" "$SSH4A_VAR"\n"""
            await call(agent, "shell_write", session_id=shell_id, text=command)
            await call(other, "shell_write", session_id=other_id, text=command)
            cursor, _ = await read_until(agent, shell_id, cursor, "PWD=/tmp:yes")
            other_cursor, _ = await read_until(
                other, other_id, other_cursor, "PWD=/:no"
            )
            assert (
                await call(
                    agent, "shell_resize", session_id=shell_id, width=101, height=41
                )
            )["ok"]
            await call(
                agent,
                "shell_write",
                session_id=shell_id,
                text="""stty size; read -rsp "Pass""_prompt: " answer; printf '\\nlength=%s\\n' "${#answer}"\n""",
            )
            cursor, output = await read_until(agent, shell_id, cursor, "Pass_prompt: ")
            assert "41 101" in output
            assert (
                await call(
                    agent,
                    "shell_send_secret",
                    session_id=shell_id,
                    password_ref="b",
                    enter=True,
                )
            )["ok"]
            cursor, output = await read_until(
                agent, shell_id, cursor, f"length={len(PASSWORD)}"
            )
            assert PASSWORD not in output
            assert (await call(other, "shell_close", session_id=other_id))["ok"]
            await call(
                agent,
                "shell_write",
                session_id=shell_id,
                text="printf 'REMOTE_%s=%s\\n' PID $$\n",
            )
            cursor, output = await read_until(agent, shell_id, cursor, "REMOTE_PID=")
            remote_pid = re.search(r"REMOTE_PID=([0-9]+)", output)
            assert remote_pid is not None
            invoke(config, "mcp", "token", "revoke", "agent-a")
            assert (
                httpx2.get(
                    mcp_url,
                    trust_env=False,
                    headers={"Authorization": f"Bearer {first}"},
                ).status_code
                == 401
            )
            assert (
                await call(other, "exec", connection={"profile": "a"}, command="true")
            )["exit_code"] == 0
            assert (
                await call(other, "shell_read", session_id=shell_id, cursor=cursor)
            )["error"] == "session"
            for _ in range(25):
                gone = await call(
                    other,
                    "exec",
                    connection={"profile": "a"},
                    command="test ! -e /proc/" + remote_pid.group(1),
                )
                if gone["exit_code"] == 0:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("The revoked client's SSH process is still running")

    asyncio.run(exercise())
