"""Data models for tax lot tracking."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
import uuid


class ValidationError(Exception):
    """Raised when model validation fails."""
    pass


def _validate_decimal(value: Decimal, field_name: str, allow_negative: bool = False) -> Decimal:
    """Validate a decimal value."""
    if not isinstance(value, Decimal):
        raise ValidationError(f"{field_name} must be a Decimal, got {type(value).__name__}")
    if value.is_nan() or value.is_infinite():
        raise ValidationError(f"{field_name} cannot be NaN or Infinite")
    if not allow_negative and value < 0:
        raise ValidationError(f"{field_name} cannot be negative: {value}")
    return value


def _validate_string(value: str, field_name: str, max_length: int = 255) -> str:
    """Validate a string value."""
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string, got {type(value).__name__}")
    if not value or not value.strip():
        raise ValidationError(f"{field_name} cannot be empty")
    if len(value) > max_length:
        raise ValidationError(f"{field_name} exceeds maximum length of {max_length}")
    return value.strip()


def _validate_datetime(value: datetime, field_name: str) -> datetime:
    """Validate a datetime value."""
    if not isinstance(value, datetime):
        raise ValidationError(f"{field_name} must be a datetime, got {type(value).__name__}")
    # Sanity check: dates shouldn't be too far in past or future
    min_date = datetime(2009, 1, 3)  # Bitcoin genesis block
    max_date = datetime(2100, 1, 1)
    if value < min_date:
        raise ValidationError(f"{field_name} cannot be before Bitcoin genesis (2009-01-03)")
    if value > max_date:
        raise ValidationError(f"{field_name} is too far in the future")
    return value


@dataclass
class Transaction:
    """Represents a cryptocurrency transaction."""

    timestamp: datetime
    type: Literal["buy", "sell", "transfer", "income"]
    asset: str  # e.g., "BTC", "ETH"
    amount: Decimal
    price_usd: Decimal  # Price per unit at time
    fee_usd: Decimal = Decimal("0")
    source: str = "manual"  # "coinbase", "binance", "csv", etc.
    raw_data: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self):
        """Validate transaction data after initialization."""
        self.validate()

    def validate(self) -> None:
        """Validate all transaction fields."""
        _validate_datetime(self.timestamp, "timestamp")

        if self.type not in ("buy", "sell", "transfer", "income"):
            raise ValidationError(f"Invalid transaction type: {self.type}")

        self.asset = _validate_string(self.asset, "asset", max_length=20).upper()

        _validate_decimal(self.amount, "amount")
        if self.amount <= 0:
            raise ValidationError(f"amount must be positive: {self.amount}")

        _validate_decimal(self.price_usd, "price_usd")
        _validate_decimal(self.fee_usd, "fee_usd")

        self.source = _validate_string(self.source, "source", max_length=50)

        if not isinstance(self.raw_data, dict):
            raise ValidationError(f"raw_data must be a dict, got {type(self.raw_data).__name__}")

        if not self.id or not isinstance(self.id, str):
            raise ValidationError("id must be a non-empty string")

    @property
    def total_usd(self) -> Decimal:
        """Total value of the transaction in USD."""
        return self.amount * self.price_usd

    @property
    def cost_with_fee(self) -> Decimal:
        """Total cost including fees (for buys)."""
        return self.total_usd + self.fee_usd

    @property
    def proceeds_minus_fee(self) -> Decimal:
        """Proceeds after fees (for sells)."""
        return self.total_usd - self.fee_usd


@dataclass
class Lot:
    """Represents a tax lot - a batch of crypto acquired at the same time."""

    asset: str
    amount: Decimal  # Remaining amount (decreases on sells)
    cost_basis_usd: Decimal  # Total cost including fees
    acquired_at: datetime
    source_tx_id: str  # Link to Transaction
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self):
        """Validate lot data after initialization."""
        self.validate()

    def validate(self) -> None:
        """Validate all lot fields."""
        self.asset = _validate_string(self.asset, "asset", max_length=20).upper()
        _validate_decimal(self.amount, "amount")
        _validate_decimal(self.cost_basis_usd, "cost_basis_usd")
        _validate_datetime(self.acquired_at, "acquired_at")
        _validate_string(self.source_tx_id, "source_tx_id")

        if not self.id or not isinstance(self.id, str):
            raise ValidationError("id must be a non-empty string")

    @property
    def cost_per_unit(self) -> Decimal:
        """Cost basis per unit of asset."""
        if self.amount <= 0:
            return Decimal("0")
        return self.cost_basis_usd / self.amount

    def is_depleted(self) -> bool:
        """Check if lot has been fully disposed."""
        return self.amount <= Decimal("0")


@dataclass
class Disposal:
    """Represents a disposal event - selling or exchanging crypto."""

    lot_id: str
    asset: str
    amount: Decimal
    proceeds_usd: Decimal
    cost_basis_usd: Decimal
    acquired_at: datetime
    disposed_at: datetime
    source_tx_id: str  # Link to sell Transaction
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self):
        """Validate disposal data after initialization."""
        self.validate()

    def validate(self) -> None:
        """Validate all disposal fields."""
        _validate_string(self.lot_id, "lot_id")
        self.asset = _validate_string(self.asset, "asset", max_length=20).upper()

        _validate_decimal(self.amount, "amount")
        if self.amount <= 0:
            raise ValidationError(f"amount must be positive: {self.amount}")

        _validate_decimal(self.proceeds_usd, "proceeds_usd")
        _validate_decimal(self.cost_basis_usd, "cost_basis_usd")

        _validate_datetime(self.acquired_at, "acquired_at")
        _validate_datetime(self.disposed_at, "disposed_at")

        if self.disposed_at < self.acquired_at:
            raise ValidationError("disposed_at cannot be before acquired_at")

        _validate_string(self.source_tx_id, "source_tx_id")

        if not self.id or not isinstance(self.id, str):
            raise ValidationError("id must be a non-empty string")

    @property
    def gain_loss_usd(self) -> Decimal:
        """Calculate gain or loss."""
        return self.proceeds_usd - self.cost_basis_usd

    @property
    def term(self) -> Literal["short", "long"]:
        """Determine if short-term (<1 year) or long-term (>=1 year)."""
        holding_period = self.disposed_at - self.acquired_at
        if holding_period.days >= 365:
            return "long"
        return "short"

    @property
    def holding_days(self) -> int:
        """Number of days the asset was held."""
        return (self.disposed_at - self.acquired_at).days

    @property
    def is_gain(self) -> bool:
        """Check if this disposal resulted in a gain."""
        return self.gain_loss_usd > 0
