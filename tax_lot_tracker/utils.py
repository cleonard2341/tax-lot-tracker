"""Utility functions for tax lot tracker."""

import hashlib
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Union

from dateutil import parser as date_parser
from dateutil.parser import ParserError

logger = logging.getLogger(__name__)


class ParseError(Exception):
    """Raised when parsing fails."""
    pass


def parse_datetime(date_string: str, strict: bool = False) -> datetime:
    """Parse a datetime string in various formats.

    Args:
        date_string: The string to parse
        strict: If True, raise on ambiguous dates

    Returns:
        Parsed datetime object

    Raises:
        ParseError: If the string cannot be parsed
    """
    if not date_string:
        raise ParseError("Empty date string")

    if not isinstance(date_string, str):
        raise ParseError(f"Expected string, got {type(date_string).__name__}")

    date_string = date_string.strip()

    # Try common formats first for better performance
    common_formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
    ]

    for fmt in common_formats:
        try:
            return datetime.strptime(date_string, fmt)
        except ValueError:
            continue

    # Fall back to dateutil parser
    try:
        parsed = date_parser.parse(date_string, dayfirst=False)

        # Sanity check the result
        if parsed.year < 2009 or parsed.year > 2100:
            raise ParseError(f"Parsed year {parsed.year} is out of valid range")

        return parsed
    except (ParserError, ValueError, OverflowError) as e:
        raise ParseError(f"Cannot parse date '{date_string}': {e}") from e


def parse_decimal(value: Union[str, float, int, Decimal], allow_negative: bool = True) -> Decimal:
    """Parse a value to Decimal, handling various formats.

    Args:
        value: The value to parse
        allow_negative: If False, raises on negative values

    Returns:
        Decimal value

    Raises:
        ParseError: If the value cannot be parsed
    """
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, float)):
        if not isinstance(value, bool):  # bool is subclass of int
            result = Decimal(str(value))
        else:
            raise ParseError(f"Cannot convert boolean to Decimal")
    elif isinstance(value, str):
        result = _parse_decimal_string(value)
    else:
        raise ParseError(f"Cannot convert {type(value).__name__} to Decimal")

    # Validate result
    if result.is_nan():
        raise ParseError("Value is NaN")
    if result.is_infinite():
        raise ParseError("Value is infinite")
    if not allow_negative and result < 0:
        raise ParseError(f"Negative value not allowed: {result}")

    return result


def _parse_decimal_string(value: str) -> Decimal:
    """Parse a string to Decimal with format handling."""
    if not value:
        raise ParseError("Empty string cannot be parsed as decimal")

    original = value
    value = value.strip()

    # Remove currency symbols
    value = re.sub(r'^[$€£¥₹]', '', value)
    value = re.sub(r'[$€£¥₹]$', '', value)

    # Handle accounting notation: (1234.56) means -1234.56
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]

    # Remove thousand separators
    # Be careful: 1,234.56 vs 1.234,56 (European)
    if "," in value and "." in value:
        # If comma comes before period, comma is thousand separator
        if value.index(",") < value.rindex("."):
            value = value.replace(",", "")
        else:
            # European format: period is thousand separator, comma is decimal
            value = value.replace(".", "").replace(",", ".")
    elif "," in value:
        # Only commas - could be thousand separator or decimal
        # If 3 digits after last comma, it's probably a thousand separator
        parts = value.split(",")
        if len(parts[-1]) == 3 and len(parts) > 1:
            value = value.replace(",", "")
        else:
            # Assume European decimal
            value = value.replace(",", ".")

    # Remove any remaining whitespace
    value = value.replace(" ", "")

    # Validate format
    if not re.match(r'^-?\d*\.?\d+$', value):
        raise ParseError(f"Cannot parse '{original}' as decimal")

    try:
        return Decimal(value)
    except InvalidOperation as e:
        raise ParseError(f"Cannot parse '{original}' as decimal: {e}") from e


def normalize_asset(asset: str) -> str:
    """Normalize asset symbol to uppercase.

    Args:
        asset: Asset symbol to normalize

    Returns:
        Uppercase trimmed asset symbol

    Raises:
        ParseError: If asset is empty or invalid
    """
    if not asset:
        raise ParseError("Asset symbol cannot be empty")
    if not isinstance(asset, str):
        raise ParseError(f"Asset must be string, got {type(asset).__name__}")

    normalized = asset.strip().upper()

    if not normalized:
        raise ParseError("Asset symbol cannot be empty")

    # Basic validation - alphanumeric only
    if not re.match(r'^[A-Z0-9]+$', normalized):
        # Allow some common variations
        normalized = re.sub(r'[^A-Z0-9]', '', normalized)
        if not normalized:
            raise ParseError(f"Invalid asset symbol: '{asset}'")

    if len(normalized) > 20:
        raise ParseError(f"Asset symbol too long: '{asset}'")

    return normalized


