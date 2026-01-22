"""Base class for exchange importers."""

from abc import ABC, abstractmethod
from datetime import datetime

from ..models import Transaction


class BaseImporter(ABC):
    """Abstract base class for exchange importers."""

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Return the source name for transactions (e.g., 'coinbase')."""
        pass

    @abstractmethod
    def fetch_transactions(
        self, since: datetime | None = None
    ) -> list[Transaction]:
        """Fetch transactions from the exchange.

        Args:
            since: Optional datetime to fetch transactions from.
                   If None, fetches all available transactions.

        Returns:
            List of Transaction objects.
        """
        pass

    def validate_credentials(self) -> bool:
        """Validate that API credentials are working.

        Returns:
            True if credentials are valid, False otherwise.
        """
        return True
