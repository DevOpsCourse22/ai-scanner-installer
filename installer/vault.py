"""The answers file: everything the user typed, encrypted with a passphrase.

Stored at $INSTALLER_DATA/answers.enc (a Docker volume), so a re-run can
Upgrade or Destroy without asking again. Format (JSON):

    {"v": 1, "salt": b64, "nonce": b64, "data": b64(AES-256-GCM ciphertext)}

The key is derived from the passphrase with scrypt; the passphrase itself is
never stored. A wrong passphrase fails the GCM tag check (VaultLocked).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

DATA_DIR = Path(os.environ.get("INSTALLER_DATA", "/data"))
FILE = "answers.enc"


class VaultLocked(Exception):
    """Wrong passphrase, or the file was changed."""


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode("utf-8"))


def path() -> Path:
    return DATA_DIR / FILE


def exists() -> bool:
    return path().is_file()


def save(answers: dict[str, Any], passphrase: str) -> None:
    salt, nonce = os.urandom(16), os.urandom(12)
    data = AESGCM(_key(passphrase, salt)).encrypt(nonce, json.dumps(answers).encode("utf-8"), None)
    blob = {"v": 1, **{k: base64.b64encode(v).decode("ascii")
                       for k, v in (("salt", salt), ("nonce", nonce), ("data", data))}}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path().with_suffix(".tmp")
    tmp.write_text(json.dumps(blob), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path())


def load(passphrase: str) -> dict[str, Any]:
    blob = json.loads(path().read_text(encoding="utf-8"))
    raw = {k: base64.b64decode(blob[k]) for k in ("salt", "nonce", "data")}
    try:
        plain = AESGCM(_key(passphrase, raw["salt"])).decrypt(raw["nonce"], raw["data"], None)
    except InvalidTag as exc:
        raise VaultLocked("That passphrase does not open the saved answers.") from exc
    return json.loads(plain)
