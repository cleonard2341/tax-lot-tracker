"""Coinbase API importer for transactions."""

import hashlib
import hmac
import time
from datetime import datetime
from decimal import Decimal
from typing import Literal

import requests

from ..models import Transaction
from ..utils import generate_tx_id, normalize_asset
from .base import BaseImporter


class CoinbaseImporter(BaseImporter):
    """Import transactions from Coinbase API."""

    BASE_URL = "https://api.coinbase.com/v2"

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = requests.Session()

    @property
    def source_name(self) -> str:
        return "coinbase"

    def _get_auth_headers(self, method: str, path: str, body: str = "") -> dict:
        """Generate authentication headers for Coinbase API."""
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path + body
        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

        return {
            "CB-ACCESS-KEY": self.api_key,
            "CB-ACCESS-SIGN": signature,
            "CB-ACCESS-TIMESTAMP": timestamp,
            "CB-VERSION": "2024-01-01",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Make authenticated request to Coinbase API."""
        url = f"{self.BASE_URL}{endpoint}"
        headers = self._get_auth_headers(method, endpoint)

        response = self.session.request(
            method, url, headers=headers, timeout=30, **kwargs
        )
        response.raise_for_status()
        return response.json()

    def _paginate(self, endpoint: str) -> list[dict]:
        """Fetch all pages from a paginated endpoint."""
        results = []
        next_uri = endpoint

        while next_uri:
            # Handle full URI vs path
            if next_uri.startswith("http"):
                next_uri = next_uri.replace(self.BASE_URL, "")

            data = self._request("GET", next_uri)
            results.extend(data.get("data", []))

            pagination = data.get("pagination", {})
            next_uri = pagination.get("next_uri")

        return results

    def validate_credentials(self) -> bool:
        """Validate API credentials."""
        try:
            self._request("GET", "/user")
            return True
        except requests.exceptions.HTTPError:
            return False

    def fetch_transactions(
        self, since: datetime | None = None
    ) -> list[Transaction]:
        """Fetch all transactions from Coinbase."""
        transactions = []

        # Get all accounts
        accounts = self._paginate("/accounts")

        for account in accounts:
            account_id = account["id"]
            currency = account.get("currency", {})
            asset = currency.get("code", "") if isinstance(currency, dict) else currency

            if not asset:
                continue

            # Get transactions for this account
            try:
                txs = self._paginate(f"/accounts/{account_id}/transactions")
            except requests.exceptions.HTTPError:
                continue

            for tx_data in txs:
                tx = self._parse_transaction(tx_data, asset)
                if tx and (since is None or tx.timestamp >= since):
                    transactions.append(tx)

        return sorted(transactions, key=lambda t: t.timestamp)

    def _parse_transaction(
        self, data: dict, default_asset: str
    ) -> Transaction | None:
        """Parse a Coinbase transaction into our Transaction model."""
        tx_type = data.get("type", "").lower()
        status = data.get("status", "")

        # Skip pending/failed transactions
        if status != "completed":
            return None

        # Map Coinbase types to our types
        type_map = {
            "buy": "buy",
            "sell": "sell",
            "send": "transfer",
            "receive": "transfer",
            "trade": "sell",  # Will create corresponding buy
            "fiat_deposit": None,  # Skip fiat
            "fiat_withdrawal": None,
            "interest": "income",
            "inflation_reward": "income",
            "staking_reward": "income",
        }

        our_type = type_map.get(tx_type)
        if our_type is None:
            return None

        # Parse amount
        amount_data = data.get("amount", {})
        amount = abs(Decimal(str(amount_data.get("amount", 0))))
        asset = normalize_asset(amount_data.get("currency", default_asset))

        if amount == 0:
            return None

        # Parse native amount (USD value)
        native_data = data.get("native_amount", {})
        total_usd = abs(Decimal(str(native_data.get("amount", 0))))

        # Calculate price per unit
        price_usd = total_usd / amount if amount else Decimal("0")

        # Parse fee from details if available
        fee_usd = Decimal("0")
        details = data.get("details", {})
        if "fee" in details:
            fee_usd = abs(Decimal(str(details["fee"].get("amount", 0))))

        # Parse timestamp
        created_at = data.get("created_at", "")
        timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        # Generate deterministic ID
        tx_id = generate_tx_id("coinbase", data.get("id", ""), created_at)

        return Transaction(
            id=tx_id,
            timestamp=timestamp,
            type=our_type,
            asset=asset,
            amount=amount,
            price_usd=price_usd,
            fee_usd=fee_usd,
            source=self.source_name,
            raw_data=data,
        )
