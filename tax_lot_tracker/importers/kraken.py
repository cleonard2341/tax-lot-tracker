"""Kraken API importer for transactions."""

import base64
import hashlib
import hmac
import time
import urllib.parse
from datetime import datetime
from decimal import Decimal

import requests

from ..models import Transaction
from ..utils import generate_tx_id, normalize_asset
from .base import BaseImporter


# Kraken uses different asset names
KRAKEN_ASSET_MAP = {
    "XXBT": "BTC",
    "XBT": "BTC",
    "XETH": "ETH",
    "XXRP": "XRP",
    "XXLM": "XLM",
    "XLTC": "LTC",
    "XXMR": "XMR",
    "XZEC": "ZEC",
    "XETC": "ETC",
    "XREP": "REP",
    "XXDG": "DOGE",
    "ZUSD": "USD",
    "ZEUR": "EUR",
    "ZGBP": "GBP",
    "ZCAD": "CAD",
    "ZJPY": "JPY",
    "USDT": "USDT",
    "USDC": "USDC",
    "DAI": "DAI",
}


class KrakenImporter(BaseImporter):
    """Import transactions from Kraken API."""

    BASE_URL = "https://api.kraken.com"

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = base64.b64decode(api_secret)
        self.session = requests.Session()

    @property
    def source_name(self) -> str:
        return "kraken"

    def _get_signature(self, urlpath: str, data: dict) -> str:
        """Generate Kraken API signature."""
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data["nonce"]) + postdata).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        signature = hmac.new(self.api_secret, message, hashlib.sha512)
        return base64.b64encode(signature.digest()).decode()

    def _request(self, endpoint: str, data: dict | None = None) -> dict:
        """Make authenticated request to Kraken API."""
        url = f"{self.BASE_URL}{endpoint}"
        data = data or {}
        data["nonce"] = str(int(time.time() * 1000))

        headers = {
            "API-Key": self.api_key,
            "API-Sign": self._get_signature(endpoint, data),
        }

        response = self.session.post(url, headers=headers, data=data, timeout=30)
        response.raise_for_status()

        result = response.json()
        if result.get("error"):
            raise ValueError(f"Kraken API error: {result['error']}")

        return result.get("result", {})

    def validate_credentials(self) -> bool:
        """Validate API credentials."""
        try:
            self._request("/0/private/Balance")
            return True
        except (requests.exceptions.HTTPError, ValueError):
            return False

    def _normalize_asset(self, asset: str) -> str:
        """Convert Kraken asset name to standard symbol."""
        asset = asset.upper()
        return KRAKEN_ASSET_MAP.get(asset, asset)

    def fetch_transactions(
        self, since: datetime | None = None
    ) -> list[Transaction]:
        """Fetch all transactions from Kraken."""
        transactions = []

        # Fetch trade history
        trades = self._fetch_trades(since)
        transactions.extend(trades)

        # Fetch ledger entries for deposits, withdrawals, staking
        ledger = self._fetch_ledger(since)
        transactions.extend(ledger)

        return sorted(transactions, key=lambda t: t.timestamp)

    def _fetch_trades(self, since: datetime | None = None) -> list[Transaction]:
        """Fetch trade history."""
        transactions = []
        offset = 0

        while True:
            params = {"ofs": offset}
            if since:
                params["start"] = int(since.timestamp())

            result = self._request("/0/private/TradesHistory", params)
            trades = result.get("trades", {})

            if not trades:
                break

            for trade_id, trade_data in trades.items():
                tx = self._parse_trade(trade_id, trade_data)
                if tx:
                    transactions.append(tx)

            offset += len(trades)
            if len(trades) < 50:  # Kraken default page size
                break

        return transactions

    def _parse_trade(self, trade_id: str, data: dict) -> Transaction | None:
        """Parse a Kraken trade into our Transaction model."""
        pair = data.get("pair", "")
        trade_type = data.get("type", "").lower()  # buy or sell

        # Parse the trading pair
        base, quote = self._parse_pair(pair)
        if not base or not quote:
            return None

        # Only process USD pairs for now
        if quote not in ("USD", "USDT", "USDC"):
            return None

        vol = Decimal(str(data.get("vol", 0)))
        price = Decimal(str(data.get("price", 0)))
        fee = Decimal(str(data.get("fee", 0)))
        timestamp = datetime.fromtimestamp(data.get("time", 0))

        tx_id = generate_tx_id("kraken", trade_id, str(data.get("time", "")))

        return Transaction(
            id=tx_id,
            timestamp=timestamp,
            type="buy" if trade_type == "buy" else "sell",
            asset=self._normalize_asset(base),
            amount=vol,
            price_usd=price,
            fee_usd=fee,
            source=self.source_name,
            raw_data={"trade_id": trade_id, **data},
        )

    def _parse_pair(self, pair: str) -> tuple[str, str]:
        """Parse a Kraken trading pair into base and quote assets."""
        # Kraken pairs can be like XXBTZUSD, ETHUSD, etc.
        # Try common patterns
        usd_quotes = ["ZUSD", "USD", "USDT", "USDC"]

        for quote in usd_quotes:
            if pair.endswith(quote):
                base = pair[:-len(quote)]
                return base, quote.lstrip("Z")

        return "", ""

    def _fetch_ledger(self, since: datetime | None = None) -> list[Transaction]:
        """Fetch ledger entries for non-trade transactions."""
        transactions = []
        offset = 0

        while True:
            params = {"ofs": offset}
            if since:
                params["start"] = int(since.timestamp())

            result = self._request("/0/private/Ledgers", params)
            ledger = result.get("ledger", {})

            if not ledger:
                break

            for entry_id, entry_data in ledger.items():
                tx = self._parse_ledger_entry(entry_id, entry_data)
                if tx:
                    transactions.append(tx)

            offset += len(ledger)
            if len(ledger) < 50:
                break

        return transactions

    def _parse_ledger_entry(self, entry_id: str, data: dict) -> Transaction | None:
        """Parse a Kraken ledger entry."""
        entry_type = data.get("type", "").lower()

        # Map Kraken types to our types
        type_map = {
            "deposit": "transfer",
            "withdrawal": "transfer",
            "staking": "income",
            "dividend": "income",
            "reward": "income",
        }

        our_type = type_map.get(entry_type)
        if not our_type:
            return None

        asset = self._normalize_asset(data.get("asset", ""))
        amount = abs(Decimal(str(data.get("amount", 0))))
        fee = abs(Decimal(str(data.get("fee", 0))))
        timestamp = datetime.fromtimestamp(data.get("time", 0))

        # Skip fiat entries
        if asset in ("USD", "EUR", "GBP", "CAD", "JPY"):
            return None

        tx_id = generate_tx_id("kraken_ledger", entry_id, str(data.get("time", "")))

        return Transaction(
            id=tx_id,
            timestamp=timestamp,
            type=our_type,
            asset=asset,
            amount=amount,
            price_usd=Decimal("0"),  # Will need price lookup
            fee_usd=fee,
            source=self.source_name,
            raw_data={"entry_id": entry_id, **data},
        )
