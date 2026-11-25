import os
from base64 import b64decode, b64encode
from binascii import Error as BinasciiError

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import AppError
from .storage import PrivateDirectory, atomic_write

KEY_FILE = "vault.key"


def get_key(directory: int, *, create: bool = False) -> bytes:
    try:
        key = PrivateDirectory.read(directory, KEY_FILE)
    except AppError as exc:
        raise AppError(
            "credential", "Could not read the private SSH password key"
        ) from exc
    if key is None and create:
        key = AESGCM.generate_key(bit_length=256)
        atomic_write(directory, KEY_FILE, key)
    if key is None or len(key) != 32:
        raise AppError("credential", "SSH password key is missing or invalid")
    return key


def associated_data(record_id: str) -> bytes:
    return f"ssh4a/1/{record_id}/password".encode()


def encrypt(key: bytes, record_id: str, value: str) -> str:
    nonce = os.urandom(12)
    encrypted = AESGCM(key).encrypt(nonce, value.encode(), associated_data(record_id))
    return b64encode(nonce + encrypted).decode("ascii")


def decrypt(directory: int, record_id: str, value: str) -> str:
    key = get_key(directory)
    try:
        data = b64decode(value, validate=True)
        return (
            AESGCM(key)
            .decrypt(data[:12], data[12:], associated_data(record_id))
            .decode()
        )
    except (InvalidTag, ValueError, BinasciiError) as exc:
        raise AppError(
            "credential", "Saved SSH password failed integrity verification"
        ) from exc
