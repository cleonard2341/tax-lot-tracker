"""Cost basis calculation engine with FIFO/LIFO/HIFO methods."""

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from .database import Database, DatabaseError
from .models import Disposal, Lot, Transaction, ValidationError

logger = logging.getLogger(__name__)

# Small epsilon for comparing decimal values
EPSILON = Decimal("0.00000001")

# Precision for monetary calculations (2 decimal places for USD)
USD_PRECISION = Decimal("0.01")

# Precision for crypto amounts (8 decimal places)
CRYPTO_PRECISION = Decimal("0.00000001")


class CalculationError(Exception):
    """Raised when cost basis calculation fails."""
    pass


class InsufficientLotsError(CalculationError):
    """Raised when there aren't enough lots to fulfill a sale."""

    def __init__(self, asset: str, requested: Decimal, available: Decimal):
        self.asset = asset
        self.requested = requested
        self.available = available
        super().__init__(
            f"Insufficient {asset} lots. "
            f"Trying to sell {requested}, but only {available} available."
        )


class NoLotsError(CalculationError):
    """Raised when no lots exist for an asset being sold."""

    def __init__(self, asset: str):
        self.asset = asset
        super().__init__(
            f"No lots available for {asset}. "
            "Cannot sell asset without prior acquisition."
        )


def _round_usd(amount: Decimal) -> Decimal:
    """Round USD amount to 2 decimal places."""
    return amount.quantize(USD_PRECISION, rounding=ROUND_HALF_UP)


def _round_crypto(amount: Decimal) -> Decimal:
    """Round crypto amount to 8 decimal places."""
    return amount.quantize(CRYPTO_PRECISION, rounding=ROUND_HALF_UP)


