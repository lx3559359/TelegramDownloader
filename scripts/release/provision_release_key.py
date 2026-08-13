from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def provision(private_path: Path, trusted_path: Path, key_id: str) -> None:
    if private_path.exists():
        raise FileExistsError("release private key already exists")
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(private_path, private_bytes)
    trusted = {
        key_id: base64.b64encode(public_bytes).decode("ascii"),
    }
    _atomic_write(
        trusted_path,
        (
            json.dumps(trusted, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode(),
    )


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--trusted-keys", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    arguments = parser.parse_args()
    provision(arguments.private_key, arguments.trusted_keys, arguments.key_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
