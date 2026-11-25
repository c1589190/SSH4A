import hashlib
import hmac
import json
import secrets
from pathlib import Path
from uuid import uuid4

from .errors import AppError
from .profiles import alias
from .storage import PrivateDirectory, atomic_write, json_data

TOKEN_FILE = "tokens.json"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Tokens:
    def __init__(self, config_dir: Path):
        self.config = PrivateDirectory(config_dir)
        self.directory = PrivateDirectory(config_dir / "mcp")

    def load(self, fd: int | None) -> dict:
        raw = self.directory.read(fd, TOKEN_FILE)
        if raw is None:
            return {}
        try:
            data = json.loads(raw)
            if data["version"] != 1:
                raise ValueError
            tokens = data["tokens"]
            for record in tokens.values():
                if not isinstance(record["id"], str) or not isinstance(
                    record["digest"], str
                ):
                    raise TypeError
            return tokens
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AppError("storage", "Invalid MCP token configuration") from exc

    def snapshot(self) -> dict:
        with self.config.open(), self.directory.open() as fd:
            return self.load(fd)

    def list(self) -> list[str]:
        return sorted(self.snapshot())

    def create(self, name: str) -> str:
        alias(name)
        with self.config.open(write=True), self.directory.locked() as fd:
            records = self.load(fd)
            if name in records:
                raise AppError("mcp_token", f"A token already exists for {name}")
            token = secrets.token_urlsafe(32)
            records[name] = {"id": uuid4().hex, "digest": token_digest(token)}
            atomic_write(fd, TOKEN_FILE, json_data({"version": 1, "tokens": records}))
            return token

    def revoke(self, name: str) -> None:
        alias(name)
        with self.config.open(write=True), self.directory.locked() as fd:
            records = self.load(fd)
            if name not in records:
                raise AppError("mcp_token", f"No token exists for {name}")
            del records[name]
            atomic_write(fd, TOKEN_FILE, json_data({"version": 1, "tokens": records}))

    def authenticate(self, authorization: str) -> str | None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or len(token) != 43 or not token.isascii():
            return None
        digest = token_digest(token)
        for record in self.snapshot().values():
            if hmac.compare_digest(digest, record["digest"]):
                return record["id"]
        return None

    def active_ids(self) -> set[str]:
        return {record["id"] for record in self.snapshot().values()}