def generate_tx_id(source: str, *args) -> str:
    """Generate a deterministic transaction ID from source data.

    Args:
        source: Source identifier (e.g., 'csv', 'coinbase')
        *args: Additional values to include in hash

    Returns:
        16-character hex ID
    """
    if not source:
        source = "unknown"

    # Convert all args to strings, handling None and special types
    str_args = []
    for arg in args:
        if arg is None:
            str_args.append("")
        elif isinstance(arg, datetime):
            str_args.append(arg.isoformat())
        elif isinstance(arg, Decimal):
            str_args.append(str(arg))
        else:
            str_args.append(str(arg))

    data = f"{source}:" + ":".join(str_args)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def format_currency(amount: Decimal, symbol: str = "$", precision: int = 2) -> str:
    """Format a decimal as currency.

    Args:
        amount: The amount to format
        symbol: Currency symbol
        precision: Decimal places

    Returns:
        Formatted currency string
    """
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))

    if amount < 0:
        return f"-{symbol}{abs(amount):,.{precision}f}"
    return f"{symbol}{amount:,.{precision}f}"


def format_crypto(amount: Decimal, asset: str, max_decimals: int = 8) -> str:
    """Format a crypto amount with appropriate precision.

    Args:
        amount: The amount to format
        asset: Asset symbol
        max_decimals: Maximum decimal places to show

    Returns:
        Formatted amount string
    """
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))

    if amount == 0:
        return f"0 {asset}"

    abs_amount = abs(amount)

    # Determine appropriate precision
    if abs_amount >= 1000:
        decimals = 2
    elif abs_amount >= 1:
        decimals = 4
    elif abs_amount >= Decimal("0.0001"):
        decimals = 6
    else:
        decimals = max_decimals

    # Format and strip trailing zeros
    formatted = f"{amount:.{decimals}f}".rstrip("0").rstrip(".")

    return f"{formatted} {asset}"


def is_same_day(dt1: datetime, dt2: datetime) -> bool:
    """Check if two datetimes are on the same day."""
    if not isinstance(dt1, datetime) or not isinstance(dt2, datetime):
        return False
    return dt1.date() == dt2.date()


def holding_period_days(acquired: datetime, disposed: datetime) -> int:
    """Calculate holding period in days.

    Args:
        acquired: Acquisition date
        disposed: Disposal date

    Returns:
        Number of days held

    Raises:
        ValueError: If disposed is before acquired
    """
    if not isinstance(acquired, datetime) or not isinstance(disposed, datetime):
        raise ValueError("Both arguments must be datetime objects")

    days = (disposed - acquired).days
    if days < 0:
        raise ValueError("Disposed date cannot be before acquired date")

    return days


def is_long_term(acquired: datetime, disposed: datetime) -> bool:
    """Check if a disposal qualifies as long-term (>= 1 year).

    Args:
        acquired: Acquisition date
        disposed: Disposal date

    Returns:
        True if held >= 365 days
    """
    return holding_period_days(acquired, disposed) >= 365


def sanitize_filename(filename: str, max_length: int = 100) -> str:
    """Sanitize a string for use as a filename.

    Args:
        filename: Original filename
        max_length: Maximum length

    Returns:
        Sanitized filename
    """
    if not filename:
        return "unnamed"

    # Remove or replace invalid characters
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', filename)

    # Remove leading/trailing spaces and dots
    sanitized = sanitized.strip(". ")

    # Truncate if too long
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length]

    return sanitized or "unnamed"


def validate_year(year: int) -> int:
    """Validate a tax year.

    Args:
        year: Year to validate

    Returns:
        Validated year

    Raises:
        ValueError: If year is invalid
    """
    if not isinstance(year, int):
        raise ValueError(f"Year must be an integer, got {type(year).__name__}")

    # Reasonable range for tax years
    if year < 2009:  # Bitcoin genesis
        raise ValueError(f"Year {year} is before cryptocurrency existed")
    if year > datetime.now().year + 1:
        raise ValueError(f"Year {year} is too far in the future")

    return year
