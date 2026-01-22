"""Price fetching from CoinGecko API."""

import time
from datetime import datetime
from decimal import Decimal

import requests

from .database import Database


# Map common symbols to CoinGecko IDs
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "USDT": "tether",
    "USDC": "usd-coin",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "SOL": "solana",
    "DOT": "polkadot",
    "MATIC": "matic-network",
    "LTC": "litecoin",
    "SHIB": "shiba-inu",
    "TRX": "tron",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "ATOM": "cosmos",
    "UNI": "uniswap",
    "XMR": "monero",
    "ETC": "ethereum-classic",
    "XLM": "stellar",
    "BCH": "bitcoin-cash",
    "ALGO": "algorand",
    "FIL": "filecoin",
    "VET": "vechain",
    "ICP": "internet-computer",
    "MANA": "decentraland",
    "SAND": "the-sandbox",
    "AXS": "axie-infinity",
    "AAVE": "aave",
    "CRO": "crypto-com-chain",
    "FTM": "fantom",
    "NEAR": "near",
    "GRT": "the-graph",
    "FLOW": "flow",
    "ENJ": "enjincoin",
    "CHZ": "chiliz",
    "HBAR": "hedera-hashgraph",
    "KCS": "kucoin-shares",
    "NEO": "neo",
    "QNT": "quant-network",
    "BAT": "basic-attention-token",
    "ZEC": "zcash",
    "DASH": "dash",
    "WAVES": "waves",
    "MKR": "maker",
    "COMP": "compound-governance-token",
    "SNX": "synthetix-network-token",
    "YFI": "yearn-finance",
}


class PriceFetcher:
    """Fetch historical crypto prices from CoinGecko."""

    BASE_URL = "https://api.coingecko.com/api/v3"

    def __init__(self, db: Database | None = None):
        self.db = db
        self.session = requests.Session()
        self._last_request_time = 0
        self._min_request_interval = 1.5  # CoinGecko rate limit

    def _rate_limit(self):
        """Respect CoinGecko rate limits."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    def _get_coingecko_id(self, asset: str) -> str:
        """Get CoinGecko ID for an asset symbol."""
        asset = asset.upper()
        if asset in COINGECKO_IDS:
            return COINGECKO_IDS[asset]
        # Try lowercase as fallback (some coins use symbol as ID)
        return asset.lower()

    def get_price(self, asset: str, timestamp: datetime) -> Decimal:
        """Fetch historical USD price for asset at timestamp."""
        asset = asset.upper()
        date_str = timestamp.strftime("%Y-%m-%d")

        # Check cache first
        if self.db:
            cached = self.db.get_cached_price(asset, date_str)
            if cached is not None:
                return cached

        # Handle stablecoins
        if asset in ("USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "GUSD"):
            price = Decimal("1.00")
            if self.db:
                self.db.cache_price(asset, date_str, price)
            return price

        # Fetch from CoinGecko
        coin_id = self._get_coingecko_id(asset)
        price = self._fetch_historical_price(coin_id, timestamp)

        # Cache the result
        if self.db and price:
            self.db.cache_price(asset, date_str, price)

        return price

    def _fetch_historical_price(
        self, coin_id: str, timestamp: datetime
    ) -> Decimal:
        """Fetch historical price from CoinGecko API."""
        self._rate_limit()

        date_str = timestamp.strftime("%d-%m-%Y")
        url = f"{self.BASE_URL}/coins/{coin_id}/history"
        params = {"date": date_str, "localization": "false"}

        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            market_data = data.get("market_data", {})
            current_price = market_data.get("current_price", {})
            usd_price = current_price.get("usd")

            if usd_price is not None:
                return Decimal(str(usd_price))

            raise ValueError(f"No price data available for {coin_id} on {date_str}")

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                raise ValueError(f"Coin '{coin_id}' not found on CoinGecko") from e
            raise
        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"Failed to fetch price from CoinGecko: {e}") from e

    def get_current_price(self, asset: str) -> Decimal:
        """Fetch current USD price for asset."""
        asset = asset.upper()

        # Handle stablecoins
        if asset in ("USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "GUSD"):
            return Decimal("1.00")

        self._rate_limit()

        coin_id = self._get_coingecko_id(asset)
        url = f"{self.BASE_URL}/simple/price"
        params = {"ids": coin_id, "vs_currencies": "usd"}

        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            if coin_id in data and "usd" in data[coin_id]:
                return Decimal(str(data[coin_id]["usd"]))

            raise ValueError(f"No price data available for {coin_id}")

        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"Failed to fetch price from CoinGecko: {e}") from e

    def get_current_prices(self, assets: list[str]) -> dict[str, Decimal]:
        """Fetch current USD prices for multiple assets."""
        prices = {}
        stablecoins = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "GUSD"}

        # Handle stablecoins
        for asset in assets:
            if asset.upper() in stablecoins:
                prices[asset.upper()] = Decimal("1.00")

        # Fetch remaining from CoinGecko
        remaining = [a for a in assets if a.upper() not in stablecoins]
        if not remaining:
            return prices

        self._rate_limit()

        coin_ids = [self._get_coingecko_id(a) for a in remaining]
        url = f"{self.BASE_URL}/simple/price"
        params = {"ids": ",".join(coin_ids), "vs_currencies": "usd"}

        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            for asset, coin_id in zip(remaining, coin_ids):
                if coin_id in data and "usd" in data[coin_id]:
                    prices[asset.upper()] = Decimal(str(data[coin_id]["usd"]))

        except requests.exceptions.RequestException:
            pass  # Return partial results

        return prices
