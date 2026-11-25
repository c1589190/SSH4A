import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from contextlib import contextmanager

from conftest import PASSWORD, invoke


class Terminal:
    def __init__(self, master, process):
        self.master = master
        self.process = process
        self.data = b""

    def write(self, data):
        os.write(self.master, data)

    def expect(self, data, timeout=6):
        until = time.monotonic() + timeout
        while data not in self.data:
            if time.monotonic() >= until:
                raise AssertionError(f"Expected {data!r}, got {self.data[-600:]!r}")
            if select.select([self.master], [], [], 0.1)[0]:
                self.data += os.read(self.master, 65536)
        _, self.data = self.data.split(data, 1)

    def resize(self, rows, columns):
        fcntl.ioctl(
            self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0)
        )
        self.process.send_signal(signal.SIGWINCH)


@contextmanager
def terminal(config, *args):
    fd, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

    def setup():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    before = termios.tcgetattr(slave)
    with subprocess.Popen(
        [sys.executable, "-m", "ssh4a.cli", "--config-dir", str(config), *args],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm"},
        preexec_fn=setup,  # noqa: PLW1509 - tests run this setup before starting threads
    ) as child:
        try:
            yield Terminal(fd, child), before, slave
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            os.close(slave)
            os.close(fd)


def test_shell_state_and_named_secret(config, connection):
    with terminal(config, "shell", *connection, "--session", "work") as (
        term,
        before,
        slave,
    ):
        term.expect(b"fixture@")
        term.write(b"cd /tmp; export SSH4A_MARK=retained\n")
        term.expect(b"/tmp$")
        term.write(b"""printf '%s:%s\\n' "$PWD" "$SSH4A_MARK"\n""")
        term.expect(b"/tmp:retained")
        term.resize(31, 93)
        term.write(b"stty size\n")
        term.expect(b"31 93")
        term.write(b"seq 250 | less\n")
        term.expect(b"\x1b[?1049h")
        term.write(b"q")
        term.expect(b"\x1b[?1049l")
        term.write(b"sleep 20\n")
        term.expect(b"sleep 20\r\n")
        time.sleep(0.1)
        term.write(b"\x03")
        term.expect(b"/tmp$")
        duplicate = invoke(
            config,
            "session",
            "send-secret",
            "missing",
            "--password-ref",
            "b",
            check=False,
        )
        assert b"[ssh4a:session]" in duplicate.stderr
        with terminal(config, "shell", *connection, "--session", "work") as (
            other,
            _,
            __,
        ):
            other.expect(b"An active session already uses work")
            assert other.process.wait(timeout=4) == 255
        term.write(
            b"""read -rsp "Pass""_prompt: " answer; printf '\\nlength=%s\\n' "${#answer}"\n"""
        )
        term.expect(b"Pass_prompt: ")
        invoke(config, "session", "send-secret", "work", "--password-ref", "b")
        invoke(
            config, "session", "send-secret", "work", "--password-ref", "b", "--enter"
        )
        term.expect(f"length={2 * len(PASSWORD)}".encode())
        assert PASSWORD.encode() not in term.data
        term.write(b"exit 4\n")
        assert term.process.wait(timeout=5) == 4
        assert termios.tcgetattr(slave) == before
    assert (
        invoke(
            config, "session", "send-secret", "work", "--password-ref", "b", check=False
        ).returncode
        == 255
    )


def test_terminal_menu(config, server):
    server.trust(config)
    server.profile(config)
    with terminal(config, "tui") as (term, before, slave):
        term.expect(b"Select a profile")
        term.write(b"q\n")
        assert term.process.wait(timeout=5) == 0
        assert termios.tcgetattr(slave) == before
    with terminal(config, "tui") as (term, before, slave):
        term.expect(b"Select a profile")
        term.write(b"1\n")
        term.expect(b"fixture@")
        term.process.terminate()
        assert term.process.wait(timeout=5) == 143
        assert termios.tcgetattr(slave) == before


def test_interactive_password_entry(config, server):
    server.trust(config)
    with terminal(
        config,
        "shell",
        "--host",
        "127.0.0.1",
        "--port",
        server.port,
        "--user",
        "fixture",
        "--ask-password",
        "--save-as",
        "ask",
        "--save-password",
    ) as (term, _, __):
        term.expect(b"Password: ")
        term.write(PASSWORD.encode() + b"\n")
        term.expect(b"fixture@")
        assert PASSWORD.encode() not in term.data
        term.write(b"exit\n")
        assert term.process.wait(timeout=5) == 0
    assert (
        invoke(config, "profile", "reveal", "ask", "password").stdout.strip()
        == PASSWORD.encode()
    )
