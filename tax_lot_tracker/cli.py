"""Command-line interface for tax lot tracker."""

import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import click

from .database import Database
from .engine import CostBasisEngine
from .importers import BinanceImporter, CoinbaseImporter, CSVImporter, KrakenImporter
from .prices import PriceFetcher
from .reports import ReportGenerator


def get_db_path() -> Path:
    """Get the database file path."""
    return Path.cwd() / "tax_lots.db"


def get_db() -> Database:
    """Get database connection."""
    return Database(get_db_path())


@click.group()
@click.version_option(version="0.1.0")
def cli():
    """Tax Lot Tracker - Crypto tax lot tracking with FIFO/LIFO/HIFO cost basis."""
    pass


# ============================================================================
# Import commands
# ============================================================================


@cli.group(name="import")
def import_cmd():
    """Import transactions from various sources."""
    pass


@import_cmd.command(name="csv")
@click.argument("file", type=click.Path(exists=True))
@click.option("--fetch-prices", is_flag=True, help="Fetch missing prices from CoinGecko")
def import_csv(file: str, fetch_prices: bool):
    """Import transactions from a CSV file."""
    db = get_db()

    try:
        importer = CSVImporter(file)
        transactions = importer.fetch_transactions()

        if fetch_prices:
            price_fetcher = PriceFetcher(db)
            for tx in transactions:
                if tx.price_usd == 0:
                    try:
                        tx.price_usd = price_fetcher.get_price(tx.asset, tx.timestamp)
                    except Exception as e:
                        click.echo(f"Warning: Could not fetch price for {tx.asset}: {e}")

        imported = 0
        skipped = 0

        for tx in transactions:
            if db.transaction_exists(tx.id):
                skipped += 1
            else:
                db.add_transaction(tx)
                imported += 1

        click.echo(f"Imported {imported} transactions from {file}")
        if skipped:
            click.echo(f"Skipped {skipped} duplicate transactions")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    finally:
        db.close()


@import_cmd.command(name="coinbase")
@click.option("--since", help="Import transactions since date (YYYY-MM-DD)")
def import_coinbase(since: str | None):
    """Import transactions from Coinbase API."""
    db = get_db()

    try:
        api_key = db.get_config("coinbase_api_key")
        api_secret = db.get_config("coinbase_api_secret")

        if not api_key or not api_secret:
            click.echo(
                "Error: Coinbase API credentials not configured. "
                "Use 'tax-lot-tracker config set coinbase_api_key <key>' and "
                "'tax-lot-tracker config set coinbase_api_secret <secret>'",
                err=True,
            )
            sys.exit(1)

        importer = CoinbaseImporter(api_key, api_secret)

        if not importer.validate_credentials():
            click.echo("Error: Invalid Coinbase API credentials", err=True)
            sys.exit(1)

        since_dt = datetime.fromisoformat(since) if since else None
        transactions = importer.fetch_transactions(since_dt)

        imported = 0
        skipped = 0

        for tx in transactions:
            if db.transaction_exists(tx.id):
                skipped += 1
            else:
                db.add_transaction(tx)
                imported += 1

        click.echo(f"Imported {imported} transactions from Coinbase")
        if skipped:
            click.echo(f"Skipped {skipped} duplicate transactions")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    finally:
        db.close()


@import_cmd.command(name="binance")
@click.option("--since", help="Import transactions since date (YYYY-MM-DD)")
@click.option("--us", is_flag=True, help="Use Binance.US API")
def import_binance(since: str | None, us: bool):
    """Import transactions from Binance API."""
    db = get_db()

    try:
        api_key = db.get_config("binance_api_key")
        api_secret = db.get_config("binance_api_secret")

        if not api_key or not api_secret:
            click.echo(
                "Error: Binance API credentials not configured. "
                "Use 'tax-lot-tracker config set binance_api_key <key>' and "
                "'tax-lot-tracker config set binance_api_secret <secret>'",
                err=True,
            )
            sys.exit(1)

        importer = BinanceImporter(api_key, api_secret, use_us=us)

        if not importer.validate_credentials():
            click.echo("Error: Invalid Binance API credentials", err=True)
            sys.exit(1)

        since_dt = datetime.fromisoformat(since) if since else None
        transactions = importer.fetch_transactions(since_dt)

        # Fetch prices for transactions without them
        price_fetcher = PriceFetcher(db)
        for tx in transactions:
            if tx.price_usd == 0:
                try:
                    tx.price_usd = price_fetcher.get_price(tx.asset, tx.timestamp)
                except Exception:
                    pass

        imported = 0
        skipped = 0

        for tx in transactions:
            if db.transaction_exists(tx.id):
                skipped += 1
            else:
                db.add_transaction(tx)
                imported += 1

        click.echo(f"Imported {imported} transactions from Binance")
        if skipped:
            click.echo(f"Skipped {skipped} duplicate transactions")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    finally:
        db.close()


