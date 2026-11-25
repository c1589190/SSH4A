import hashlib
import ipaddress
import json
import re
from base64 import b64decode, b64encode
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from . import credentials
from .errors import AppError
from .storage import PrivateDirectory, atomic_write, json_data

PROFILES_FILE = "profiles.json"
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}\Z")


def alias(value: str) -> str:
    if not ALIAS.fullmatch(value):
        raise AppError(
            "input", "Names must be 1–64 letters, digits, dots, underscores, or hyphens"
        )
    return value


def hostname(value: str) -> str:
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    try:
        name = value.encode("idna").decode("ascii").lower().removesuffix(".")
    except UnicodeError as exc:
        raise AppError("input", "Invalid SSH host") from exc
    if len(name) > 253 or any(not LABEL.fullmatch(label) for label in name.split(".")):
        raise AppError("input", "Invalid SSH host")
    return name


def username(value: str) -> str:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AppError("input", "Invalid SSH username")
    return value


def port_number(value: int) -> int:
    if not 1 <= value <= 65535:
        raise AppError("input", "Port must be between 1 and 65535")
    return value


def fingerprint(key_bytes: bytes) -> str:
    return "SHA256:" + b64encode(hashlib.sha256(key_bytes).digest()).decode().rstrip(
        "="
    )


def validate_fingerprint(value: str) -> str:
    if not FINGERPRINT.fullmatch(value):
        raise AppError("input", "Expected an OpenSSH SHA256 host fingerprint")
    decoded = b64decode(value.removeprefix("SHA256:") + "=", validate=True)
    if "SHA256:" + b64encode(decoded).decode().rstrip("=") != value:
        raise AppError("input", "Expected a canonical OpenSSH SHA256 host fingerprint")
    return value


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    fingerprint: str | None = None


def default_profile_name(target: Target) -> str:
    name = f"{target.user}-{target.host}"
    if target.port != 22:
        name += f"-{target.port}"
    if ALIAS.fullmatch(name):
        return name
    identity = json_data(
        {"user": target.user, "host": target.host, "port": target.port}
    )
    return f"ssh-{hashlib.sha256(identity).hexdigest()[:16]}"


@dataclass(frozen=True)
class Connection:
    profile: str | None = None
    host: str | None = None
    host_ref: str | None = None
    user: str | None = None
    user_ref: str | None = None
    port: int | None = None
    password_ref: str | None = None
    password: str | None = field(default=None, repr=False)


def lookup(data: dict, name: str) -> dict:
    record = data["profiles"].get(alias(name))
    if record is None:
        raise AppError("profile", f"No such profile: {name}")
    return record


def public_profile(name: str, record: dict) -> dict:
    return {
        "name": name,
        "host": record["host"],
        "port": record["port"],
        "user": record["user"],
        "has_password": "password" in record,
    }


def password_from(directory: int, record: dict) -> str:
    if "password" not in record:
        raise AppError("credential", "The selected profile has no saved SSH password")
    return credentials.decrypt(directory, record["id"], record["password"])


class Profiles:
    def __init__(self, path: Path):
        self.directory = PrivateDirectory(path)

    def load(self, directory: int | None) -> dict:
        raw = self.directory.read(directory, PROFILES_FILE)
        if raw is None:
            return {"version": 1, "profiles": {}, "trusted_hosts": {}}
        try:
            data = json.loads(raw)
            if data["version"] != 1 or not isinstance(data["trusted_hosts"], dict):
                raise ValueError
            for record in data["profiles"].values():
                if not all(
                    isinstance(record[name], str) for name in ("id", "host", "user")
                ):
                    raise ValueError
                if type(record["port"]) is not int:
                    raise ValueError
                if "password" in record and not isinstance(record["password"], str):
                    raise ValueError
            return data
        except (AttributeError, TypeError, KeyError, ValueError) as exc:
            raise AppError(
                "storage", "Invalid or unsupported SSH configuration"
            ) from exc

    def list(self) -> list[dict]:
        with self.directory.open() as directory:
            profiles = self.load(directory)["profiles"]
            return [public_profile(name, profiles[name]) for name in sorted(profiles)]

    def show(self, name: str) -> dict:
        with self.directory.open() as directory:
            return public_profile(name, lookup(self.load(directory), name))

    def reveal(self, name: str, field_name: str) -> str | int:
        with self.directory.open() as directory:
            record = lookup(self.load(directory), name)
            if field_name == "password":
                return password_from(directory, record)
            if field_name in ("host", "user", "port"):
                return record[field_name]
            raise AppError("input", "Unknown profile field")

    def save(
        self,
        name: str,
        *,
        create: bool,
        host=None,
        user=None,
        port=None,
        password=None,
        verified_fingerprint: str | None = None,
    ) -> None:
        alias(name)
        with self.directory.locked() as directory:
            data = self.load(directory)
            profiles = data["profiles"]
            if create:
                if name in profiles:
                    raise AppError("profile", f"Profile already exists: {name}")
                if host is None or user is None:
                    raise AppError("input", "A host and username are required")
                record = {"id": uuid4().hex, "port": 22}
            else:
                record = lookup(data, name).copy()
            if host is not None:
                record["host"] = hostname(host)
            if user is not None:
                record["user"] = username(user)
            if port is not None:
                record["port"] = port_number(port)
            if verified_fingerprint is not None:
                validate_fingerprint(verified_fingerprint)
                endpoint = f"{record['host']}/{record['port']}"
                trusted = data["trusted_hosts"]
                if endpoint in trusted and trusted[endpoint] != verified_fingerprint:
                    raise AppError(
                        "host_key", "The trusted SSH fingerprint changed during setup"
                    )
                trusted[endpoint] = verified_fingerprint
            if password is not None:
                has_credentials = any(
                    "password" in other for other in profiles.values()
                )
                key = credentials.get_key(directory, create=not has_credentials)
                record["password"] = credentials.encrypt(key, record["id"], password)
            profiles[name] = record
            atomic_write(directory, PROFILES_FILE, json_data(data))

    def delete(self, name: str) -> None:
        with self.directory.locked() as directory:
            data = self.load(directory)
            lookup(data, name)
            del data["profiles"][name]
            atomic_write(directory, PROFILES_FILE, json_data(data))

    def trust(self, host: str, port: int, expected: str) -> None:
        key = f"{hostname(host)}/{port_number(port)}"
        expected = validate_fingerprint(expected)
        with self.directory.locked() as directory:
            data = self.load(directory)
            data["trusted_hosts"][key] = expected
            atomic_write(directory, PROFILES_FILE, json_data(data))

    def resolve(self, options: Connection) -> Target:
        with self.directory.open() as directory:
            data = self.load(directory)
            base = lookup(data, options.profile) if options.profile is not None else {}

            def source(field_name, value, reference):
                if reference is None:
                    return value if value is not None else base.get(field_name)
                if value is not None:
                    raise AppError("input", f"Provide one source for {field_name}")
                record = lookup(data, reference)
                if field_name == "password":
                    return password_from(directory, record)
                return record[field_name]

            host = source("host", options.host, options.host_ref)
            user = source("user", options.user, options.user_ref)
            if host is None or user is None:
                raise AppError("input", "An SSH host and username are required")
            host = hostname(host)
            user = username(user)
            port = port_number(
                options.port if options.port is not None else base.get("port", 22)
            )
            if options.password_ref is None and options.password is None:
                password = password_from(directory, base)
            else:
                password = source("password", options.password, options.password_ref)
            expected = data["trusted_hosts"].get(f"{host}/{port}")
            if expected is not None:
                validate_fingerprint(expected)
            return Target(host, port, user, password, expected)
