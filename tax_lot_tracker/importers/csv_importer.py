"""Generic CSV importer for transactions."""

import csv
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from ..models import Transaction, ValidationError
from ..utils import ParseError, generate_tx_id, normalize_asset, parse_datetime, parse_decimal
from .base import BaseImporter

logger = logging.getLogger(__name__)


class CSVImportError(Exception):
    """Raised when CSV import fails."""

    def __init__(self, message: str, row_number: int | None = None, row_data: dict | None = None):
        self.row_number = row_number
        self.row_data = row_data
        if row_number:
            message = f"Row {row_number}: {message}"
        super().__init__(message)


class CSVImporter(BaseImporter):
    """Import transactions from CSV files.

    Supports flexible column names and various date/number formats.
    """

    # Expected column names (case-insensitive, in order of preference)
    COLUMN_MAPPINGS = {
        "timestamp": ["timestamp", "date", "time", "datetime", "date/time", "trade_date", "created_at"],
        "type": ["type", "transaction type", "transaction_type", "side", "action", "tx_type"],
        "asset": ["asset", "symbol", "currency", "coin", "crypto", "base_currency", "ticker"],
        "amount": ["amount", "quantity", "qty", "size", "volume", "base_amount"],
        "price": ["price", "price_usd", "price usd", "unit price", "rate", "unit_price", "spot_price"],
        "fee": ["fee", "fee_usd", "fee usd", "commission", "fees", "trading_fee"],
        "total": ["total", "total_usd", "total usd", "value", "cost", "subtotal", "quote_amount"],
    }

    TYPE_MAPPINGS = {
        "buy": ["buy", "purchase", "bought", "market_buy", "limit_buy", "acquire"],
        "sell": ["sell", "sold", "sale", "market_sell", "limit_sell", "dispose"],
        "income": ["income", "reward", "staking", "mining", "interest", "earn", "airdrop", "fork", "dividend"],
        "transfer": ["transfer", "send", "receive", "deposit", "withdrawal", "withdraw", "move"],
    }

    # Maximum file size to process (50MB)
    MAX_FILE_SIZE = 50 * 1024 * 1024

    def __init__(self, file_path: str | Path):
        self.file_path = Path(file_path)

        if not self.file_path.exists():
            raise FileNotFoundError(f"CSV file not found: {self.file_path}")

        if not self.file_path.is_file():
            raise CSVImportError(f"Path is not a file: {self.file_path}")

        # Check file size
        file_size = self.file_path.stat().st_size
        if file_size > self.MAX_FILE_SIZE:
            raise CSVImportError(
                f"File too large: {file_size / 1024 / 1024:.1f}MB "
                f"(max {self.MAX_FILE_SIZE / 1024 / 1024:.0f}MB)"
            )

        if file_size == 0:
            raise CSVImportError("File is empty")

    @property
    def source_name(self) -> str:
        return "csv"

    def fetch_transactions(
        self, since: datetime | None = None
    ) -> list[Transaction]:
        """Parse transactions from the CSV file.

        Args:
            since: Optional datetime to filter transactions

        Returns:
            List of Transaction objects sorted by timestamp

        Raises:
            CSVImportError: If parsing fails
        """
        transactions = []
        errors = []

        try:
            with open(self.file_path, newline="", encoding="utf-8-sig") as f:
                # Detect delimiter
                sample = f.read(8192)
                f.seek(0)

                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel

                reader = csv.DictReader(f, dialect=dialect)

                if not reader.fieldnames:
                    raise CSVImportError("CSV file has no header row")

                # Map columns
                try:
                    column_map = self._map_columns(reader.fieldnames)
                except CSVImportError:
                    raise

                logger.info(f"Mapped columns: {column_map}")

                for row_num, row in enumerate(reader, start=2):
                    try:
                        tx = self._parse_row(row, column_map, row_num)
                        if tx is not None:
                            if since is None or tx.timestamp >= since:
                                transactions.append(tx)
                    except (ParseError, ValidationError, CSVImportError) as e:
                        errors.append((row_num, str(e), row))
                        logger.warning(f"Skipping row {row_num}: {e}")

        except UnicodeDecodeError as e:
            # Try with different encoding
            try:
                return self._parse_with_encoding("latin-1", since)
            except Exception:
                raise CSVImportError(f"Cannot decode file: {e}") from e

        if errors and len(errors) > len(transactions):
            # More errors than successes - likely wrong format
            sample_errors = errors[:3]
            error_msg = "\n".join(f"  Row {r}: {e}" for r, e, _ in sample_errors)
            raise CSVImportError(
                f"Too many parsing errors ({len(errors)} errors, {len(transactions)} successes). "
                f"Sample errors:\n{error_msg}"
            )

        if errors:
            logger.warning(f"Completed with {len(errors)} errors out of {len(transactions) + len(errors)} rows")

        return sorted(transactions, key=lambda t: (t.timestamp, t.id))

    def _parse_with_encoding(self, encoding: str, since: datetime | None) -> list[Transaction]:
        """Try parsing with a different encoding."""
        transactions = []

        with open(self.file_path, newline="", encoding=encoding) as f:
            dialect = csv.excel
            reader = csv.DictReader(f, dialect=dialect)

            if not reader.fieldnames:
                raise CSVImportError("CSV file has no header row")

            column_map = self._map_columns(reader.fieldnames)

            for row_num, row in enumerate(reader, start=2):
                try:
                    tx = self._parse_row(row, column_map, row_num)
                    if tx is not None:
                        if since is None or tx.timestamp >= since:
                            transactions.append(tx)
                except Exception as e:
                    logger.warning(f"Skipping row {row_num}: {e}")

        return sorted(transactions, key=lambda t: (t.timestamp, t.id))

    def _map_columns(self, fieldnames: list[str]) -> dict[str, str]:
        """Map CSV columns to expected fields.

        Args:
            fieldnames: List of column names from CSV

        Returns:
            Dict mapping our field names to CSV column names

        Raises:
            CSVImportError: If required columns are missing
        """
        if not fieldnames:
            raise CSVImportError("No column headers found")

        column_map = {}
        fieldnames_lower = {f.lower().strip(): f for f in fieldnames if f}

        for field, aliases in self.COLUMN_MAPPINGS.items():
            for alias in aliases:
                if alias in fieldnames_lower:
                    column_map[field] = fieldnames_lower[alias]
                    break

        # Validate required columns
        required = ["timestamp", "type", "asset", "amount"]
        missing = [f for f in required if f not in column_map]

        if missing:
            raise CSVImportError(
                f"Missing required columns: {missing}. "
                f"Expected one of: {[self.COLUMN_MAPPINGS[m] for m in missing]}. "
                f"Found columns: {list(fieldnames)}"
            )

        # Must have either price or total
        if "price" not in column_map and "total" not in column_map:
            raise CSVImportError(
                "Missing price column. Need either 'price' or 'total' column. "
                f"Found columns: {list(fieldnames)}"
            )

        return column_map

    def _parse_row(
        self, row: dict, column_map: dict[str, str], row_num: int
    ) -> Transaction | None:
        """Parse a single CSV row into a Transaction.

        Args:
            row: CSV row as dict
            column_map: Column name mapping
            row_num: Row number for error messages

        Returns:
            Transaction object or None for empty/skippable rows

        Raises:
            CSVImportError: If parsing fails
        """
        # Get raw values
        timestamp_str = self._get_value(row, column_map, "timestamp")
        type_str = self._get_value(row, column_map, "type")
        asset_str = self._get_value(row, column_map, "asset")
        amount_str = self._get_value(row, column_map, "amount")

        # Skip empty rows
        if not timestamp_str or not asset_str or not amount_str:
            return None

        # Parse timestamp
        try:
            timestamp = parse_datetime(timestamp_str)
        except ParseError as e:
            raise CSVImportError(f"Invalid date '{timestamp_str}': {e}", row_num)

        # Parse transaction type
        try:
            tx_type = self._normalize_type(type_str)
        except ValueError as e:
            raise CSVImportError(str(e), row_num)

        # Parse asset
        try:
            asset = normalize_asset(asset_str)
        except ParseError as e:
            raise CSVImportError(f"Invalid asset '{asset_str}': {e}", row_num)

        # Parse amount (always positive)
        try:
            amount = abs(parse_decimal(amount_str))
        except ParseError as e:
            raise CSVImportError(f"Invalid amount '{amount_str}': {e}", row_num)

        if amount <= 0:
            return None  # Skip zero-amount transactions

        # Parse price - either from price column or calculated from total
        price_usd = Decimal("0")
        price_str = self._get_value(row, column_map, "price")
        total_str = self._get_value(row, column_map, "total")

        try:
            if price_str:
                price_usd = abs(parse_decimal(price_str))
            elif total_str:
                total = abs(parse_decimal(total_str))
                if amount > 0:
                    price_usd = total / amount
            else:
                raise CSVImportError("No price or total value found", row_num)
        except ParseError as e:
            raise CSVImportError(f"Invalid price/total value: {e}", row_num)

        # Parse fee (optional)
        fee_usd = Decimal("0")
        fee_str = self._get_value(row, column_map, "fee")
        if fee_str:
            try:
                fee_usd = abs(parse_decimal(fee_str))
            except ParseError:
                # Fee parsing failure is non-fatal
                logger.debug(f"Row {row_num}: Could not parse fee '{fee_str}'")

        # Generate deterministic ID
        tx_id = generate_tx_id(
            "csv",
            self.file_path.name,
            row_num,
            timestamp_str,
            asset_str,
            amount_str,
        )

        try:
            return Transaction(
                id=tx_id,
                timestamp=timestamp,
                type=tx_type,
                asset=asset,
                amount=amount,
                price_usd=price_usd,
                fee_usd=fee_usd,
                source=self.source_name,
                raw_data=dict(row),
            )
        except ValidationError as e:
            raise CSVImportError(f"Invalid transaction data: {e}", row_num)

    def _get_value(self, row: dict, column_map: dict, field: str) -> str:
        """Get a value from a row using the column map.

        Args:
            row: CSV row dict
            column_map: Column name mapping
            field: Field name to get

        Returns:
            Stripped string value or empty string
        """
        col_name = column_map.get(field)
        if not col_name:
            return ""

        value = row.get(col_name, "")
        if value is None:
            return ""

        return str(value).strip()

    def _normalize_type(
        self, type_str: str
    ) -> Literal["buy", "sell", "transfer", "income"]:
        """Normalize transaction type string.

        Args:
            type_str: Raw type string from CSV

        Returns:
            Normalized type

        Raises:
            ValueError: If type is unknown
        """
        if not type_str:
            raise ValueError("Transaction type is required")

        type_lower = type_str.lower().strip()

        for tx_type, aliases in self.TYPE_MAPPINGS.items():
            if type_lower in aliases:
                return tx_type

        # Try partial matching for common variations
        if "buy" in type_lower or "purchase" in type_lower:
            return "buy"
        if "sell" in type_lower or "sale" in type_lower:
            return "sell"
        if "stake" in type_lower or "reward" in type_lower or "interest" in type_lower:
            return "income"
        if "transfer" in type_lower or "send" in type_lower or "receive" in type_lower:
            return "transfer"
        if "deposit" in type_lower:
            return "transfer"
        if "withdraw" in type_lower:
            return "transfer"

        raise ValueError(f"Unknown transaction type: '{type_str}'")

    def validate_file(self) -> dict:
        """Validate CSV file without importing.

        Returns:
            Dict with validation results including row count and any issues
        """
        result = {
            "valid": True,
            "row_count": 0,
            "column_count": 0,
            "columns": [],
            "mapped_columns": {},
            "sample_rows": [],
            "issues": [],
        }

        try:
            with open(self.file_path, newline="", encoding="utf-8-sig") as f:
                sample = f.read(8192)
                f.seek(0)

                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel

                reader = csv.DictReader(f, dialect=dialect)

                if reader.fieldnames:
                    result["columns"] = list(reader.fieldnames)
                    result["column_count"] = len(reader.fieldnames)

                    try:
                        result["mapped_columns"] = self._map_columns(reader.fieldnames)
                    except CSVImportError as e:
                        result["issues"].append(str(e))
                        result["valid"] = False

                # Count rows and get samples
                for i, row in enumerate(reader):
                    result["row_count"] += 1
                    if i < 3:
                        result["sample_rows"].append(dict(row))

        except Exception as e:
            result["valid"] = False
            result["issues"].append(f"Error reading file: {e}")

        return result
