"""Tests for transaction importers."""

import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tax_lot_tracker.importers.csv_importer import CSVImporter, CSVImportError


class TestCSVImporter:
    """Test CSV importer."""

    def test_basic_import(self):
        """Test importing a basic CSV file."""
        csv_content = """timestamp,type,asset,amount,price,fee
2024-01-15 10:30:00,buy,BTC,0.5,42000,10.50
2024-01-20 14:00:00,sell,BTC,0.25,45000,5.25
2024-02-01 09:00:00,buy,ETH,2.0,2200,5.00
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            f.write(csv_content)
            f.flush()

            importer = CSVImporter(f.name)
            transactions = importer.fetch_transactions()

            assert len(transactions) == 3

            # Check first transaction
            tx1 = transactions[0]
            assert tx1.type == "buy"
            assert tx1.asset == "BTC"
            assert tx1.amount == Decimal("0.5")
            assert tx1.price_usd == Decimal("42000")
            assert tx1.fee_usd == Decimal("10.50")
            assert tx1.timestamp == datetime(2024, 1, 15, 10, 30, 0)

            # Check sell transaction
            tx2 = transactions[1]
            assert tx2.type == "sell"
            assert tx2.asset == "BTC"
            assert tx2.amount == Decimal("0.25")

            Path(f.name).unlink()

    def test_alternative_column_names(self):
        """Test CSV with alternative column names."""
        csv_content = """date,transaction type,symbol,quantity,unit price,commission
2024-01-15,purchase,BTC,1.0,40000,20
2024-01-20,sale,BTC,0.5,45000,10
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            f.write(csv_content)
            f.flush()

            importer = CSVImporter(f.name)
            transactions = importer.fetch_transactions()

            assert len(transactions) == 2
            assert transactions[0].type == "buy"
            assert transactions[1].type == "sell"

            Path(f.name).unlink()

    def test_total_column_for_price(self):
        """Test calculating price from total column."""
        csv_content = """timestamp,type,asset,amount,total,fee
2024-01-15,buy,BTC,0.5,20000,10
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            f.write(csv_content)
            f.flush()

            importer = CSVImporter(f.name)
            transactions = importer.fetch_transactions()

            assert len(transactions) == 1
            # Price should be calculated: 20000 / 0.5 = 40000
            assert transactions[0].price_usd == Decimal("40000")

            Path(f.name).unlink()

    def test_income_type(self):
        """Test importing income transactions (staking, etc.)."""
        csv_content = """timestamp,type,asset,amount,price,fee
2024-01-15,staking,ETH,0.01,2000,0
2024-01-20,reward,BTC,0.001,42000,0
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            f.write(csv_content)
            f.flush()

            importer = CSVImporter(f.name)
            transactions = importer.fetch_transactions()

            assert len(transactions) == 2
            assert transactions[0].type == "income"
            assert transactions[1].type == "income"

            Path(f.name).unlink()

    def test_missing_required_columns(self):
        """Test error when required columns are missing."""
        csv_content = """date,symbol,amount
2024-01-15,BTC,0.5
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            f.write(csv_content)
            f.flush()

            with pytest.raises(CSVImportError, match="Missing required columns"):
                CSVImporter(f.name).fetch_transactions()

            Path(f.name).unlink()

    def test_since_filter(self):
        """Test filtering transactions by date."""
        csv_content = """timestamp,type,asset,amount,price,fee
2024-01-01,buy,BTC,1.0,40000,10
2024-02-01,buy,BTC,1.0,42000,10
2024-03-01,buy,BTC,1.0,44000,10
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            f.write(csv_content)
            f.flush()

            importer = CSVImporter(f.name)
            transactions = importer.fetch_transactions(
                since=datetime(2024, 2, 1)
            )

            assert len(transactions) == 2  # Feb and Mar only

            Path(f.name).unlink()

    def test_semicolon_delimiter(self):
        """Test CSV with semicolon delimiter."""
        csv_content = """timestamp;type;asset;amount;price;fee
2024-01-15;buy;BTC;0.5;42000;10
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            f.write(csv_content)
            f.flush()

            importer = CSVImporter(f.name)
            transactions = importer.fetch_transactions()

            assert len(transactions) == 1
            assert transactions[0].asset == "BTC"

            Path(f.name).unlink()

    def test_negative_amounts_converted(self):
        """Test that negative amounts are converted to positive."""
        csv_content = """timestamp,type,asset,amount,price,fee
2024-01-15,sell,BTC,-0.5,42000,10
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            f.write(csv_content)
            f.flush()

            importer = CSVImporter(f.name)
            transactions = importer.fetch_transactions()

            assert transactions[0].amount == Decimal("0.5")  # Positive

            Path(f.name).unlink()

    def test_currency_formatting(self):
        """Test handling of currency-formatted values."""
        csv_content = """timestamp,type,asset,amount,price,fee
2024-01-15,buy,BTC,0.5,"$42,000.00","$10.50"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            f.write(csv_content)
            f.flush()

            importer = CSVImporter(f.name)
            transactions = importer.fetch_transactions()

            assert transactions[0].price_usd == Decimal("42000.00")
            assert transactions[0].fee_usd == Decimal("10.50")

            Path(f.name).unlink()

    def test_deterministic_ids(self):
        """Test that transaction IDs are deterministic."""
        csv_content = """timestamp,type,asset,amount,price,fee
2024-01-15,buy,BTC,0.5,42000,10
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            f.write(csv_content)
            f.flush()

            importer1 = CSVImporter(f.name)
            tx1 = importer1.fetch_transactions()[0]

            importer2 = CSVImporter(f.name)
            tx2 = importer2.fetch_transactions()[0]

            # Same file, same row should produce same ID
            assert tx1.id == tx2.id

            Path(f.name).unlink()

    def test_file_not_found(self):
        """Test error when file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            CSVImporter("/nonexistent/file.csv")

    def test_empty_rows_skipped(self):
        """Test that empty rows are skipped."""
        csv_content = """timestamp,type,asset,amount,price,fee
2024-01-15,buy,BTC,0.5,42000,10
,,,,,
2024-01-20,sell,BTC,0.25,45000,5
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as f:
            f.write(csv_content)
            f.flush()

            importer = CSVImporter(f.name)
            transactions = importer.fetch_transactions()

            assert len(transactions) == 2

            Path(f.name).unlink()
