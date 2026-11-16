"""The shown mark of an account id: a vendor's account id is compared, never printed."""

import hashlib

FINGERPRINT_CHARS = 12


def fingerprint(identity: str) -> str:
    """Return a short, stable, one-way mark of an account id, safe to show."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]
