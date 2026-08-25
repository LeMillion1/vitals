#!/usr/bin/env python3
"""Generate one installation VAPID key pair for ``.env``.

Run this intentionally in an operator terminal and store the private value with
the rest of the deployment secrets.  The command does not inspect or modify an
existing environment file.
"""

from __future__ import annotations

import argparse
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subject",
        required=True,
        help="VAPID contact URI, for example mailto:admin@example.com",
    )
    args = parser.parse_args()
    private = ec.generate_private_key(ec.SECP256R1())
    public_bytes = private.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    scalar = private.private_numbers().private_value.to_bytes(32, "big")
    print("VITALS_WEB_PUSH_ENABLED=true")
    print(f"VITALS_WEB_PUSH_VAPID_PUBLIC_KEY={_base64url(public_bytes)}")
    print(f"VITALS_WEB_PUSH_VAPID_PRIVATE_KEY={_base64url(scalar)}")
    print(f"VITALS_WEB_PUSH_VAPID_SUBJECT={args.subject}")


if __name__ == "__main__":
    main()
