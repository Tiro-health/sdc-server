#!/usr/bin/env python3
"""Generate an Ed25519 keypair for signing sdc-server license JWTs.

The private key never leaves your machine (or wherever you keep secrets — a
password manager, a sealed secret in Kubernetes, etc.). Only the *public* key
is embedded in the Docker image.

Usage:

    uv run python scripts/gen_license_keypair.py --out-private ./private.pem \
        --out-public ./public.pem

Then paste the contents of public.pem into
``sdc-server/src/sdc_server/license_gate.py`` as ``EMBEDDED_PUBKEY_PEM``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-private", type=Path, required=True)
    parser.add_argument("--out-public", type=Path, required=True)
    args = parser.parse_args()

    if args.out_private.exists():
        print(f"refusing to overwrite {args.out_private}", file=sys.stderr)
        return 1

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    args.out_private.write_bytes(private_pem)
    args.out_private.chmod(0o600)
    args.out_public.write_bytes(public_pem)

    print(f"wrote {args.out_private} (private, mode 600)")
    print(f"wrote {args.out_public}  (public — paste into license_gate.py)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