class CostBasisEngine:
    """Engine for calculating cost basis using various methods.

    Supports FIFO (First In, First Out), LIFO (Last In, First Out),
    and HIFO (Highest In, First Out) accounting methods.
    """

    VALID_METHODS = ("fifo", "lifo", "hifo")

    def __init__(
        self,
        db: Database,
        method: Literal["fifo", "lifo", "hifo"] = "fifo",
    ):
        if not isinstance(db, Database):
            raise TypeError(f"Expected Database, got {type(db).__name__}")
        if method not in self.VALID_METHODS:
            raise ValueError(f"Invalid method: {method}. Must be one of {self.VALID_METHODS}")

        self.db = db
        self.method = method

    def process_transaction(self, tx: Transaction) -> list[Disposal]:
        """Process a single transaction, create lots or disposals.

        Args:
            tx: The transaction to process

        Returns:
            List of Disposal objects (empty for buys/income/transfers)

        Raises:
            CalculationError: If the transaction cannot be processed
            ValidationError: If the transaction data is invalid
        """
        if not isinstance(tx, Transaction):
            raise TypeError(f"Expected Transaction, got {type(tx).__name__}")

        logger.debug(f"Processing {tx.type} transaction: {tx.amount} {tx.asset}")

        try:
            if tx.type in ("buy", "income"):
                self._create_lot(tx)
                return []
            elif tx.type == "sell":
                return self._match_lots(tx)
            else:
                # Transfer type - no tax event
                logger.debug(f"Skipping transfer transaction: {tx.id}")
                return []
        except (NoLotsError, InsufficientLotsError):
            raise
        except Exception as e:
            raise CalculationError(f"Failed to process transaction {tx.id}: {e}") from e

    def _create_lot(self, tx: Transaction) -> Lot:
        """Create a new tax lot from a buy or income transaction.

        For buys: cost basis = (amount * price) + fee
        For income: cost basis = amount * price (FMV at time of receipt)
        """
        if tx.amount <= 0:
            raise ValidationError(f"Cannot create lot with zero or negative amount: {tx.amount}")

        if tx.type == "buy":
            cost_basis = (tx.amount * tx.price_usd) + tx.fee_usd
        else:  # income
            cost_basis = tx.amount * tx.price_usd

        # Ensure positive cost basis
        cost_basis = max(cost_basis, Decimal("0"))

        lot = Lot(
            asset=tx.asset.upper(),
            amount=tx.amount,
            cost_basis_usd=_round_usd(cost_basis),
            acquired_at=tx.timestamp,
            source_tx_id=tx.id,
        )

        self.db.add_lot(lot)
        logger.debug(f"Created lot {lot.id}: {lot.amount} {lot.asset} @ ${lot.cost_basis_usd}")

        return lot

    def _match_lots(self, tx: Transaction) -> list[Disposal]:
        """Match a sell transaction against lots using the configured method.

        This implements the core tax lot matching logic:
        1. Get available lots sorted by method (FIFO/LIFO/HIFO)
        2. Match sell amount against lots in order
        3. Create disposal records for each matched portion
        4. Update lot remaining amounts
        """
        if tx.amount <= 0:
            raise ValidationError(f"Cannot sell zero or negative amount: {tx.amount}")

        asset = tx.asset.upper()
        lots = self.db.get_lots(asset, self.method)

        if not lots:
            raise NoLotsError(asset)

        # Calculate total available with proper decimal precision
        total_available = sum(lot.amount for lot in lots)

        # Use epsilon comparison for decimal safety
        if total_available + EPSILON < tx.amount:
            raise InsufficientLotsError(asset, tx.amount, total_available)

        remaining = tx.amount
        disposals = []

        # Calculate proceeds per unit (price - proportional fee)
        # Fee is spread across all units sold
        total_proceeds = tx.price_usd * tx.amount
        fee_per_unit = tx.fee_usd / tx.amount if tx.amount > 0 else Decimal("0")
        proceeds_per_unit = tx.price_usd - fee_per_unit

        for lot in lots:
            if remaining <= EPSILON:
                break

            if lot.amount <= EPSILON:
                continue

            # Match as much as possible from this lot
            matched = min(lot.amount, remaining)
            matched = _round_crypto(matched)

            if matched <= EPSILON:
                continue

            # Calculate proportional cost basis from this lot
            if lot.amount > EPSILON:
                cost_basis_ratio = matched / lot.amount
                cost_basis_portion = _round_usd(cost_basis_ratio * lot.cost_basis_usd)
            else:
                cost_basis_portion = Decimal("0")

            # Calculate proceeds for this portion
            proceeds = _round_usd(matched * proceeds_per_unit)

            # Ensure non-negative values
            proceeds = max(proceeds, Decimal("0"))
            cost_basis_portion = max(cost_basis_portion, Decimal("0"))

            disposal = Disposal(
                lot_id=lot.id,
                asset=asset,
                amount=matched,
                proceeds_usd=proceeds,
                cost_basis_usd=cost_basis_portion,
                acquired_at=lot.acquired_at,
                disposed_at=tx.timestamp,
                source_tx_id=tx.id,
            )

            disposals.append(disposal)
            self.db.add_disposal(disposal)

            # Update lot remaining amount and cost basis
            new_amount = _round_crypto(lot.amount - matched)
            new_cost_basis = _round_usd(lot.cost_basis_usd - cost_basis_portion)

            # Ensure non-negative
            new_amount = max(new_amount, Decimal("0"))
            new_cost_basis = max(new_cost_basis, Decimal("0"))

            self.db.update_lot(lot.id, new_amount, new_cost_basis)

            # Update in-memory lot for subsequent iterations (important for HIFO)
            lot.amount = new_amount
            lot.cost_basis_usd = new_cost_basis

            remaining = _round_crypto(remaining - matched)

            logger.debug(
                f"Matched {matched} {asset} from lot {lot.id}, "
                f"cost basis ${cost_basis_portion}, proceeds ${proceeds}"
            )

        # Verify we matched everything (within epsilon)
        if remaining > EPSILON:
            logger.warning(
                f"Unmatched amount after processing: {remaining} {asset}. "
                "This may indicate a rounding issue."
            )

        logger.info(
            f"Processed sell of {tx.amount} {asset}: "
            f"{len(disposals)} disposals, "
            f"total gain/loss ${sum(d.gain_loss_usd for d in disposals):,.2f}"
        )

        return disposals

    def recalculate_all(self) -> list[Disposal]:
        """Wipe disposals, reset lots, and reprocess all transactions in order.

        This is useful when changing accounting methods or fixing data issues.
        All disposals are recalculated from scratch based on transaction history.

        Returns:
            List of all disposal records created
        """
        logger.info(f"Recalculating all transactions using {self.method.upper()} method")

        # Clear existing disposals
        self.db.clear_disposals()

        # Reset lot amounts to original values
        self.db.reset_lots()

        # Get all transactions sorted by timestamp
        transactions = self.db.get_transactions()
        logger.info(f"Processing {len(transactions)} transactions")

        all_disposals = []
        errors = []

        for i, tx in enumerate(transactions):
            try:
                disposals = self.process_transaction(tx)
                all_disposals.extend(disposals)

                if (i + 1) % 100 == 0:
                    logger.info(f"Processed {i + 1}/{len(transactions)} transactions")

            except CalculationError as e:
                errors.append((tx, e))
                logger.error(f"Error processing transaction {tx.id}: {e}")

        if errors:
            error_summary = "\n".join(
                f"  - {tx.timestamp.date()} {tx.type} {tx.amount} {tx.asset}: {e}"
                for tx, e in errors[:10]
            )
            if len(errors) > 10:
                error_summary += f"\n  ... and {len(errors) - 10} more errors"

            logger.warning(f"Completed with {len(errors)} errors:\n{error_summary}")

        logger.info(
            f"Recalculation complete: {len(all_disposals)} disposals created "
            f"from {len(transactions)} transactions"
        )

        return all_disposals

    def calculate_gains(self, year: int | None = None) -> dict:
        """Calculate gains/losses summary.

        Args:
            year: Optional tax year to filter by

        Returns:
            Dictionary with gains summary including:
            - short_term: Short-term gains/losses (< 1 year holding)
            - long_term: Long-term gains/losses (>= 1 year holding)
            - total: Combined totals
            - by_asset: Breakdown by asset
        """
        disposals = self.db.get_disposals(year)

        summary = {
            "year": year,
            "disposal_count": len(disposals),
            "short_term": {
                "gains": Decimal("0"),
                "losses": Decimal("0"),
                "net": Decimal("0"),
                "count": 0,
                "proceeds": Decimal("0"),
                "cost_basis": Decimal("0"),
            },
            "long_term": {
                "gains": Decimal("0"),
                "losses": Decimal("0"),
                "net": Decimal("0"),
                "count": 0,
                "proceeds": Decimal("0"),
                "cost_basis": Decimal("0"),
            },
            "total": {
                "proceeds": Decimal("0"),
                "cost_basis": Decimal("0"),
                "net": Decimal("0"),
            },
            "by_asset": {},
        }

        for disposal in disposals:
            term = disposal.term
            term_key = f"{term}_term"
            gain_loss = disposal.gain_loss_usd

            # Update term-specific stats
            summary[term_key]["count"] += 1
            summary[term_key]["proceeds"] += disposal.proceeds_usd
            summary[term_key]["cost_basis"] += disposal.cost_basis_usd
            summary[term_key]["net"] += gain_loss

            if gain_loss >= 0:
                summary[term_key]["gains"] += gain_loss
            else:
                summary[term_key]["losses"] += abs(gain_loss)

            # Update totals
            summary["total"]["proceeds"] += disposal.proceeds_usd
            summary["total"]["cost_basis"] += disposal.cost_basis_usd
            summary["total"]["net"] += gain_loss

            # Track by asset
            asset = disposal.asset
            if asset not in summary["by_asset"]:
                summary["by_asset"][asset] = {
                    "proceeds": Decimal("0"),
                    "cost_basis": Decimal("0"),
                    "net": Decimal("0"),
                    "count": 0,
                    "short_term_count": 0,
                    "long_term_count": 0,
                }

            asset_stats = summary["by_asset"][asset]
            asset_stats["proceeds"] += disposal.proceeds_usd
            asset_stats["cost_basis"] += disposal.cost_basis_usd
            asset_stats["net"] += gain_loss
            asset_stats["count"] += 1
            asset_stats[f"{term}_term_count"] += 1

        # Round all monetary values
        for key in ("short_term", "long_term"):
            for field in ("gains", "losses", "net", "proceeds", "cost_basis"):
                summary[key][field] = _round_usd(summary[key][field])

        for field in ("proceeds", "cost_basis", "net"):
            summary["total"][field] = _round_usd(summary["total"][field])

        for asset_stats in summary["by_asset"].values():
            for field in ("proceeds", "cost_basis", "net"):
                asset_stats[field] = _round_usd(asset_stats[field])

        return summary

    def validate_lots(self) -> list[str]:
        """Validate lot data integrity.

        Returns:
            List of validation error messages (empty if all valid)
        """
        errors = []

        lots = self.db.get_all_lots(include_depleted=True)

        for lot in lots:
            # Check for negative amounts
            if lot.amount < 0:
                errors.append(f"Lot {lot.id} has negative amount: {lot.amount}")

            # Check for negative cost basis
            if lot.cost_basis_usd < 0:
                errors.append(f"Lot {lot.id} has negative cost basis: {lot.cost_basis_usd}")

            # Verify source transaction exists
            tx = self.db.get_transaction(lot.source_tx_id)
            if not tx:
                errors.append(f"Lot {lot.id} references missing transaction: {lot.source_tx_id}")
            elif tx.asset.upper() != lot.asset.upper():
                errors.append(
                    f"Lot {lot.id} asset mismatch: lot={lot.asset}, tx={tx.asset}"
                )

        return errors

    def get_unrealized_gains(self, current_prices: dict[str, Decimal]) -> dict:
        """Calculate unrealized gains based on current prices.

        Args:
            current_prices: Dict mapping asset symbols to current USD prices

        Returns:
            Dictionary with unrealized gains by asset
        """
        portfolio = self.db.get_portfolio()
        result = {
            "total_cost_basis": Decimal("0"),
            "total_market_value": Decimal("0"),
            "total_unrealized": Decimal("0"),
            "by_asset": {},
        }

        for asset, data in portfolio.items():
            cost_basis = data["cost_basis"]
            amount = data["amount"]

            current_price = current_prices.get(asset.upper(), Decimal("0"))
            market_value = amount * current_price
            unrealized = market_value - cost_basis

            result["by_asset"][asset] = {
                "amount": amount,
                "cost_basis": _round_usd(cost_basis),
                "market_value": _round_usd(market_value),
                "unrealized": _round_usd(unrealized),
                "current_price": current_price,
            }

            result["total_cost_basis"] += cost_basis
            result["total_market_value"] += market_value
            result["total_unrealized"] += unrealized

        result["total_cost_basis"] = _round_usd(result["total_cost_basis"])
        result["total_market_value"] = _round_usd(result["total_market_value"])
        result["total_unrealized"] = _round_usd(result["total_unrealized"])

        return result
