"""Tests for cost basis calculation engine."""

import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tax_lot_tracker.database import Database
from tax_lot_tracker.engine import CostBasisEngine, InsufficientLotsError, NoLotsError
from tax_lot_tracker.models import Transaction


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    yield db
    db.close()
    Path(db_path).unlink(missing_ok=True)


class TestCostBasisEngine:
    """Test cost basis calculation with different methods."""

    def test_fifo_basic(self, temp_db):
        """Test basic FIFO cost basis calculation."""
        engine = CostBasisEngine(temp_db, "fifo")

        # Buy 1 BTC at $10,000
        buy1 = Transaction(
            timestamp=datetime(2024, 1, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("10000"),
            fee_usd=Decimal("10"),
        )
        temp_db.add_transaction(buy1)  # Add to DB first
        engine.process_transaction(buy1)

        # Buy 1 BTC at $20,000
        buy2 = Transaction(
            timestamp=datetime(2024, 2, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("20000"),
            fee_usd=Decimal("20"),
        )
        temp_db.add_transaction(buy2)
        engine.process_transaction(buy2)

        # Sell 1 BTC at $25,000
        sell = Transaction(
            timestamp=datetime(2024, 6, 1),
            type="sell",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("25000"),
            fee_usd=Decimal("25"),
        )
        temp_db.add_transaction(sell)
        disposals = engine.process_transaction(sell)

        assert len(disposals) == 1
        disposal = disposals[0]

        # FIFO: Should use first lot (cost basis $10,010)
        assert disposal.cost_basis_usd == Decimal("10010")  # 10000 + 10 fee
        assert disposal.proceeds_usd == Decimal("24975")  # 25000 - 25 fee
        assert disposal.gain_loss_usd == Decimal("14965")  # 24975 - 10010
        assert disposal.term == "short"  # Less than 1 year

    def test_lifo_basic(self, temp_db):
        """Test basic LIFO cost basis calculation."""
        engine = CostBasisEngine(temp_db, "lifo")

        # Buy 1 BTC at $10,000
        buy1 = Transaction(
            timestamp=datetime(2024, 1, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("10000"),
            fee_usd=Decimal("10"),
        )
        temp_db.add_transaction(buy1)
        engine.process_transaction(buy1)

        # Buy 1 BTC at $20,000
        buy2 = Transaction(
            timestamp=datetime(2024, 2, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("20000"),
            fee_usd=Decimal("20"),
        )
        temp_db.add_transaction(buy2)
        engine.process_transaction(buy2)

        # Sell 1 BTC at $25,000
        sell = Transaction(
            timestamp=datetime(2024, 6, 1),
            type="sell",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("25000"),
            fee_usd=Decimal("25"),
        )
        temp_db.add_transaction(sell)
        disposals = engine.process_transaction(sell)

        assert len(disposals) == 1
        disposal = disposals[0]

        # LIFO: Should use last lot (cost basis $20,020)
        assert disposal.cost_basis_usd == Decimal("20020")  # 20000 + 20 fee
        assert disposal.proceeds_usd == Decimal("24975")  # 25000 - 25 fee
        assert disposal.gain_loss_usd == Decimal("4955")  # 24975 - 20020

    def test_hifo_basic(self, temp_db):
        """Test basic HIFO cost basis calculation."""
        engine = CostBasisEngine(temp_db, "hifo")

        # Buy 1 BTC at $20,000
        buy1 = Transaction(
            timestamp=datetime(2024, 1, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("20000"),
            fee_usd=Decimal("20"),
        )
        temp_db.add_transaction(buy1)
        engine.process_transaction(buy1)

        # Buy 1 BTC at $10,000
        buy2 = Transaction(
            timestamp=datetime(2024, 2, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("10000"),
            fee_usd=Decimal("10"),
        )
        temp_db.add_transaction(buy2)
        engine.process_transaction(buy2)

        # Sell 1 BTC at $25,000
        sell = Transaction(
            timestamp=datetime(2024, 6, 1),
            type="sell",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("25000"),
            fee_usd=Decimal("25"),
        )
        temp_db.add_transaction(sell)
        disposals = engine.process_transaction(sell)

        assert len(disposals) == 1
        disposal = disposals[0]

        # HIFO: Should use highest cost lot (first buy at $20,020)
        assert disposal.cost_basis_usd == Decimal("20020")
        assert disposal.gain_loss_usd == Decimal("4955")

    def test_partial_lot_sale(self, temp_db):
        """Test selling part of a lot."""
        engine = CostBasisEngine(temp_db, "fifo")

        # Buy 2 BTC at $10,000
        buy = Transaction(
            timestamp=datetime(2024, 1, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("2"),
            price_usd=Decimal("10000"),
            fee_usd=Decimal("20"),
        )
        temp_db.add_transaction(buy)
        engine.process_transaction(buy)

        # Sell 1 BTC at $15,000
        sell = Transaction(
            timestamp=datetime(2024, 6, 1),
            type="sell",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("15000"),
            fee_usd=Decimal("15"),
        )
        temp_db.add_transaction(sell)
        disposals = engine.process_transaction(sell)

        assert len(disposals) == 1
        disposal = disposals[0]

        # Cost basis should be half: (2 * 10000 + 20) / 2 = 10010
        assert disposal.cost_basis_usd == Decimal("10010")
        assert disposal.amount == Decimal("1")

        # Check remaining lot
        lots = temp_db.get_lots("BTC", "fifo")
        assert len(lots) == 1
        assert lots[0].amount == Decimal("1")

    def test_multiple_lot_sale(self, temp_db):
        """Test selling across multiple lots."""
        engine = CostBasisEngine(temp_db, "fifo")

        # Buy 1 BTC at $10,000
        buy1 = Transaction(
            timestamp=datetime(2024, 1, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("10000"),
            fee_usd=Decimal("10"),
        )
        temp_db.add_transaction(buy1)
        engine.process_transaction(buy1)

        # Buy 1 BTC at $20,000
        buy2 = Transaction(
            timestamp=datetime(2024, 2, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("20000"),
            fee_usd=Decimal("20"),
        )
        temp_db.add_transaction(buy2)
        engine.process_transaction(buy2)

        # Sell 1.5 BTC at $25,000
        sell = Transaction(
            timestamp=datetime(2024, 6, 1),
            type="sell",
            asset="BTC",
            amount=Decimal("1.5"),
            price_usd=Decimal("25000"),
            fee_usd=Decimal("37.50"),
        )
        temp_db.add_transaction(sell)
        disposals = engine.process_transaction(sell)

        # Should create 2 disposals
        assert len(disposals) == 2

        # First disposal: 1 BTC from lot 1
        assert disposals[0].amount == Decimal("1")
        assert disposals[0].cost_basis_usd == Decimal("10010")

        # Second disposal: 0.5 BTC from lot 2
        assert disposals[1].amount == Decimal("0.5")
        # Half of lot 2: (20000 + 20) / 2 = 10010
        assert disposals[1].cost_basis_usd == Decimal("10010")

    def test_long_term_holding(self, temp_db):
        """Test long-term capital gains (>= 1 year)."""
        engine = CostBasisEngine(temp_db, "fifo")

        # Buy 1 BTC
        buy = Transaction(
            timestamp=datetime(2023, 1, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("10000"),
            fee_usd=Decimal("10"),
        )
        temp_db.add_transaction(buy)
        engine.process_transaction(buy)

        # Sell after > 1 year
        sell = Transaction(
            timestamp=datetime(2024, 2, 1),
            type="sell",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("50000"),
            fee_usd=Decimal("50"),
        )
        temp_db.add_transaction(sell)
        disposals = engine.process_transaction(sell)

        assert len(disposals) == 1
        assert disposals[0].term == "long"

    def test_income_creates_lot(self, temp_db):
        """Test that income (staking, mining) creates a lot."""
        engine = CostBasisEngine(temp_db, "fifo")

        # Receive staking reward
        income = Transaction(
            timestamp=datetime(2024, 1, 1),
            type="income",
            asset="ETH",
            amount=Decimal("0.1"),
            price_usd=Decimal("2000"),
            fee_usd=Decimal("0"),
        )
        temp_db.add_transaction(income)
        engine.process_transaction(income)

        # Check lot was created
        lots = temp_db.get_lots("ETH", "fifo")
        assert len(lots) == 1
        assert lots[0].amount == Decimal("0.1")
        assert lots[0].cost_basis_usd == Decimal("200")  # 0.1 * 2000

    def test_insufficient_lots(self, temp_db):
        """Test error when selling more than available."""
        engine = CostBasisEngine(temp_db, "fifo")

        # Buy 1 BTC
        buy = Transaction(
            timestamp=datetime(2024, 1, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("10000"),
            fee_usd=Decimal("10"),
        )
        temp_db.add_transaction(buy)
        engine.process_transaction(buy)

        # Try to sell 2 BTC
        sell = Transaction(
            timestamp=datetime(2024, 6, 1),
            type="sell",
            asset="BTC",
            amount=Decimal("2"),
            price_usd=Decimal("25000"),
            fee_usd=Decimal("25"),
        )
        temp_db.add_transaction(sell)

        with pytest.raises(InsufficientLotsError):
            engine.process_transaction(sell)

    def test_no_lots_error(self, temp_db):
        """Test error when selling without any lots."""
        engine = CostBasisEngine(temp_db, "fifo")

        sell = Transaction(
            timestamp=datetime(2024, 6, 1),
            type="sell",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("25000"),
            fee_usd=Decimal("25"),
        )
        temp_db.add_transaction(sell)

        with pytest.raises(NoLotsError):
            engine.process_transaction(sell)

    def test_recalculate_all(self, temp_db):
        """Test recalculating all transactions."""
        engine = CostBasisEngine(temp_db, "fifo")

        # Add transactions
        buy = Transaction(
            timestamp=datetime(2024, 1, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("10000"),
            fee_usd=Decimal("10"),
        )
        temp_db.add_transaction(buy)

        sell = Transaction(
            timestamp=datetime(2024, 6, 1),
            type="sell",
            asset="BTC",
            amount=Decimal("0.5"),
            price_usd=Decimal("15000"),
            fee_usd=Decimal("7.50"),
        )
        temp_db.add_transaction(sell)

        # Recalculate
        disposals = engine.recalculate_all()

        assert len(disposals) == 1
        assert disposals[0].amount == Decimal("0.5")

    def test_invalid_method(self, temp_db):
        """Test error on invalid method."""
        with pytest.raises(ValueError, match="Invalid method"):
            CostBasisEngine(temp_db, "invalid")

    def test_transfer_no_tax_event(self, temp_db):
        """Test that transfers don't create disposals."""
        engine = CostBasisEngine(temp_db, "fifo")

        # Buy first
        buy = Transaction(
            timestamp=datetime(2024, 1, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("10000"),
            fee_usd=Decimal("10"),
        )
        temp_db.add_transaction(buy)
        engine.process_transaction(buy)

        # Transfer (not a tax event)
        transfer = Transaction(
            timestamp=datetime(2024, 2, 1),
            type="transfer",
            asset="BTC",
            amount=Decimal("0.5"),
            price_usd=Decimal("12000"),
            fee_usd=Decimal("5"),
        )
        temp_db.add_transaction(transfer)
        disposals = engine.process_transaction(transfer)

        assert len(disposals) == 0


class TestGainsCalculation:
    """Test gains/losses calculation."""

    def test_calculate_gains_by_term(self, temp_db):
        """Test gains summary by term."""
        engine = CostBasisEngine(temp_db, "fifo")

        # Short-term trade
        buy1 = Transaction(
            timestamp=datetime(2024, 1, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("10000"),
            fee_usd=Decimal("0"),
        )
        temp_db.add_transaction(buy1)
        engine.process_transaction(buy1)

        sell1 = Transaction(
            timestamp=datetime(2024, 6, 1),
            type="sell",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("15000"),
            fee_usd=Decimal("0"),
        )
        temp_db.add_transaction(sell1)
        engine.process_transaction(sell1)

        # Long-term trade
        buy2 = Transaction(
            timestamp=datetime(2023, 1, 1),
            type="buy",
            asset="ETH",
            amount=Decimal("10"),
            price_usd=Decimal("1000"),
            fee_usd=Decimal("0"),
        )
        temp_db.add_transaction(buy2)
        engine.process_transaction(buy2)

        sell2 = Transaction(
            timestamp=datetime(2024, 3, 1),
            type="sell",
            asset="ETH",
            amount=Decimal("10"),
            price_usd=Decimal("2000"),
            fee_usd=Decimal("0"),
        )
        temp_db.add_transaction(sell2)
        engine.process_transaction(sell2)

        # Calculate gains
        gains = engine.calculate_gains(2024)

        assert gains["short_term"]["net"] == Decimal("5000")  # 15000 - 10000
        assert gains["long_term"]["net"] == Decimal("10000")  # 20000 - 10000
        assert gains["total"]["net"] == Decimal("15000")

    def test_gains_with_losses(self, temp_db):
        """Test gains calculation with losses."""
        engine = CostBasisEngine(temp_db, "fifo")

        # Buy high
        buy = Transaction(
            timestamp=datetime(2024, 1, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("50000"),
            fee_usd=Decimal("0"),
        )
        temp_db.add_transaction(buy)
        engine.process_transaction(buy)

        # Sell low (loss)
        sell = Transaction(
            timestamp=datetime(2024, 6, 1),
            type="sell",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("30000"),
            fee_usd=Decimal("0"),
        )
        temp_db.add_transaction(sell)
        engine.process_transaction(sell)

        gains = engine.calculate_gains(2024)

        assert gains["short_term"]["losses"] == Decimal("20000")
        assert gains["short_term"]["gains"] == Decimal("0")
        assert gains["short_term"]["net"] == Decimal("-20000")


class TestValidation:
    """Test lot validation."""

    def test_validate_lots_clean(self, temp_db):
        """Test validation passes on clean data."""
        engine = CostBasisEngine(temp_db, "fifo")

        buy = Transaction(
            timestamp=datetime(2024, 1, 1),
            type="buy",
            asset="BTC",
            amount=Decimal("1"),
            price_usd=Decimal("10000"),
            fee_usd=Decimal("10"),
        )
        temp_db.add_transaction(buy)
        engine.process_transaction(buy)

        errors = engine.validate_lots()
        assert len(errors) == 0
