import json

from conftest import invoke

from ssh4a.profiles import fingerprint


def test_exec_streams_and_save(config, server, connection, tmp_path):
    status = tmp_path / "state.json"
    args = ["exec", *connection, "--status-file", str(status)]
    result = invoke(
        config, *args, "--", "cat; printf error >&2; exit 7", data=b"input", check=False
    )
    assert (result.returncode, result.stdout, result.stderr) == (7, b"input", b"error")
    assert json.loads(status.read_text()) == {"kind": "remote_exit", "exit_code": 7}
    result = invoke(
        config, *args, "--", "head -c 3000000 /dev/zero; head -c 3000000 /dev/zero >&2"
    )
    assert (len(result.stdout), len(result.stderr)) == (3000000, 3000000)
    result = invoke(config, *args, "--", "exit 255", check=False)
    assert result.returncode == 255
    assert json.loads(status.read_text()) == {"kind": "remote_exit", "exit_code": 255}
    failed_auth = invoke(
        config,
        "exec",
        "--profile",
        "a",
        "--save-as",
        "never",
        "--status-file",
        str(status),
        "--",
        "true",
        check=False,
    )
    assert b"[ssh4a:authentication]" in failed_auth.stderr
    assert json.loads(status.read_text())["error"] == "authentication"
    assert invoke(config, "profile", "show", "never", check=False).returncode == 255
    invoke(config, *args, "--save-as", "metadata", "--", "true")
    assert not json.loads(invoke(config, "profile", "show", "metadata").stdout)[
        "has_password"
    ]
    invoke(config, *args, "--save-as", "stored", "--save-password", "--", "true")
    assert (
        invoke(config, "exec", "--profile", "stored", "--", "id -un").stdout
        == b"fixture\n"
    )


def test_wrong_host_fingerprint_and_timeouts(config, server, connection, tmp_path):
    status = tmp_path / "state.json"
    args = ["exec", *connection, "--status-file", str(status)]
    result = invoke(config, *args, "--timeout", "0.1", "--", "sleep 5", check=False)
    assert result.returncode == 255
    assert json.loads(status.read_text())["error"] == "timeout"
    invoke(
        config,
        "trust",
        "set",
        "--host",
        "127.0.0.1",
        "--port",
        "1",
        "--fingerprint",
        server.fingerprint,
    )
    result = invoke(config, *args, "--port", "1", "--", "true", check=False)
    assert result.returncode == 255
    assert json.loads(status.read_text())["error"] == "connection"
    invoke(
        config,
        "trust",
        "set",
        "--host",
        "127.0.0.1",
        "--port",
        server.port,
        "--fingerprint",
        fingerprint(b"wrong key"),
    )
    result = invoke(config, "exec", "--profile", "a", "--", "true", check=False)
    assert b"[ssh4a:host_key]" in result.stderr
