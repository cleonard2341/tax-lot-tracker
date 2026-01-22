"""Tests for tax report generation."""

import csv
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tax_lot_tracker.database import Database
from tax_lot_tracker.engine import CostBasisEngine
from tax_lot_tracker.models import Transaction
from tax_lot_tracker.reports import ReportGenerator


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    yield db
    db.close()
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def sample_data(temp_db):
    """Create sample transaction data."""
    engine = CostBasisEngine(temp_db, "fifo")

    # Short-term BTC trade (gain)
    buy1 = Transaction(
        timestamp=datetime(2024, 1, 1),
        type="buy",
        asset="BTC",
        amount=Decimal("1"),
        price_usd=Decimal("40000"),
        fee_usd=Decimal("40"),
    )
    temp_db.add_transaction(buy1)
    engine.process_transaction(buy1)

    sell1 = Transaction(
        timestamp=datetime(2024, 6, 1),
        type="sell",
        asset="BTC",
        amount=Decimal("1"),
        price_usd=Decimal("60000"),
        fee_usd=Decimal("60"),
    )
    temp_db.add_transaction(sell1)
    engine.process_transaction(sell1)

    # Long-term ETH trade (loss)
    buy2 = Transaction(
        timestamp=datetime(2023, 1, 1),
        type="buy",
        asset="ETH",
        amount=Decimal("10"),
        price_usd=Decimal("2000"),
        fee_usd=Decimal("20"),
    )
    temp_db.add_transaction(buy2)
    engine.process_transaction(buy2)

    sell2 = Transaction(
        timestamp=datetime(2024, 3, 1),
        type="sell",
        asset="ETH",
        amount=Decimal("10"),
        price_usd=Decimal("1500"),
        fee_usd=Decimal("15"),
    )
    temp_db.add_transaction(sell2)
    engine.process_transaction(sell2)

    return temp_db


class TestReportGenerator:
    """Test report generation."""

    def test_generate_summary(self, sample_data):
        """Test generating a summary report."""
        report_gen = ReportGenerator(sample_data)
        summary = report_gen.generate_summary(2024)

        assert summary["year"] == 2024
        assert summary["total_transactions"] == 2

        # Short-term: BTC gain
        # Proceeds: 60000 - 60 = 59940
        # Cost: 40000 + 40 = 40040
        # Gain: 19900
        assert summary["short_term"]["count"] == 1
        assert summary["short_term"]["proceeds"] == Decimal("59940")
        assert summary["short_term"]["cost_basis"] == Decimal("40040")
        assert summary["short_term"]["gains"] == Decimal("19900")
        assert summary["short_term"]["losses"] == Decimal("0")

        # Long-term: ETH loss
        # Proceeds: 15000 - 15 = 14985
        # Cost: 20000 + 20 = 20020
        # Loss: -5035
        assert summary["long_term"]["count"] == 1
        assert summary["long_term"]["losses"] == Decimal("5035")
        assert summary["long_term"]["gains"] == Decimal("0")

    def test_generate_form_8949(self, sample_data):
        """Test generating Form 8949 CSV."""
        report_gen = ReportGenerator(sample_data)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            output_path = f.name

        report_gen.generate_form_8949(2024, output_path)

        # Read and verify the CSV
        with open(output_path) as f:
            reader = csv.reader(f)
            rows = list(reader)

        # Check header
        assert rows[0][0] == "Description of Property"
        assert rows[0][1] == "Date Acquired"
        assert rows[0][2] == "Date Sold"

        # Find data rows (skip section headers and empty rows)
        data_rows = [
            r
            for r in rows
            if r and r[0] and not r[0].startswith("---") and r[0] != "Description of Property"
        ]

        # Should have BTC and ETH transactions plus subtotals
        btc_row = next(r for r in data_rows if "BTC" in r[0])
        assert "BTC" in btc_row[0]
        assert "01/01/2024" == btc_row[1]  # Acquired
        assert "06/01/2024" == btc_row[2]  # Sold

        Path(output_path).unlink()

    def test_format_summary(self, sample_data):
        """Test formatting summary as string."""
        report_gen = ReportGenerator(sample_data)
        summary = report_gen.generate_summary(2024)
        formatted = report_gen.format_summary(summary)

        assert "Tax Year 2024" in formatted
        assert "SHORT-TERM" in formatted
        assert "LONG-TERM" in formatted
        assert "BY ASSET" in formatted
        assert "BTC" in formatted
        assert "ETH" in formatted

    def test_empty_year(self, temp_db):
        """Test report for year with no transactions."""
        report_gen = ReportGenerator(temp_db)
        summary = report_gen.generate_summary(2024)

        assert summary["total_transactions"] == 0
        assert summary["short_term"]["count"] == 0
        assert summary["long_term"]["count"] == 0

    def test_by_asset_breakdown(self, sample_data):
        """Test asset breakdown in summary."""
        report_gen = ReportGenerator(sample_data)
        summary = report_gen.generate_summary(2024)

        assert "BTC" in summary["by_asset"]
        assert "ETH" in summary["by_asset"]

        btc_data = summary["by_asset"]["BTC"]
        assert btc_data["count"] == 1
        assert btc_data["net"] == Decimal("19900")

        eth_data = summary["by_asset"]["ETH"]
        assert eth_data["count"] == 1
        assert eth_data["net"] == Decimal("-5035")


class TestForm8949Format:
    """Test Form 8949 format specifics."""

    def test_short_long_term_separation(self, sample_data):
        """Test that short and long term are separated."""
        report_gen = ReportGenerator(sample_data)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            output_path = f.name

        report_gen.generate_form_8949(2024, output_path)

        with open(output_path) as f:
            content = f.read()

        assert "SHORT-TERM (Part I)" in content
        assert "LONG-TERM (Part II)" in content
        assert "SHORT-TERM SUBTOTAL" in content
        assert "LONG-TERM SUBTOTAL" in content
        assert "TOTAL" in content

        Path(output_path).unlink()

    def test_date_format(self, sample_data):
        """Test date format is MM/DD/YYYY."""
        report_gen = ReportGenerator(sample_data)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            output_path = f.name

        report_gen.generate_form_8949(2024, output_path)

        with open(output_path) as f:
            reader = csv.reader(f)
            rows = list(reader)

        # Find a data row with dates
        data_rows = [
            r for r in rows
            if len(r) >= 3 and r[1] and "/" in r[1]
        ]

        assert len(data_rows) > 0
        # Check format MM/DD/YYYY
        date_str = data_rows[0][1]
        parts = date_str.split("/")
        assert len(parts) == 3
        assert len(parts[2]) == 4  # Year is 4 digits

        Path(output_path).unlink()

    def test_decimal_precision(self, sample_data):
        """Test that amounts have proper decimal precision."""
        report_gen = ReportGenerator(sample_data)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            output_path = f.name

        report_gen.generate_form_8949(2024, output_path)

        with open(output_path) as f:
            reader = csv.reader(f)
            rows = list(reader)

        # Find rows with dollar amounts
        for row in rows:
            for i, cell in enumerate(row):
                if cell and "." in cell and i >= 3:  # Dollar columns
                    # Should have 2 decimal places
                    if cell.replace("-", "").replace(".", "").isdigit():
                        parts = cell.split(".")
                        if len(parts) == 2:
                            assert len(parts[1]) == 2

        Path(output_path).unlink()
