import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import invoke

from ssh4a.errors import AppError
from ssh4a.profiles import Connection, Profiles
from ssh4a.storage import config_path


def test_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_path() == tmp_path / "ssh4a"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    assert config_path().is_absolute()


def test_list_empty_config(config):
    assert invoke(config, "list").stdout == b"No saved profiles.\n"
    assert json.loads(invoke(config, "list", "--json").stdout) == []
    assert not config.exists()


def test_list_saved_profiles(config):
    store = Profiles(config)
    store.save("z-work", create=True, host="2001:db8::2", user="部署", port=2222)
    store.save(
        "a-backup",
        create=True,
        host="backup.example.org",
        user="backup",
        password="secret",
    )
    (config / "vault.key").unlink()

    table = invoke(config, "list").stdout.decode()
    assert [line.split() for line in table.splitlines()] == [
        ["NAME", "HOST", "PORT", "HAS_PASSWORD", "USER"],
        ["a-backup", "backup.example.org", "22", "yes", "backup"],
        ["z-work", "2001:db8::2", "2222", "no", "部署"],
    ]
    output = invoke(config, "list", "--json").stdout
    assert output == invoke(config, "profile", "list").stdout
    assert json.loads(output) == [
        {
            "name": "a-backup",
            "host": "backup.example.org",
            "port": 22,
            "user": "backup",
            "has_password": True,
        },
        {
            "name": "z-work",
            "host": "2001:db8::2",
            "port": 2222,
            "user": "部署",
            "has_password": False,
        },
    ]


def test_encryption_and_explicit_reveal(config):
    first = " leading 远端 trailing  "
    invoke(
        config,
        "profile",
        "add",
        "a",
        "--host",
        "192.0.2.8",
        "--user",
        "deploy",
        "--password-stdin",
        data=(first + "\r\n").encode(),
    )
    snapshot = config / "profiles.json"
    assert first not in snapshot.read_text()
    assert first.encode() not in invoke(config, "profile", "show", "a").stdout
    assert (
        invoke(config, "profile", "reveal", "a", "password").stdout
        == (first + "\n").encode()
    )
    invoke(config, "profile", "update", "a", "--password-stdin", data=b"another\n")
    assert invoke(config, "profile", "reveal", "a", "password").stdout == b"another\n"
    assert "another" not in snapshot.read_text()
    assert (config.stat().st_mode & 0o777) == 0o700
    key = config / "vault.key"
    assert (key.stat().st_mode & 0o777) == 0o600
    original = key.read_bytes()
    key.chmod(0o644)
    failure = invoke(config, "profile", "reveal", "a", "password", check=False)
    assert b"[ssh4a:credential]" in failure.stderr
    key.chmod(0o600)
    key.unlink()
    before = snapshot.read_bytes()
    changed = invoke(
        config, "profile", "update", "a", "--password-stdin", data=b"new", check=False
    )
    assert b"[ssh4a:credential]" in changed.stderr
    assert not key.exists() and snapshot.read_bytes() == before
    key.write_bytes(original)
    key.chmod(0o600)
    tampered = json.loads(before)
    value = tampered["profiles"]["a"]["password"]
    tampered["profiles"]["a"]["password"] = ("A" if value[0] != "A" else "B") + value[
        1:
    ]
    snapshot.write_text(json.dumps(tampered))
    assert (
        b"integrity"
        in invoke(config, "profile", "reveal", "a", "password", check=False).stderr
    )
    invoke(config, "profile", "delete", "a")
    assert json.loads(invoke(config, "profile", "list").stdout) == []


def test_independent_references_and_parallel_updates(config):
    store = Profiles(config)
    with ThreadPoolExecutor(2) as pool:
        operations = [
            pool.submit(
                invoke,
                config,
                "profile",
                "add",
                f"host{num}",
                "--host",
                f"192.0.2.{num}",
                "--user",
                f"user{num}",
                "--password-stdin",
                data=f"secret{num}".encode(),
            )
            for num in range(1, 5)
        ]
        for operation in operations:
            operation.result()
    assert len(store.list()) == 4
    target = store.resolve(
        Connection(host_ref="host1", user_ref="host2", password_ref="host3")
    )
    assert (target.host, target.user, target.password, target.port) == (
        "192.0.2.1",
        "user2",
        "secret3",
        22,
    )
    override = store.resolve(Connection(profile="host1", password_ref="host4"))
    assert (override.host, override.user, override.password) == (
        "192.0.2.1",
        "user1",
        "secret4",
    )
    with pytest.raises(AppError, match="one source"):
        store.resolve(Connection(host="192.0.2.1", host_ref="host1", user="user1"))
    with pytest.raises(AppError, match="no saved"):
        store.resolve(Connection(host="192.0.2.1", user="user1"))
