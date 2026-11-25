import json
import termios

import pytest
from conftest import PASSWORD, invoke
from test_shell import terminal

from ssh4a.cli import add_endpoint, expand_ip_shorthand
from ssh4a.errors import AppError
from ssh4a.profiles import Profiles, fingerprint


def test_add_tests_password_and_saves_only_on_success(config, server):
    args = (f"127.0.0.1:{server.port}", "fixture", "--password-stdin")
    failed = invoke(config, *args, data=b"wrong", check=False)
    assert failed.returncode != 0
    assert b"[ssh4a:authentication]" in failed.stderr
    assert not (config / "profiles.json").exists()
    assert not (config / "vault.key").exists()

    name = f"fixture-127.0.0.1-{server.port}"
    result = invoke(config, *args, data=PASSWORD.encode())
    assert name.encode() in result.stdout and PASSWORD.encode() not in result.stdout
    assert (
        invoke(config, "exec", "--profile", name, "--", "id -un").stdout == b"fixture\n"
    )
    saved = (config / "profiles.json").read_text()
    assert PASSWORD not in saved
    assert (
        json.loads(saved)["trusted_hosts"][f"127.0.0.1/{server.port}"]
        == server.fingerprint
    )


def test_add_prompts_without_echo_and_uses_requested_alias(config, server):
    with terminal(
        config, "add", "127.0.0.1", "fixture", "--port", server.port, "--name", "work"
    ) as (term, _, slave):
        term.expect(b"Password: ")
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        term.write(PASSWORD.encode() + b"\n")
        term.expect(b"Saved as work.")
        assert PASSWORD.encode() not in term.data
        assert term.process.wait(timeout=5) == 0
    assert (
        invoke(config, "exec", "--profile", "work", "--", "id -un").stdout
        == b"fixture\n"
    )


def test_add_rejects_a_changed_host_identity(config, server):
    store = Profiles(config)
    store.trust("127.0.0.1", int(server.port), fingerprint(b"previous host key"))
    before = (config / "profiles.json").read_bytes()
    result = invoke(
        config,
        "add",
        "127.0.0.1",
        "fixture",
        "--port",
        server.port,
        "--name",
        "work",
        "--password-stdin",
        data=PASSWORD.encode(),
        check=False,
    )
    assert result.returncode != 0 and b"[ssh4a:host_key]" in result.stderr
    assert (config / "profiles.json").read_bytes() == before
    assert not (config / "vault.key").exists()


@pytest.mark.parametrize(
    ("address", "port_option", "expected"),
    [
        ("192.0.2.10", None, ("192.0.2.10", 22)),
        ("192.0.2.10:62745", 62745, ("192.0.2.10", 62745)),
        ("[2001:db8::1]:62745", None, ("2001:db8::1", 62745)),
        ("2001:db8::1", 2222, ("2001:db8::1", 2222)),
    ],
)
def test_ip_shorthand_with_and_without_a_port(address, port_option, expected):
    argv = ["--config-dir", "/tmp/example", address, "fixture"]
    assert expand_ip_shorthand(argv) == [*argv[:2], "add", *argv[2:]]
    assert add_endpoint(address, port_option) == expected


def test_invalid_or_conflicting_port_fails_before_password_entry(config):
    invalid = invoke(config, "192.0.2.10:65536", "fixture", check=False)
    assert b"Port must be between 1 and 65535" in invalid.stderr
    conflict = invoke(
        config, "192.0.2.10:62745", "fixture", "--port", "22", check=False
    )
    assert b"specify different values" in conflict.stderr
    assert not config.exists()


def test_cancel_password_prompt_keeps_shell_on_next_line(config):
    with terminal(config, "192.0.2.10:62745", "fixture") as (term, _, __):
        term.expect(b"Password: ")
        term.write(b"\x03")
        term.expect(b"\r\n")
        assert term.process.wait(timeout=5) == 130
    assert not config.exists()


def test_add_commit_preserves_a_concurrently_recorded_fingerprint(config):
    store = Profiles(config)
    store.trust("192.0.2.10", 22, fingerprint(b"concurrent trust update"))
    before = (config / "profiles.json").read_bytes()

    with pytest.raises(AppError, match="changed during setup"):
        store.save(
            "work",
            create=True,
            host="192.0.2.10",
            user="fixture",
            password=PASSWORD,
            verified_fingerprint=fingerprint(b"SSH peer key"),
        )

    assert (config / "profiles.json").read_bytes() == before
    assert not (config / "vault.key").exists()
