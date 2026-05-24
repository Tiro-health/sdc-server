#!/usr/bin/env python3
"""Mint a signed Ed25519 JWT license for sdc-server.

Usage:

    uv run python scripts/mint_license.py \
        --private-key ./private.pem \
        --subject "acme-hospital" \
        --days 90 \
        --out acme.jwt

The resulting JWT carries (at minimum):
    iss = tiro.health
    aud = sdc-server
    sub = <customer id>
    iat = now
    exp = now + days

Hand the .jwt file to the customer (or feed it through ``FHIR_SDC_LICENSE``).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key

ISSUER = "tiro.health"
AUDIENCE = "sdc-server"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--subject", required=True, help="Customer identifier")
    parser.add_argument("--days", type=int, required=True, help="Validity in days")
    parser.add_argument("--out", type=Path, help="Write the token here (else stdout)")
    parser.add_argument(
        "--claim",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Additional claim (string value). Repeatable.",
    )
    args = parser.parse_args()

    if args.days <= 0:
        print("--days must be positive", file=sys.stderr)
        return 1

    private_key = load_pem_private_key(args.private_key.read_bytes(), password=None)

    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": args.subject,
        "iat": now,
        "exp": now + args.days * 86400,
    }
    for extra in args.claim:
        if "=" not in extra:
            print(f"bad --claim (need KEY=VALUE): {extra!r}", file=sys.stderr)
            return 1
        k, v = extra.split("=", 1)
        claims[k] = v

    token = jwt.encode(claims, private_key, algorithm="EdDSA")

    if args.out:
        args.out.write_text(token + "\n")
        print(f"wrote {args.out} (sub={args.subject}, expires in {args.days} days)")
    else:
        print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
