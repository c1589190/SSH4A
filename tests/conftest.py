import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

PASSWORD = "ssh4a-fixture-only"


def invoke(config, *args, data=b"", check=True):
    result = subprocess.run(
        [sys.executable, "-m", "ssh4a.cli", "--config-dir", str(config), *args],
        check=False,
        input=data,
        capture_output=True,
        timeout=15,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr.decode(errors="replace"))
    return result


@dataclass
class Server:
    container: str
    port: str
    fingerprint: str

    def trust(self, config):
        invoke(
            config,
            "trust",
            "set",
            "--host",
            "127.0.0.1",
            "--port",
            self.port,
            "--fingerprint",
            self.fingerprint,
        )

    def profile(self, config, name="a", password=PASSWORD):
        invoke(
            config,
            "profile",
            "add",
            name,
            "--host",
            "127.0.0.1",
            "--port",
            self.port,
            "--user",
            "fixture",
            "--password-stdin",
            data=password.encode(),
        )


@contextmanager
def start_server(*, sftp=True):
    if os.environ.get("SSH4A_TEST_DOCKER") != "1":
        pytest.skip("Enable the loopback OpenSSH fixture with SSH4A_TEST_DOCKER=1")
    args = ["docker", "run", "--rm", "-d", "-p", "127.0.0.1::22"]
    if not sftp:
        args.extend(["-e", "SSH4A_TEST_NO_SFTP=1"])
    container = subprocess.check_output(
        [*args, "ssh4a-fixture:local"], text=True
    ).strip()
    try:
        mapped = subprocess.check_output(
            ["docker", "port", container, "22/tcp"], text=True
        )
        port = mapped.strip().rsplit(":", 1)[1]
        for _ in range(80):
            try:
                with socket.create_connection(
                    ("127.0.0.1", int(port)), timeout=0.2
                ) as sock:
                    if sock.recv(30).startswith(b"SSH-"):
                        break
            except OSError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("OpenSSH fixture failed to start")
        key = subprocess.check_output(
            [
                "docker",
                "exec",
                container,
                "ssh-keygen",
                "-lf",
                "/run/sshd/host_key.pub",
            ],
            text=True,
        )
        yield Server(container, port, key.split()[1])
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], check=False, stdout=subprocess.DEVNULL
        )


@pytest.fixture(scope="session")
def server():
    with start_server() as active:
        yield active


@pytest.fixture
def config(tmp_path):
    return tmp_path / "config"


@pytest.fixture
def connection(config, server):
    server.trust(config)
    server.profile(config, "a", "wrong-fixture-password")
    invoke(
        config,
        "profile",
        "add",
        "b",
        "--host",
        "192.0.2.9",
        "--user",
        "other",
        "--password-stdin",
        data=PASSWORD.encode(),
    )
    return ["--profile", "a", "--password-ref", "b"]
