"""Binance API importer for transactions."""

import hashlib
import hmac
import time
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

import requests

from ..models import Transaction
from ..utils import generate_tx_id, normalize_asset
from .base import BaseImporter


class BinanceImporter(BaseImporter):
    """Import transactions from Binance API."""

    BASE_URL = "https://api.binance.com"
    US_BASE_URL = "https://api.binance.us"

    def __init__(self, api_key: str, api_secret: str, use_us: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = self.US_BASE_URL if use_us else self.BASE_URL
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})

    @property
    def source_name(self) -> str:
        return "binance"

    def _sign_request(self, params: dict) -> dict:
        """Add signature to request parameters."""
        params["timestamp"] = int(time.time() * 1000)
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(),
            query_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = signature
        return params

    def _request(self, endpoint: str, params: dict | None = None) -> dict | list:
        """Make authenticated request to Binance API."""
        params = params or {}
        params = self._sign_request(params)

        url = f"{self.base_url}{endpoint}"
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def validate_credentials(self) -> bool:
        """Validate API credentials."""
        try:
            self._request("/api/v3/account")
            return True
        except requests.exceptions.HTTPError:
            return False

    def fetch_transactions(
        self, since: datetime | None = None
    ) -> list[Transaction]:
        """Fetch all transactions from Binance."""
        transactions = []

        # Fetch trades from all trading pairs
        trades = self._fetch_all_trades(since)
        transactions.extend(trades)

        # Fetch deposits
        deposits = self._fetch_deposits(since)
        transactions.extend(deposits)

        # Fetch withdrawals
        withdrawals = self._fetch_withdrawals(since)
        transactions.extend(withdrawals)

        # Fetch dividend/staking rewards
        dividends = self._fetch_dividends(since)
        transactions.extend(dividends)

        return sorted(transactions, key=lambda t: t.timestamp)

    def _fetch_all_trades(self, since: datetime | None = None) -> list[Transaction]:
        """Fetch trades from all trading pairs."""
        transactions = []

        # Get exchange info to get all trading pairs
        try:
            exchange_info = self.session.get(
                f"{self.base_url}/api/v3/exchangeInfo", timeout=30
            ).json()
        except requests.exceptions.RequestException:
            return transactions

        symbols = [s["symbol"] for s in exchange_info.get("symbols", [])]

        # Filter to USDT/BUSD/USD pairs for simplicity
        usd_pairs = [
            s for s in symbols
            if s.endswith(("USDT", "BUSD", "USD"))
        ]

        start_time = int(since.timestamp() * 1000) if since else None

        for symbol in usd_pairs:
            try:
                params = {"symbol": symbol, "limit": 1000}
                if start_time:
                    params["startTime"] = start_time

                trades = self._request("/api/v3/myTrades", params)

                for trade in trades:
                    tx = self._parse_trade(trade, symbol)
                    if tx:
                        transactions.append(tx)

            except requests.exceptions.HTTPError:
                continue

        return transactions

    def _parse_trade(self, data: dict, symbol: str) -> Transaction | None:
        """Parse a Binance trade into our Transaction model."""
        # Determine base and quote assets
        quote_assets = ["USDT", "BUSD", "USD"]
        quote = None
        for q in quote_assets:
            if symbol.endswith(q):
                quote = q
                break

        if not quote:
            return None

        base = symbol[:-len(quote)]
        is_buyer = data.get("isBuyer", False)

        # Parse amounts
        qty = Decimal(str(data.get("qty", 0)))
        price = Decimal(str(data.get("price", 0)))
        commission = Decimal(str(data.get("commission", 0)))
        commission_asset = data.get("commissionAsset", "")

        # Convert commission to USD if needed
        if commission_asset == quote:
            fee_usd = commission
        elif commission_asset == base:
            fee_usd = commission * price
        else:
            fee_usd = Decimal("0")  # Other token, skip

        # Parse timestamp
        timestamp = datetime.fromtimestamp(data.get("time", 0) / 1000)

        # Generate deterministic ID
        tx_id = generate_tx_id(
            "binance",
            str(data.get("id", "")),
            symbol,
            str(data.get("time", "")),
        )

        return Transaction(
            id=tx_id,
            timestamp=timestamp,
            type="buy" if is_buyer else "sell",
            asset=normalize_asset(base),
            amount=qty,
            price_usd=price,
            fee_usd=fee_usd,
            source=self.source_name,
            raw_data=data,
        )

    def _fetch_deposits(self, since: datetime | None = None) -> list[Transaction]:
        """Fetch deposit history."""
        transactions = []

        params = {"status": 1}  # Success only
        if since:
            params["startTime"] = int(since.timestamp() * 1000)

        try:
            deposits = self._request("/sapi/v1/capital/deposit/hisrec", params)

            for deposit in deposits:
                tx = self._parse_deposit(deposit)
                if tx:
                    transactions.append(tx)

        except requests.exceptions.HTTPError:
            pass

        return transactions

    def _parse_deposit(self, data: dict) -> Transaction | None:
        """Parse a Binance deposit."""
        if data.get("status") != 1:
            return None

        amount = Decimal(str(data.get("amount", 0)))
        asset = normalize_asset(data.get("coin", ""))
        timestamp = datetime.fromtimestamp(data.get("insertTime", 0) / 1000)

        tx_id = generate_tx_id(
            "binance_deposit",
            data.get("txId", ""),
            str(data.get("insertTime", "")),
        )

        return Transaction(
            id=tx_id,
            timestamp=timestamp,
            type="transfer",
            asset=asset,
            amount=amount,
            price_usd=Decimal("0"),  # Will need price lookup
            fee_usd=Decimal("0"),
            source=self.source_name,
            raw_data=data,
        )

    def _fetch_withdrawals(self, since: datetime | None = None) -> list[Transaction]:
        """Fetch withdrawal history."""
        transactions = []

        params = {"status": 6}  # Completed only
        if since:
            params["startTime"] = int(since.timestamp() * 1000)

        try:
            withdrawals = self._request("/sapi/v1/capital/withdraw/history", params)

            for withdrawal in withdrawals:
                tx = self._parse_withdrawal(withdrawal)
                if tx:
                    transactions.append(tx)

        except requests.exceptions.HTTPError:
            pass

        return transactions

    def _parse_withdrawal(self, data: dict) -> Transaction | None:
        """Parse a Binance withdrawal."""
        if data.get("status") != 6:
            return None

        amount = Decimal(str(data.get("amount", 0)))
        asset = normalize_asset(data.get("coin", ""))
        fee = Decimal(str(data.get("transactionFee", 0)))

        apply_time = data.get("applyTime", "")
        if apply_time:
            timestamp = datetime.fromisoformat(apply_time.replace("Z", "+00:00"))
        else:
            timestamp = datetime.now()

        tx_id = generate_tx_id(
            "binance_withdrawal",
            data.get("id", ""),
            apply_time,
        )

        return Transaction(
            id=tx_id,
            timestamp=timestamp,
            type="transfer",
            asset=asset,
            amount=amount,
            price_usd=Decimal("0"),
            fee_usd=fee,
            source=self.source_name,
            raw_data=data,
        )

    def _fetch_dividends(self, since: datetime | None = None) -> list[Transaction]:
        """Fetch dividend/staking reward history."""
        transactions = []

        params = {}
        if since:
            params["startTime"] = int(since.timestamp() * 1000)

        try:
            result = self._request("/sapi/v1/asset/assetDividend", params)
            rows = result.get("rows", [])

            for row in rows:
                tx = self._parse_dividend(row)
                if tx:
                    transactions.append(tx)

        except requests.exceptions.HTTPError:
            pass

        return transactions

    def _parse_dividend(self, data: dict) -> Transaction | None:
        """Parse a Binance dividend/reward."""
        amount = Decimal(str(data.get("amount", 0)))
        asset = normalize_asset(data.get("asset", ""))
        div_time = data.get("divTime", 0)
        timestamp = datetime.fromtimestamp(div_time / 1000)

        tx_id = generate_tx_id(
            "binance_dividend",
            str(data.get("tranId", "")),
            str(div_time),
        )

        return Transaction(
            id=tx_id,
            timestamp=timestamp,
            type="income",
            asset=asset,
            amount=amount,
            price_usd=Decimal("0"),  # Will need price lookup
            fee_usd=Decimal("0"),
            source=self.source_name,
            raw_data=data,
        )
