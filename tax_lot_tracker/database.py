"""SQLite database operations for tax lot tracking."""

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from .models import Disposal, Lot, Transaction, ValidationError

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    """Base exception for database errors."""
    pass


class IntegrityError(DatabaseError):
    """Raised when data integrity is violated."""
    pass


def adapt_decimal(d: Decimal) -> str:
    """Convert Decimal to string for SQLite storage."""
    if d is None:
        return "0"
    return str(d)


def convert_decimal(s: bytes) -> Decimal:
    """Convert SQLite string back to Decimal."""
    try:
        return Decimal(s.decode("utf-8"))
    except Exception:
        return Decimal("0")


sqlite3.register_adapter(Decimal, adapt_decimal)
sqlite3.register_converter("DECIMAL", convert_decimal)


class Database:
    """SQLite database for storing transactions, lots, and disposals.

    Thread-safe database operations with transaction support.
    """

    SCHEMA_VERSION = 1

    def __init__(self, db_path: str | Path = "tax_lots.db"):
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._lock = threading.RLock()

        # Validate path
        if self.db_path.is_dir():
            raise DatabaseError(f"Database path is a directory: {self.db_path}")

        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize connection and tables
        self._get_connection()
        self._create_tables()
        self._migrate_if_needed()

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                detect_types=sqlite3.PARSE_DECLTYPES,
                timeout=30.0,  # Wait up to 30s for locks
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
            # Enable foreign keys
            self._local.conn.execute("PRAGMA foreign_keys = ON")
            # Use WAL mode for better concurrency
            self._local.conn.execute("PRAGMA journal_mode = WAL")
        return self._local.conn

    @property
    def conn(self) -> sqlite3.Connection:
        """Database connection property."""
        return self._get_connection()

    @contextmanager
    def transaction(self):
        """Context manager for database transactions.

        Usage:
            with db.transaction():
                db.add_transaction(tx1)
                db.add_transaction(tx2)
                # Commits on success, rolls back on exception
        """
        conn = self._get_connection()
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Transaction rolled back due to error: {e}")
            raise

    def _create_tables(self):
        """Create database tables if they don't exist."""
        with self._lock:
            cursor = self.conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    type TEXT NOT NULL CHECK(type IN ('buy', 'sell', 'transfer', 'income')),
                    asset TEXT NOT NULL,
                    amount DECIMAL NOT NULL CHECK(amount > 0),
                    price_usd DECIMAL NOT NULL CHECK(price_usd >= 0),
                    fee_usd DECIMAL NOT NULL CHECK(fee_usd >= 0),
                    source TEXT NOT NULL,
                    raw_data TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS lots (
                    id TEXT PRIMARY KEY,
                    asset TEXT NOT NULL,
                    amount DECIMAL NOT NULL CHECK(amount >= 0),
                    original_amount DECIMAL NOT NULL CHECK(original_amount > 0),
                    cost_basis_usd DECIMAL NOT NULL CHECK(cost_basis_usd >= 0),
                    acquired_at TEXT NOT NULL,
                    source_tx_id TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (source_tx_id) REFERENCES transactions(id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS disposals (
                    id TEXT PRIMARY KEY,
                    lot_id TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    amount DECIMAL NOT NULL CHECK(amount > 0),
                    proceeds_usd DECIMAL NOT NULL CHECK(proceeds_usd >= 0),
                    cost_basis_usd DECIMAL NOT NULL CHECK(cost_basis_usd >= 0),
                    acquired_at TEXT NOT NULL,
                    disposed_at TEXT NOT NULL,
                    source_tx_id TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (lot_id) REFERENCES lots(id),
                    FOREIGN KEY (source_tx_id) REFERENCES transactions(id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS price_cache (
                    asset TEXT NOT NULL,
                    date TEXT NOT NULL,
                    price_usd DECIMAL NOT NULL CHECK(price_usd >= 0),
                    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (asset, date)
                )
            """)

            # Create indexes for common queries
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_transactions_timestamp
                ON transactions(timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_transactions_asset
                ON transactions(asset)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_lots_asset
                ON lots(asset)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_lots_acquired_at
                ON lots(acquired_at)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_disposals_disposed_at
                ON disposals(disposed_at)
            """)

            self.conn.commit()

    def _migrate_if_needed(self):
        """Run database migrations if needed."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
        row = cursor.fetchone()
        current_version = row["version"] if row else 0

        if current_version < self.SCHEMA_VERSION:
            self._run_migrations(current_version)

    def _run_migrations(self, from_version: int):
        """Run migrations from a specific version."""
        with self._lock:
            cursor = self.conn.cursor()

            # Migration 1: Add original_amount to lots if missing
            if from_version < 1:
                try:
                    cursor.execute("SELECT original_amount FROM lots LIMIT 1")
                except sqlite3.OperationalError:
                    # Column doesn't exist, add it
                    cursor.execute("""
                        ALTER TABLE lots ADD COLUMN original_amount DECIMAL
                    """)
                    # Backfill from transactions
                    cursor.execute("""
                        UPDATE lots SET original_amount = (
                            SELECT t.amount FROM transactions t
                            WHERE t.id = lots.source_tx_id
                        )
                    """)
                    # Set default for any orphaned lots
                    cursor.execute("""
                        UPDATE lots SET original_amount = amount
                        WHERE original_amount IS NULL
                    """)

            # Update schema version
            cursor.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (self.SCHEMA_VERSION,)
            )
            self.conn.commit()

    def close(self):
        """Close database connection."""
        if hasattr(self._local, 'conn') and self._local.conn:
            try:
                self._local.conn.close()
            except Exception as e:
                logger.warning(f"Error closing database connection: {e}")
            finally:
                self._local.conn = None

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        return False

    # Transaction operations
    def add_transaction(self, tx: Transaction) -> None:
        """Add a transaction to the database."""
        if not isinstance(tx, Transaction):
            raise TypeError(f"Expected Transaction, got {type(tx).__name__}")

        with self._lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO transactions
                    (id, timestamp, type, asset, amount, price_usd, fee_usd, source, raw_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tx.id,
                        tx.timestamp.isoformat(),
                        tx.type,
                        tx.asset.upper(),
                        str(tx.amount),
                        str(tx.price_usd),
                        str(tx.fee_usd),
                        tx.source,
                        json.dumps(tx.raw_data, default=str),
                    ),
                )
                self.conn.commit()
            except sqlite3.IntegrityError as e:
                raise IntegrityError(f"Failed to add transaction: {e}") from e

    def get_transaction(self, tx_id: str) -> Transaction | None:
        """Get a transaction by ID."""
        if not tx_id:
            return None

        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,))
        row = cursor.fetchone()
        if row:
            return self._row_to_transaction(row)
        return None

    def get_transactions(
        self, asset: str | None = None, tx_type: str | None = None
    ) -> list[Transaction]:
        """Get transactions, optionally filtered by asset or type."""
        cursor = self.conn.cursor()
        query = "SELECT * FROM transactions WHERE 1=1"
        params: list = []

        if asset:
            query += " AND asset = ?"
            params.append(asset.upper())
        if tx_type:
            if tx_type not in ("buy", "sell", "transfer", "income"):
                raise ValueError(f"Invalid transaction type: {tx_type}")
            query += " AND type = ?"
            params.append(tx_type)

        query += " ORDER BY timestamp ASC, id ASC"
        cursor.execute(query, params)
        return [self._row_to_transaction(row) for row in cursor.fetchall()]

    def _row_to_transaction(self, row: sqlite3.Row) -> Transaction:
        """Convert database row to Transaction object."""
        try:
            raw_data = json.loads(row["raw_data"]) if row["raw_data"] else {}
        except (json.JSONDecodeError, TypeError):
            raw_data = {}

        return Transaction(
            id=row["id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            type=row["type"],
            asset=row["asset"],
            amount=Decimal(str(row["amount"])),
            price_usd=Decimal(str(row["price_usd"])),
            fee_usd=Decimal(str(row["fee_usd"])),
            source=row["source"],
            raw_data=raw_data,
        )

    # Lot operations
    def add_lot(self, lot: Lot) -> None:
        """Add a lot to the database."""
        if not isinstance(lot, Lot):
            raise TypeError(f"Expected Lot, got {type(lot).__name__}")

        with self._lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO lots
                    (id, asset, amount, original_amount, cost_basis_usd, acquired_at, source_tx_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lot.id,
                        lot.asset.upper(),
                        str(lot.amount),
                        str(lot.amount),  # original_amount = initial amount
                        str(lot.cost_basis_usd),
                        lot.acquired_at.isoformat(),
                        lot.source_tx_id,
                    ),
                )
                self.conn.commit()
            except sqlite3.IntegrityError as e:
                raise IntegrityError(f"Failed to add lot: {e}") from e

    def update_lot(self, lot_id: str, amount: Decimal, cost_basis_usd: Decimal) -> None:
        """Update a lot's amount and cost basis."""
        if not lot_id:
            raise ValueError("lot_id is required")
        if amount < 0:
            raise ValueError("amount cannot be negative")
        if cost_basis_usd < 0:
            raise ValueError("cost_basis_usd cannot be negative")

        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE lots SET amount = ?, cost_basis_usd = ? WHERE id = ?",
                (str(amount), str(cost_basis_usd), lot_id),
            )
            if cursor.rowcount == 0:
                raise IntegrityError(f"Lot not found: {lot_id}")
            self.conn.commit()

    def update_lot_amount(self, lot_id: str, new_amount: Decimal) -> None:
        """Update the remaining amount in a lot."""
        if not lot_id:
            raise ValueError("lot_id is required")
        if new_amount < 0:
            raise ValueError("new_amount cannot be negative")

        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE lots SET amount = ? WHERE id = ?",
                (str(new_amount), lot_id),
            )
            if cursor.rowcount == 0:
                raise IntegrityError(f"Lot not found: {lot_id}")
            self.conn.commit()

    def get_lots(
        self,
        asset: str,
        method: Literal["fifo", "lifo", "hifo"] = "fifo",
        include_depleted: bool = False,
    ) -> list[Lot]:
        """Get lots for an asset, sorted by cost basis method."""
        if not asset:
            raise ValueError("asset is required")
        if method not in ("fifo", "lifo", "hifo"):
            raise ValueError(f"Invalid method: {method}")

        cursor = self.conn.cursor()

        base_query = "SELECT * FROM lots WHERE asset = ?"
        params: list = [asset.upper()]

        if not include_depleted:
            # Use small epsilon for floating point comparison
            base_query += " AND CAST(amount AS REAL) > 0.00000001"

        if method == "fifo":
            base_query += " ORDER BY acquired_at ASC, id ASC"
        elif method == "lifo":
            base_query += " ORDER BY acquired_at DESC, id DESC"
        elif method == "hifo":
            # Highest cost per unit first, handle division by zero
            base_query += """ ORDER BY
                CASE WHEN CAST(amount AS REAL) > 0
                    THEN CAST(cost_basis_usd AS REAL) / CAST(amount AS REAL)
                    ELSE 0
                END DESC, id DESC"""

        cursor.execute(base_query, params)
        return [self._row_to_lot(row) for row in cursor.fetchall()]

    def get_all_lots(self, include_depleted: bool = False) -> list[Lot]:
        """Get all lots."""
        cursor = self.conn.cursor()
        query = "SELECT * FROM lots"
        if not include_depleted:
            query += " WHERE CAST(amount AS REAL) > 0.00000001"
        query += " ORDER BY asset, acquired_at, id"
        cursor.execute(query)
        return [self._row_to_lot(row) for row in cursor.fetchall()]

    def _row_to_lot(self, row: sqlite3.Row) -> Lot:
        """Convert database row to Lot object."""
        return Lot(
            id=row["id"],
            asset=row["asset"],
            amount=Decimal(str(row["amount"])),
            cost_basis_usd=Decimal(str(row["cost_basis_usd"])),
            acquired_at=datetime.fromisoformat(row["acquired_at"]),
            source_tx_id=row["source_tx_id"],
        )

    # Disposal operations
    def add_disposal(self, disposal: Disposal) -> None:
        """Add a disposal to the database."""
        if not isinstance(disposal, Disposal):
            raise TypeError(f"Expected Disposal, got {type(disposal).__name__}")

        with self._lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO disposals
                    (id, lot_id, asset, amount, proceeds_usd, cost_basis_usd,
                     acquired_at, disposed_at, source_tx_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        disposal.id,
                        disposal.lot_id,
                        disposal.asset.upper(),
                        str(disposal.amount),
                        str(disposal.proceeds_usd),
                        str(disposal.cost_basis_usd),
                        disposal.acquired_at.isoformat(),
                        disposal.disposed_at.isoformat(),
                        disposal.source_tx_id,
                    ),
                )
                self.conn.commit()
            except sqlite3.IntegrityError as e:
                raise IntegrityError(f"Failed to add disposal: {e}") from e

    def get_disposals(self, year: int | None = None) -> list[Disposal]:
        """Get disposals, optionally filtered by year."""
        cursor = self.conn.cursor()
        query = "SELECT * FROM disposals"

        if year is not None:
            if not (1900 <= year <= 2100):
                raise ValueError(f"Invalid year: {year}")
            start = f"{year}-01-01T00:00:00"
            end = f"{year}-12-31T23:59:59"
            query += " WHERE disposed_at >= ? AND disposed_at <= ?"
            cursor.execute(query + " ORDER BY disposed_at, id", (start, end))
        else:
            cursor.execute(query + " ORDER BY disposed_at, id")

        return [self._row_to_disposal(row) for row in cursor.fetchall()]

    def clear_disposals(self) -> None:
        """Clear all disposals (for recalculation)."""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM disposals")
            self.conn.commit()
            logger.info("Cleared all disposals")

    def _row_to_disposal(self, row: sqlite3.Row) -> Disposal:
        """Convert database row to Disposal object."""
        return Disposal(
            id=row["id"],
            lot_id=row["lot_id"],
            asset=row["asset"],
            amount=Decimal(str(row["amount"])),
            proceeds_usd=Decimal(str(row["proceeds_usd"])),
            cost_basis_usd=Decimal(str(row["cost_basis_usd"])),
            acquired_at=datetime.fromisoformat(row["acquired_at"]),
            disposed_at=datetime.fromisoformat(row["disposed_at"]),
            source_tx_id=row["source_tx_id"],
        )

    # Portfolio operations
    def get_portfolio(self) -> dict[str, dict]:
        """Get current holdings summary."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT asset,
                   SUM(CAST(amount AS REAL)) as total_amount,
                   SUM(CAST(cost_basis_usd AS REAL)) as total_cost_basis
            FROM lots
            WHERE CAST(amount AS REAL) > 0.00000001
            GROUP BY asset
            ORDER BY asset
        """)

        portfolio = {}
        for row in cursor.fetchall():
            if row["total_amount"] and row["total_amount"] > 0:
                portfolio[row["asset"]] = {
                    "amount": Decimal(str(row["total_amount"])),
                    "cost_basis": Decimal(str(row["total_cost_basis"] or 0)),
                }
        return portfolio

    # Config operations
    def set_config(self, key: str, value: str) -> None:
        """Set a config value."""
        if not key or not isinstance(key, str):
            raise ValueError("key must be a non-empty string")
        if not isinstance(value, str):
            raise ValueError("value must be a string")

        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, datetime.now().isoformat()),
            )
            self.conn.commit()

    def get_config(self, key: str, default: str | None = None) -> str | None:
        """Get a config value."""
        if not key:
            return default

        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            return row["value"]
        return default

    def get_all_config(self) -> dict[str, str]:
        """Get all config values."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT key, value FROM config ORDER BY key")
        return {row["key"]: row["value"] for row in cursor.fetchall()}

    def delete_config(self, key: str) -> bool:
        """Delete a config value. Returns True if deleted."""
        if not key:
            return False

        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM config WHERE key = ?", (key,))
            self.conn.commit()
            return cursor.rowcount > 0

    # Price cache operations
    def cache_price(self, asset: str, date: str, price_usd: Decimal) -> None:
        """Cache a historical price."""
        if not asset or not date:
            raise ValueError("asset and date are required")
        if price_usd < 0:
            raise ValueError("price_usd cannot be negative")

        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """INSERT OR REPLACE INTO price_cache
                   (asset, date, price_usd, fetched_at)
                   VALUES (?, ?, ?, ?)""",
                (asset.upper(), date, str(price_usd), datetime.now().isoformat()),
            )
            self.conn.commit()

    def get_cached_price(self, asset: str, date: str) -> Decimal | None:
        """Get cached historical price."""
        if not asset or not date:
            return None

        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT price_usd FROM price_cache WHERE asset = ? AND date = ?",
            (asset.upper(), date),
        )
        row = cursor.fetchone()
        if row:
            return Decimal(str(row["price_usd"]))
        return None

    # Reset operations
    def reset_lots(self) -> None:
        """Reset all lots to original amounts (for recalculation)."""
        with self._lock:
            cursor = self.conn.cursor()
            # Restore lot amounts from original_amount column
            cursor.execute("""
                UPDATE lots SET
                    amount = original_amount,
                    cost_basis_usd = (
                        SELECT CASE
                            WHEN t.type = 'buy' THEN (CAST(t.amount AS REAL) * CAST(t.price_usd AS REAL)) + CAST(t.fee_usd AS REAL)
                            ELSE CAST(t.amount AS REAL) * CAST(t.price_usd AS REAL)
                        END
                        FROM transactions t
                        WHERE t.id = lots.source_tx_id
                    )
            """)
            self.conn.commit()
            logger.info("Reset all lots to original amounts")

    def transaction_exists(self, tx_id: str) -> bool:
        """Check if a transaction already exists."""
        if not tx_id:
            return False

        cursor = self.conn.cursor()
        cursor.execute("SELECT 1 FROM transactions WHERE id = ?", (tx_id,))
        return cursor.fetchone() is not None

    def get_statistics(self) -> dict:
        """Get database statistics."""
        cursor = self.conn.cursor()

        stats = {}

        cursor.execute("SELECT COUNT(*) as count FROM transactions")
        stats["transactions"] = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM lots")
        stats["lots"] = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM lots WHERE CAST(amount AS REAL) > 0.00000001")
        stats["active_lots"] = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM disposals")
        stats["disposals"] = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(DISTINCT asset) as count FROM lots")
        stats["unique_assets"] = cursor.fetchone()["count"]

        return stats
