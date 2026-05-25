"""License gate for sdc-server.

Verifies an Ed25519-signed JWT before the application is allowed to serve
traffic. The token is expected in (priority order):

    1. ``FHIR_SDC_LICENSE``           — token as a string
    2. ``FHIR_SDC_LICENSE_FILE``      — path to a file containing the token
    3. ``/etc/sdc-server/license.jwt`` — default mount point

Verification expects ``alg=EdDSA``, ``iss=tiro.health``, ``aud=sdc-server``,
and the standard ``exp``/``iat``/``sub`` claims. Anything else aborts the
process with a non-zero exit code.

The public key used to verify is loaded from:

    1. ``FHIR_SDC_LICENSE_PUBKEY_FILE`` — path to a PEM file
    2. ``FHIR_SDC_LICENSE_PUBKEY``      — PEM string (with literal newlines or
                                          ``\\n`` escapes; both are accepted)
    3. The constant ``EMBEDDED_PUBKEY_PEM`` below — set this to your production
       public key before publishing the Docker image.

``FHIR_SDC_LICENSE_SKIP=1`` bypasses the gate entirely. This is a dev-only
escape hatch — production containers must not set it.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from sdc_server._build_flags import ALLOW_LICENSE_SKIP

# Production public key. Replace before publishing the image.
# Generated with `python -m sdc_server.license_gate gen-key`.
EMBEDDED_PUBKEY_PEM: bytes = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAeDEgzpRLN/miuTecR9DL5KDGejnns5cu5Khfo3YtC7M=
-----END PUBLIC KEY-----
"""

EXPECTED_ISSUER = "tiro.health"
EXPECTED_AUDIENCE = "sdc-server"
DEFAULT_TOKEN_FILE = "/etc/sdc-server/license.jwt"


class LicenseError(Exception):
    """Raised when the license cannot be loaded or fails verification."""


def bypass_requested() -> bool:
    """True iff the env var asks to skip *and* the build permits it.

    Release builds set ``ALLOW_LICENSE_SKIP = False`` at compile time, so the
    env var becomes a no-op even if the customer sets it — the baked-in
    bytecode never reads it.
    """
    return ALLOW_LICENSE_SKIP and os.environ.get("FHIR_SDC_LICENSE_SKIP") == "1"


def _load_token() -> str:
    token = os.environ.get("FHIR_SDC_LICENSE")
    if token:
        return token.strip()
    path_str = os.environ.get("FHIR_SDC_LICENSE_FILE", DEFAULT_TOKEN_FILE)
    path = Path(path_str)
    if path.is_file():
        return path.read_text().strip()
    raise LicenseError(
        f"No license found. Set FHIR_SDC_LICENSE, or mount a token at {path_str}."
    )


def _load_pubkey_pem() -> bytes:
    file_override = os.environ.get("FHIR_SDC_LICENSE_PUBKEY_FILE")
    if file_override:
        return Path(file_override).read_bytes()
    inline = os.environ.get("FHIR_SDC_LICENSE_PUBKEY")
    if inline:
        return inline.replace("\\n", "\n").encode("utf-8")
    return EMBEDDED_PUBKEY_PEM


def verify_license() -> dict:
    """Verify the configured license token. Returns the decoded claims on success."""
    import jwt
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    token = _load_token()
    pubkey_pem = _load_pubkey_pem()
    try:
        pubkey = load_pem_public_key(pubkey_pem)
    except Exception as exc:
        raise LicenseError(
            "Could not load license public key — the image was published without "
            "a real key. Set FHIR_SDC_LICENSE_PUBKEY_FILE or replace EMBEDDED_PUBKEY_PEM."
        ) from exc

    try:
        claims = jwt.decode(
            token,
            pubkey,
            algorithms=["EdDSA"],
            issuer=EXPECTED_ISSUER,
            audience=EXPECTED_AUDIENCE,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise LicenseError(f"License expired: {exc}") from exc
    except jwt.InvalidTokenError as exc:
        raise LicenseError(f"Invalid license: {exc}") from exc

    return claims


def verify_license_or_exit() -> None:
    """Verify the license and exit the process on failure.

    Called once at module import from ``sdc_server.app``. Also runnable as
    ``python -m sdc_server.license_gate`` so the Docker entrypoint can gate
    startup before exec'ing uvicorn.
    """
    if bypass_requested():
        print("[license] FHIR_SDC_LICENSE_SKIP=1 — verification bypassed", file=sys.stderr)
        return

    try:
        claims = verify_license()
    except LicenseError as exc:
        print(f"[license] {exc}", file=sys.stderr)
        sys.exit(2)

    sub = claims.get("sub", "?")
    exp = claims.get("exp", 0)
    remaining_days = max(0.0, (exp - time.time()) / 86400)
    print(
        f"[license] valid for sub={sub} ({remaining_days:.1f} days remaining)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    verify_license_or_exit()
