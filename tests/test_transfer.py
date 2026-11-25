import io
from hashlib import sha256
from pathlib import Path

import pytest
from conftest import invoke, start_server

from ssh4a.errors import AppError
from ssh4a.transfer import upload


def test_round_trip_and_overwrite(config, connection, tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(bytes(range(256)) * 2048)
    target = tmp_path / "target.bin"
    remote = "/home/fixture/with space.bin"
    invoke(config, "put", *connection, str(source), remote)
    invoke(config, "get", *connection, remote, str(target))
    assert sha256(source.read_bytes()).digest() == sha256(target.read_bytes()).digest()
    original = target.read_bytes()
    source.write_bytes(b"replacement")
    denied = invoke(config, "put", *connection, str(source), remote, check=False)
    assert b"[ssh4a:file_exists]" in denied.stderr
    denied = invoke(config, "get", *connection, remote, str(target), check=False)
    assert b"[ssh4a:file_exists]" in denied.stderr
    assert target.read_bytes() == original
    invoke(config, "put", *connection, "--overwrite", str(source), remote)
    invoke(config, "get", *connection, "--overwrite", remote, str(target))
    assert target.read_bytes() == b"replacement"
    leftovers = invoke(config, "exec", *connection, "--", "find . -name '*.part'")
    assert not leftovers.stdout


def test_missing_sftp(config, tmp_path):
    with start_server(sftp=False) as server:
        server.trust(config)
        server.profile(config)
        target = tmp_path / "target"
        result = invoke(
            config, "get", "--profile", "a", "remote", str(target), check=False
        )
        assert b"[ssh4a:sftp_unavailable]" in result.stderr
        assert not target.exists()


def test_interrupted_upload_does_not_publish(tmp_path):
    class Interrupted(io.BytesIO):
        def write(self, data):
            raise EOFError("disconnected")

    class InterruptedSFTP:
        staged = ""

        def open(self, name, mode):
            assert mode == "wx"
            self.staged = name
            return Interrupted()

        def chmod(self, name, mode):
            assert name == self.staged and mode == 0o600

        def remove(self, name):
            assert name == self.staged
            raise EOFError("disconnected")

    local = tmp_path / "data"
    local.write_bytes(b"test-data")
    client = InterruptedSFTP()
    with pytest.raises(AppError, match="Could not confirm cleanup") as failed:
        upload(client, Path(local), "/destination", False)
    assert client.staged in str(failed.value)
