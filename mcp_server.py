"""The Claude actions. Claude reads the receipt; these tools only store and look up."""
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import db

mcp = FastMCP(
    "ChatLedger",
    instructions=(
        "ChatLedger is the user's expense ledger. When the user shares a receipt "
        "(photo, PDF or text), read it yourself, then call add_expense once per receipt. "
        "Copy values exactly as printed. If a field is not on the receipt, leave it empty. "
        "Never guess a GST number or invoice number. After saving, tell the user the "
        "expense id and any flags the tool returns."
    ),
    host="0.0.0.0",
    stateless_http=True,
    json_response=True,
    # Render serves us on its own hostname, so the localhost-only host check must be off.
    # The secret in the URL is what protects this endpoint.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _row(e, flags):
    return {
        "id": e.id, "vendor": e.vendor, "date": e.expense_date.isoformat(),
        "amount": e.amount, "tax": e.tax, "currency": e.currency,
        "gst_number": e.gst_number, "invoice_number": e.invoice_number,
        "category": e.category, "status": e.status, "flags": flags,
    }


@mcp.tool()
def add_expense(
    vendor: str,
    date: str,
    amount: float,
    category: str = "Other",
    tax: float = 0.0,
    currency: str = "INR",
    gst_number: str = "",
    invoice_number: str = "",
    notes: str = "",
) -> dict:
    """Save one expense read from a receipt or invoice.

    vendor: the supplier name as printed.
    date: the receipt date as YYYY-MM-DD.
    amount: the total paid, tax included.
    category: one of Travel, Meals, Software, Office, Rent, Utilities, Marketing,
      Professional fees, Equipment, Other.
    tax: total GST or tax shown on the receipt, 0 if none is shown.
    gst_number: the supplier's GSTIN if printed, otherwise empty.
    invoice_number: the invoice or bill number if printed, otherwise empty.
    Returns the saved expense and any problems found, such as a possible duplicate,
    a missing GST number or a category over its monthly limit.
    """
    with db.SessionLocal() as s:
        try:
            d = db.parse_date(date)
        except ValueError as exc:
            return {"saved": False, "error": str(exc)}
        if amount is None or amount <= 0:
            return {"saved": False, "error": "Amount must be greater than 0."}
        if not vendor.strip():
            return {"saved": False, "error": "Vendor is required."}
        e = db.add_expense(
            s, vendor=vendor, expense_date=d, amount=amount, tax=tax,
            currency=currency, gst_number=gst_number, invoice_number=invoice_number,
            category=category, notes=notes, source="claude",
        )
        flags = db.compute_flags(s, [e])[e.id]
        return {"saved": True, "status": "pending approval", "expense": _row(e, flags)}


@mcp.tool()
def list_expenses(month: str = "", category: str = "", status: str = "") -> dict:
    """List saved expenses, newest first.

    month: optional, as YYYY-MM.
    category: optional category name.
    status: optional, one of pending, approved, rejected.
    """
    with db.SessionLocal() as s:
        rows = db.query_expenses(s, month=month or None, category=category or None,
                                 status=status or None)[:50]
        flags = db.compute_flags(s, rows)
        return {"count": len(rows), "expenses": [_row(r, flags[r.id]) for r in rows]}


@mcp.tool()
def monthly_summary(month: str) -> dict:
    """Total spend by category for one month (YYYY-MM). Rejected expenses are left out."""
    with db.SessionLocal() as s:
        cats, total, tax = db.summary(s, month)
        return {"month": month, "total": total, "total_tax": tax, "by_category": cats}
