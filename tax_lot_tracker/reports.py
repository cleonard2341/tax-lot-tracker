"""Tax report generation for Form 8949."""

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .database import Database
from .models import Disposal


class ReportGenerator:
    """Generate tax reports from disposal data."""

    def __init__(self, db: Database):
        self.db = db

    def generate_form_8949(
        self,
        year: int,
        output_path: str | Path | None = None,
    ) -> str:
        """Generate Form 8949 CSV for a given tax year."""
        disposals = self.db.get_disposals(year)

        if output_path is None:
            output_path = Path(f"form_8949_{year}.csv")
        else:
            output_path = Path(output_path)

        # Separate short-term and long-term
        short_term = [d for d in disposals if d.term == "short"]
        long_term = [d for d in disposals if d.term == "long"]

        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)

            # Header
            writer.writerow([
                "Description of Property",
                "Date Acquired",
                "Date Sold",
                "Proceeds",
                "Cost Basis",
                "Adjustment Code",
                "Adjustment Amount",
                "Gain or Loss",
            ])

            # Short-term section
            if short_term:
                writer.writerow([])
                writer.writerow(["--- SHORT-TERM (Part I) ---"])
                writer.writerow([])
                for disposal in short_term:
                    self._write_disposal_row(writer, disposal)

                # Short-term subtotal
                writer.writerow([])
                st_proceeds = sum(d.proceeds_usd for d in short_term)
                st_cost = sum(d.cost_basis_usd for d in short_term)
                st_gain = sum(d.gain_loss_usd for d in short_term)
                writer.writerow([
                    "SHORT-TERM SUBTOTAL",
                    "",
                    "",
                    f"{st_proceeds:.2f}",
                    f"{st_cost:.2f}",
                    "",
                    "",
                    f"{st_gain:.2f}",
                ])

            # Long-term section
            if long_term:
                writer.writerow([])
                writer.writerow(["--- LONG-TERM (Part II) ---"])
                writer.writerow([])
                for disposal in long_term:
                    self._write_disposal_row(writer, disposal)

                # Long-term subtotal
                writer.writerow([])
                lt_proceeds = sum(d.proceeds_usd for d in long_term)
                lt_cost = sum(d.cost_basis_usd for d in long_term)
                lt_gain = sum(d.gain_loss_usd for d in long_term)
                writer.writerow([
                    "LONG-TERM SUBTOTAL",
                    "",
                    "",
                    f"{lt_proceeds:.2f}",
                    f"{lt_cost:.2f}",
                    "",
                    "",
                    f"{lt_gain:.2f}",
                ])

            # Grand total
            writer.writerow([])
            total_proceeds = sum(d.proceeds_usd for d in disposals)
            total_cost = sum(d.cost_basis_usd for d in disposals)
            total_gain = sum(d.gain_loss_usd for d in disposals)
            writer.writerow([
                "TOTAL",
                "",
                "",
                f"{total_proceeds:.2f}",
                f"{total_cost:.2f}",
                "",
                "",
                f"{total_gain:.2f}",
            ])

        return str(output_path)

    def _write_disposal_row(self, writer: csv.writer, disposal: Disposal):
        """Write a single disposal row."""
        description = f"{disposal.amount:.8f} {disposal.asset}".rstrip("0").rstrip(".")
        writer.writerow([
            description,
            disposal.acquired_at.strftime("%m/%d/%Y"),
            disposal.disposed_at.strftime("%m/%d/%Y"),
            f"{disposal.proceeds_usd:.2f}",
            f"{disposal.cost_basis_usd:.2f}",
            "",  # Adjustment code
            "",  # Adjustment amount
            f"{disposal.gain_loss_usd:.2f}",
        ])

    def generate_summary(self, year: int) -> dict:
        """Generate a summary report for a tax year."""
        disposals = self.db.get_disposals(year)

        summary = {
            "year": year,
            "total_transactions": len(disposals),
            "short_term": {
                "count": 0,
                "proceeds": Decimal("0"),
                "cost_basis": Decimal("0"),
                "gains": Decimal("0"),
                "losses": Decimal("0"),
                "net": Decimal("0"),
            },
            "long_term": {
                "count": 0,
                "proceeds": Decimal("0"),
                "cost_basis": Decimal("0"),
                "gains": Decimal("0"),
                "losses": Decimal("0"),
                "net": Decimal("0"),
            },
            "by_asset": {},
        }

        for disposal in disposals:
            term = disposal.term
            gain_loss = disposal.gain_loss_usd

            summary[f"{term}_term"]["count"] += 1
            summary[f"{term}_term"]["proceeds"] += disposal.proceeds_usd
            summary[f"{term}_term"]["cost_basis"] += disposal.cost_basis_usd
            summary[f"{term}_term"]["net"] += gain_loss

            if gain_loss >= 0:
                summary[f"{term}_term"]["gains"] += gain_loss
            else:
                summary[f"{term}_term"]["losses"] += abs(gain_loss)

            # Track by asset
            if disposal.asset not in summary["by_asset"]:
                summary["by_asset"][disposal.asset] = {
                    "count": 0,
                    "proceeds": Decimal("0"),
                    "cost_basis": Decimal("0"),
                    "net": Decimal("0"),
                }
            summary["by_asset"][disposal.asset]["count"] += 1
            summary["by_asset"][disposal.asset]["proceeds"] += disposal.proceeds_usd
            summary["by_asset"][disposal.asset]["cost_basis"] += disposal.cost_basis_usd
            summary["by_asset"][disposal.asset]["net"] += gain_loss

        return summary

    def format_summary(self, summary: dict) -> str:
        """Format summary report as a string."""
        lines = []
        lines.append(f"Tax Year {summary['year']} Summary")
        lines.append("=" * 50)
        lines.append(f"Total Disposals: {summary['total_transactions']}")
        lines.append("")

        # Short-term
        st = summary["short_term"]
        lines.append("SHORT-TERM (held < 1 year)")
        lines.append("-" * 30)
        lines.append(f"  Transactions: {st['count']}")
        lines.append(f"  Proceeds:     ${st['proceeds']:,.2f}")
        lines.append(f"  Cost Basis:   ${st['cost_basis']:,.2f}")
        lines.append(f"  Gains:        ${st['gains']:,.2f}")
        lines.append(f"  Losses:       ${st['losses']:,.2f}")
        lines.append(f"  Net:          ${st['net']:,.2f}")
        lines.append("")

        # Long-term
        lt = summary["long_term"]
        lines.append("LONG-TERM (held >= 1 year)")
        lines.append("-" * 30)
        lines.append(f"  Transactions: {lt['count']}")
        lines.append(f"  Proceeds:     ${lt['proceeds']:,.2f}")
        lines.append(f"  Cost Basis:   ${lt['cost_basis']:,.2f}")
        lines.append(f"  Gains:        ${lt['gains']:,.2f}")
        lines.append(f"  Losses:       ${lt['losses']:,.2f}")
        lines.append(f"  Net:          ${lt['net']:,.2f}")
        lines.append("")

        # Total
        total_net = st["net"] + lt["net"]
        lines.append("TOTAL")
        lines.append("-" * 30)
        lines.append(f"  Net Gain/Loss: ${total_net:,.2f}")
        lines.append("")

        # By asset
        if summary["by_asset"]:
            lines.append("BY ASSET")
            lines.append("-" * 30)
            for asset, data in sorted(summary["by_asset"].items()):
                lines.append(f"  {asset}:")
                lines.append(f"    Transactions: {data['count']}")
                lines.append(f"    Net:          ${data['net']:,.2f}")

        return "\n".join(lines)
