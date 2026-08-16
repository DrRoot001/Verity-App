#!/usr/bin/env python3
"""Generate a local .env with real, freshly-generated development secrets.

Local development still exercises the production code paths for JWT signing and
envelope encryption (PRD §28.1), so it needs real keys — not empty strings.
These keys are development-only and are regenerated on demand; production keys
come from the managed secret store.

Usage:
    python scripts/bootstrap_env.py [--force]
"""

from __future__ import annotations

import argparse
import base64
import pathlib
import secrets
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
EXAMPLE_PATH = REPO_ROOT / ".env.example"

GENERATED: dict[str, str] = {}


def _generate_keys() -> None:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        sys.exit(
            "cryptography is required. Install backend deps first:\n"
            "  make setup-backend"
        )

    private_key = ed25519.Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    # Multi-line PEMs are stored with escaped newlines so .env stays single-line
    # per key; the config layer unescapes them.
    GENERATED["VERITY_JWT_PRIVATE_KEY_PEM"] = private_pem.replace("\n", "\\n")
    GENERATED["VERITY_JWT_PUBLIC_KEY_PEM"] = public_pem.replace("\n", "\\n")
    GENERATED["VERITY_JWT_KID"] = f"dev-{secrets.token_hex(4)}"
    GENERATED["VERITY_DATA_ENCRYPTION_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="overwrite an existing .env")
    args = parser.parse_args()

    if ENV_PATH.exists() and not args.force:
        print(f"{ENV_PATH} already exists. Re-run with --force to regenerate.")
        return 0

    if not EXAMPLE_PATH.exists():
        sys.exit(f"missing {EXAMPLE_PATH}")

    _generate_keys()

    lines: list[str] = []
    for line in EXAMPLE_PATH.read_text().splitlines():
        key = line.split("=", 1)[0] if "=" in line and not line.startswith("#") else None
        if key and key in GENERATED:
            lines.append(f"{key}={GENERATED[key]}")
        else:
            lines.append(line)

    ENV_PATH.write_text("\n".join(lines) + "\n")
    ENV_PATH.chmod(0o600)

    print(f"wrote {ENV_PATH} (mode 600) with generated development secrets:")
    for key in GENERATED:
        print(f"  - {key}")
    print("\nThese are development-only keys. Never reuse them in a deployed environment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