@import_cmd.command(name="kraken")
@click.option("--since", help="Import transactions since date (YYYY-MM-DD)")
def import_kraken(since: str | None):
    """Import transactions from Kraken API."""
    db = get_db()

    try:
        api_key = db.get_config("kraken_api_key")
        api_secret = db.get_config("kraken_api_secret")

        if not api_key or not api_secret:
            click.echo(
                "Error: Kraken API credentials not configured. "
                "Use 'tax-lot-tracker config set kraken_api_key <key>' and "
                "'tax-lot-tracker config set kraken_api_secret <secret>'",
                err=True,
            )
            sys.exit(1)

        importer = KrakenImporter(api_key, api_secret)

        if not importer.validate_credentials():
            click.echo("Error: Invalid Kraken API credentials", err=True)
            sys.exit(1)

        since_dt = datetime.fromisoformat(since) if since else None
        transactions = importer.fetch_transactions(since_dt)

        # Fetch prices for transactions without them
        price_fetcher = PriceFetcher(db)
        for tx in transactions:
            if tx.price_usd == 0:
                try:
                    tx.price_usd = price_fetcher.get_price(tx.asset, tx.timestamp)
                except Exception:
                    pass

        imported = 0
        skipped = 0

        for tx in transactions:
            if db.transaction_exists(tx.id):
                skipped += 1
            else:
                db.add_transaction(tx)
                imported += 1

        click.echo(f"Imported {imported} transactions from Kraken")
        if skipped:
            click.echo(f"Skipped {skipped} duplicate transactions")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    finally:
        db.close()


# ============================================================================
# Calculate command
# ============================================================================


@cli.command()
@click.option(
    "--method",
    type=click.Choice(["fifo", "lifo", "hifo"]),
    default="fifo",
    help="Cost basis calculation method",
)
@click.option("--recalculate", is_flag=True, help="Recalculate all from scratch")
def calculate(method: str, recalculate: bool):
    """Calculate cost basis for all transactions."""
    db = get_db()

    try:
        engine = CostBasisEngine(db, method)

        if recalculate:
            click.echo(f"Recalculating all transactions using {method.upper()}...")
            disposals = engine.recalculate_all()
            click.echo(f"Created {len(disposals)} disposal records")
        else:
            # Process only new transactions
            transactions = db.get_transactions()
            existing_disposals = set(d.source_tx_id for d in db.get_disposals())

            new_count = 0
            for tx in transactions:
                if tx.id not in existing_disposals:
                    disposals = engine.process_transaction(tx)
                    new_count += len(disposals)

            click.echo(f"Processed transactions using {method.upper()}")
            click.echo(f"Created {new_count} new disposal records")

    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    finally:
        db.close()


# ============================================================================
# Portfolio command
# ============================================================================


@cli.command()
@click.option("--prices", is_flag=True, help="Fetch current prices")
def portfolio(prices: bool):
    """Show current portfolio holdings."""
    db = get_db()

    try:
        holdings = db.get_portfolio()

        if not holdings:
            click.echo("No holdings found. Import transactions first.")
            return

        current_prices = {}
        if prices:
            price_fetcher = PriceFetcher(db)
            assets = list(holdings.keys())
            current_prices = price_fetcher.get_current_prices(assets)

        click.echo("\nCurrent Portfolio")
        click.echo("=" * 60)

        total_cost_basis = Decimal("0")
        total_value = Decimal("0")

        for asset, data in sorted(holdings.items()):
            amount = data["amount"]
            cost_basis = data["cost_basis"]
            total_cost_basis += cost_basis

            line = f"{asset:8} {amount:>18.8f}  Cost Basis: ${cost_basis:>12,.2f}"

            if prices and asset in current_prices:
                price = current_prices[asset]
                value = amount * price
                total_value += value
                pnl = value - cost_basis
                pnl_pct = (pnl / cost_basis * 100) if cost_basis else 0
                sign = "+" if pnl >= 0 else ""
                line += f"  Value: ${value:>12,.2f}  ({sign}{pnl_pct:.1f}%)"

            click.echo(line)

        click.echo("-" * 60)
        summary = f"{'TOTAL':8} {'':18}  Cost Basis: ${total_cost_basis:>12,.2f}"
        if prices and total_value:
            pnl = total_value - total_cost_basis
            pnl_pct = (pnl / total_cost_basis * 100) if total_cost_basis else 0
            sign = "+" if pnl >= 0 else ""
            summary += f"  Value: ${total_value:>12,.2f}  ({sign}{pnl_pct:.1f}%)"
        click.echo(summary)

    finally:
        db.close()


