"""What one look at a provider's account showed.

This module is pure. An account id is opaque and belongs to the vendor: it is compared, never
printed; what a route or a report shows is a fingerprint made by an adapter.
"""

from dataclasses import dataclass

from agents_dispatcher.domain.login import LoginState
from agents_dispatcher.domain.quota import QuotaEvidence


@dataclass(frozen=True)
class AccountReading:
    """The login, the account identity if the vendor names one, and the quota evidence."""

    login: LoginState
    identity: str | None
    quota: QuotaEvidence
