"""Verify the signed bytecode integrity manifest baked into the image.

At build time the Dockerfile writes:

    /app/integrity/manifest.sha256   ← `sha256sum` output for every .pyc + entrypoint
    /app/integrity/manifest.sig      ← Ed25519 raw signature over manifest.sha256

The verification public key is the same one used for license JWTs
(``EMBEDDED_PUBKEY_PEM`` in ``sdc_server.license_gate``) — both come from the
atticus signing key in Google Secret Manager.

This module is invoked by ``entrypoint.sh`` as ``python -m
sdc_server.integrity_check`` *before* the license gate. On success it prints
one line to stderr; on any failure it prints the reason and exits 2.

Note: this check is structurally limited — a root attacker inside the
container can patch this module to no-op the check, just as they could patch
the previous shell-based check. The defence here is depth, not strength.
For stronger guarantees, sign the published image with cosign and verify
out-of-band before deploying.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from sdc_server.license_gate import EMBEDDED_PUBKEY_PEM

INTEGRITY_DIR = Path("/app/integrity")
MANIFEST_PATH = INTEGRITY_DIR / "manifest.sha256"
SIGNATURE_PATH = INTEGRITY_DIR / "manifest.sig"


class IntegrityError(Exception):
    """Raised when the manifest signature or any file hash fails verification."""


def verify_integrity() -> None:
    """Verify the manifest signature, then every file it references.

    Raises ``IntegrityError`` on any mismatch. Quiet on success.
    """
    manifest_bytes = MANIFEST_PATH.read_bytes()
    signature = SIGNATURE_PATH.read_bytes()
    pubkey = load_pem_public_key(EMBEDDED_PUBKEY_PEM)

    try:
        pubkey.verify(signature, manifest_bytes)
    except InvalidSignature as exc:
        raise IntegrityError(
            "manifest signature is invalid — image has been tampered with"
        ) from exc

    # sha256sum output: "<64-hex>  <path>" (text mode) or "<64-hex> *<path>" (binary).
    # We accept both — partition on first space, strip the leading mode marker.
    for lineno, line in enumerate(manifest_bytes.decode().splitlines(), 1):
        if not line.strip():
            continue
        digest_hex, sep, rest = line.partition(" ")
        if not sep or not rest:
            raise IntegrityError(f"malformed manifest line {lineno}: {line!r}")
        path_str = rest[1:] if rest[0] in (" ", "*") else rest
        path = Path(path_str)
        try:
            actual_hex = hashlib.sha256(path.read_bytes()).hexdigest()
        except FileNotFoundError as exc:
            raise IntegrityError(
                f"file listed in manifest is missing: {path}"
            ) from exc
        if actual_hex != digest_hex:
            raise IntegrityError(
                f"file hash does not match the signed manifest: {path}"
            )


def main() -> None:
    try:
        verify_integrity()
    except IntegrityError as exc:
        print(f"[integrity] {exc}", file=sys.stderr)
        sys.exit(2)
    print("[integrity] manifest verified", file=sys.stderr)


if __name__ == "__main__":
    main()
