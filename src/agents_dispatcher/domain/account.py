"""What one look at a provider's account showed, and how an account identity is shown.

This module is pure. An account id is opaque and belongs to the vendor: it is compared, never
printed. What a route or a report may show is a short fingerprint of it.
"""

import hashlib
from dataclasses import dataclass

from agents_dispatcher.domain.login import LoginState
from agents_dispatcher.domain.quota import QuotaEvidence

FINGERPRINT_CHARS = 12


@dataclass(frozen=True)
class AccountReading:
    """The login, the account identity if the vendor names one, and the quota evidence."""

    login: LoginState
    identity: str | None
    quota: QuotaEvidence


def fingerprint(identity: str) -> str:
    """Return a short, stable, one-way mark of an account id, safe to show."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]