# ============================================================================
# Gains command
# ============================================================================


@cli.command()
@click.argument("year", type=int)
def gains(year: int):
    """Show gains/losses for a tax year."""
    db = get_db()

    try:
        report_gen = ReportGenerator(db)
        summary = report_gen.generate_summary(year)

        click.echo(report_gen.format_summary(summary))

    finally:
        db.close()


# ============================================================================
# Report command
# ============================================================================


@cli.command()
@click.argument("year", type=int)
@click.option("-o", "--output", help="Output file path")
def report(year: int, output: str | None):
    """Generate Form 8949 CSV report for a tax year."""
    db = get_db()

    try:
        report_gen = ReportGenerator(db)
        output_path = report_gen.generate_form_8949(year, output)

        click.echo(f"Form 8949 report generated: {output_path}")

        # Also show summary
        summary = report_gen.generate_summary(year)
        click.echo(f"\nTotal short-term: ${summary['short_term']['net']:,.2f}")
        click.echo(f"Total long-term:  ${summary['long_term']['net']:,.2f}")
        click.echo(f"Total net:        ${summary['short_term']['net'] + summary['long_term']['net']:,.2f}")

    finally:
        db.close()


# ============================================================================
# Config commands
# ============================================================================


@cli.group()
def config():
    """Manage configuration settings."""
    pass


@config.command(name="set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str):
    """Set a configuration value."""
    db = get_db()

    try:
        db.set_config(key, value)
        # Mask secrets in output
        if "secret" in key.lower() or "key" in key.lower():
            display_value = value[:4] + "..." + value[-4:] if len(value) > 8 else "****"
        else:
            display_value = value
        click.echo(f"Set {key} = {display_value}")

    finally:
        db.close()


@config.command(name="show")
def config_show():
    """Show current configuration."""
    db = get_db()

    try:
        all_config = db.get_all_config()

        if not all_config:
            click.echo("No configuration set.")
            return

        click.echo("\nConfiguration")
        click.echo("=" * 40)

        for key, value in sorted(all_config.items()):
            # Mask secrets
            if "secret" in key.lower() or "key" in key.lower():
                display_value = value[:4] + "..." + value[-4:] if len(value) > 8 else "****"
            else:
                display_value = value
            click.echo(f"{key}: {display_value}")

    finally:
        db.close()


@config.command(name="get")
@click.argument("key")
def config_get(key: str):
    """Get a configuration value."""
    db = get_db()

    try:
        value = db.get_config(key)
        if value is None:
            click.echo(f"Key '{key}' not found")
            sys.exit(1)

        # Mask secrets
        if "secret" in key.lower() or "key" in key.lower():
            display_value = value[:4] + "..." + value[-4:] if len(value) > 8 else "****"
        else:
            display_value = value
        click.echo(display_value)

    finally:
        db.close()


# ============================================================================
# Transactions command (for debugging)
# ============================================================================


@cli.command()
@click.option("--asset", help="Filter by asset")
@click.option("--type", "tx_type", help="Filter by type (buy, sell, income, transfer)")
@click.option("--limit", default=20, help="Number of transactions to show")
def transactions(asset: str | None, tx_type: str | None, limit: int):
    """List imported transactions."""
    db = get_db()

    try:
        txs = db.get_transactions(asset=asset, tx_type=tx_type)

        if not txs:
            click.echo("No transactions found.")
            return

        click.echo(f"\nTransactions ({len(txs)} total, showing last {min(limit, len(txs))})")
        click.echo("=" * 80)

        for tx in txs[-limit:]:
            date_str = tx.timestamp.strftime("%Y-%m-%d %H:%M")
            click.echo(
                f"{date_str}  {tx.type:8}  {tx.amount:>14.8f} {tx.asset:5}  "
                f"@ ${tx.price_usd:>10,.2f}  Fee: ${tx.fee_usd:>8,.2f}  [{tx.source}]"
            )

    finally:
        db.close()


if __name__ == "__main__":
    cli()
