# Tax Lot Tracker

A Python CLI tool for cryptocurrency tax lot tracking with FIFO/LIFO/HIFO cost basis methods, exchange API imports, and US tax report generation.

## Features

- **Cost Basis Methods**: FIFO (First In, First Out), LIFO (Last In, First Out), HIFO (Highest In, First Out)
- **Exchange Imports**: Coinbase, Binance, Kraken APIs
- **CSV Import**: Generic CSV import with flexible column mapping
- **Tax Reports**: Form 8949 CSV export for US taxes
- **Price Lookup**: Automatic historical price fetching from CoinGecko
- **Portfolio View**: Current holdings with cost basis and P&L

## Installation

```bash
pip install -e .
```

## Quick Start

### 1. Import Transactions

From CSV:
```bash
tax-lot-tracker import csv transactions.csv
```

From exchanges (after setting API keys):
```bash
tax-lot-tracker config set coinbase_api_key YOUR_KEY
tax-lot-tracker config set coinbase_api_secret YOUR_SECRET
tax-lot-tracker import coinbase
```

### 2. Calculate Cost Basis

```bash
tax-lot-tracker calculate --method fifo
```

### 3. View Portfolio

```bash
tax-lot-tracker portfolio --prices
```

### 4. Generate Tax Report

```bash
tax-lot-tracker report 2024
tax-lot-tracker gains 2024
```

## Commands

### Import Commands

```bash
# Import from CSV
tax-lot-tracker import csv <file> [--fetch-prices]

# Import from exchanges
tax-lot-tracker import coinbase [--since YYYY-MM-DD]
tax-lot-tracker import binance [--since YYYY-MM-DD] [--us]
tax-lot-tracker import kraken [--since YYYY-MM-DD]
```

### Analysis Commands

```bash
# Calculate cost basis
tax-lot-tracker calculate [--method fifo|lifo|hifo] [--recalculate]

# View portfolio
tax-lot-tracker portfolio [--prices]

# View gains/losses
tax-lot-tracker gains <year>

# List transactions
tax-lot-tracker transactions [--asset BTC] [--type buy] [--limit 20]
```

### Report Commands

```bash
# Generate Form 8949 CSV
tax-lot-tracker report <year> [-o output.csv]
```

### Configuration

```bash
# Set API keys
tax-lot-tracker config set <key> <value>

# Show configuration
tax-lot-tracker config show

# Get a specific value
tax-lot-tracker config get <key>
```

## CSV Format

The CSV importer accepts files with these columns (names are flexible):

| Required | Column Names |
|----------|--------------|
| Yes | timestamp, date, time, datetime |
| Yes | type, transaction type, side, action |
| Yes | asset, symbol, currency, coin |
| Yes | amount, quantity, qty, size |
| One of | price, price_usd OR total, total_usd |
| No | fee, fee_usd, commission |

Example:
```csv
timestamp,type,asset,amount,price,fee
2024-01-15 10:30:00,buy,BTC,0.5,42000,10.50
2024-01-20 14:00:00,sell,BTC,0.25,45000,5.25
```

Transaction types recognized:
- Buy: `buy`, `purchase`, `bought`
- Sell: `sell`, `sold`, `sale`
- Income: `income`, `reward`, `staking`, `mining`, `interest`
- Transfer: `transfer`, `send`, `receive`, `deposit`, `withdrawal`

## Cost Basis Methods

### FIFO (First In, First Out)
Sells the oldest lots first. Often results in larger gains in bull markets.

### LIFO (Last In, First Out)
Sells the newest lots first. Can minimize gains if recent purchases were at higher prices.

### HIFO (Highest In, First Out)
Sells lots with the highest cost basis first. Minimizes taxable gains.

## US Tax Rules

This tool handles:
- **Short vs Long-term**: Disposals held < 1 year = short-term (higher tax rate)
- **Crypto-to-crypto trades**: Treated as taxable events
- **Fees**: Added to cost basis (buys) or reduce proceeds (sells)
- **Income**: Mining/staking rewards = income at FMV, becomes cost basis

**Not currently handled:**
- Wash sale rule (IRS guidance unclear for crypto)
- Specific identification (manually picking lots)

## Database

All data is stored in `tax_lots.db` (SQLite) in the current directory. Tables:
- `transactions`: Raw imported transactions
- `lots`: Tax lots (created from buys/income)
- `disposals`: Matched disposals (created from sells)
- `config`: API keys and settings
- `price_cache`: Cached historical prices

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
pytest --cov=tax_lot_tracker
```

## License

MIT
