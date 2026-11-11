"""ChatLedger: web app, login, pages, CSV, accountant link. No AI keys anywhere."""
import csv
import io
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, Response, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

import db
from mcp_server import mcp

ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
MCP_SECRET = os.environ.get("MCP_SECRET", "")

missing = [n for n, v in [("ADMIN_USER", ADMIN_USER), ("ADMIN_PASSWORD", ADMIN_PASSWORD),
                          ("SESSION_SECRET", SESSION_SECRET), ("MCP_SECRET", MCP_SECRET)] if not v]
if missing:
    raise RuntimeError("Missing environment variables: " + ", ".join(missing))
if "/" in MCP_SECRET or " " in MCP_SECRET:
    raise RuntimeError("MCP_SECRET must not contain spaces or slashes. Use letters and digits.")

mcp_asgi = mcp.streamable_http_app()  # must exist before the lifespan starts


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    async with mcp.session_manager.run():
        yield


web = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
web.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax",
                   https_only=os.environ.get("INSECURE_COOKIES") != "1", max_age=60 * 60 * 24 * 14)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def inr(value, currency="INR"):
    """Indian digit grouping, e.g. 1,23,456.00"""
    if value is None:
        return ""
    neg = value < 0
    whole, frac = f"{abs(value):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    sym = "₹" if currency == "INR" else currency + " "
    return f"{'-' if neg else ''}{sym}{whole}.{frac}"


templates.env.filters["inr"] = inr


def authed(request):
    return request.session.get("user") == ADMIN_USER


def need_login(request):
    return RedirectResponse("/login", status_code=303)


def render(request, name, **ctx):
    return templates.TemplateResponse(request, name, ctx)


# ---------- login ----------
@web.get("/healthz")
def healthz():
    return {"ok": True}


@web.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if authed(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", error=None)


@web.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    ok_user = secrets.compare_digest(username.encode(), ADMIN_USER.encode())
    ok_pass = secrets.compare_digest(password.encode(), ADMIN_PASSWORD.encode())
    if ok_user and ok_pass:
        request.session["user"] = ADMIN_USER
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", error="That username and password don't match.")


@web.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- screen 1: ledger ----------
def ledger_context(s, month, category, status):
    rows = db.query_expenses(s, month=month or None, category=category or None,
                             status=status or None)
    flags = db.compute_flags(s, rows)
    live = [r for r in rows if r.status != "rejected"]
    return {
        "rows": rows, "flags": flags, "month": month or "", "category": category or "",
        "status": status or "", "months": db.months_with_data(s),
        "categories": db.CATEGORIES,
        "total": sum(r.amount for r in live), "tax_total": sum((r.tax or 0) for r in live),
        "flagged": sum(1 for r in rows if flags[r.id]),
    }


@web.get("/", response_class=HTMLResponse)
def ledger(request: Request, month: str = "", category: str = "", status: str = ""):
    if not authed(request):
        return need_login(request)
    with db.SessionLocal() as s:
        return render(request, "ledger.html", shared=False, token=None,
                      **ledger_context(s, month, category, status))


# ---------- screen 2: expense form ----------
def form_to_values(form):
    d = db.parse_date(form["expense_date"])
    amount = float(form["amount"])
    if amount <= 0:
        raise ValueError("Amount must be greater than 0.")
    if not form["vendor"].strip():
        raise ValueError("Vendor is required.")
    return dict(
        vendor=form["vendor"].strip(), expense_date=d, amount=amount,
        tax=float(form.get("tax") or 0), currency=(form.get("currency") or "INR").upper(),
        gst_number=(form.get("gst_number") or "").strip().upper(),
        invoice_number=(form.get("invoice_number") or "").strip(),
        category=form.get("category") if form.get("category") in db.CATEGORIES else "Other",
        notes=form.get("notes") or "",
    )


@web.get("/expenses/new", response_class=HTMLResponse)
def new_expense(request: Request):
    if not authed(request):
        return need_login(request)
    blank = db.Expense(vendor="", expense_date=date.today(), amount=0, tax=0, currency="INR",
                       gst_number="", invoice_number="", category="Other", notes="",
                       status="pending")
    return render(request, "expense.html", e=blank, is_new=True, flags=[], error=None,
                  categories=db.CATEGORIES)


@web.post("/expenses/new")
async def create_expense(request: Request):
    if not authed(request):
        return need_login(request)
    form = dict(await request.form())
    with db.SessionLocal() as s:
        try:
            vals = form_to_values(form)
        except (ValueError, KeyError) as exc:
            blank = db.Expense(vendor=form.get("vendor", ""), expense_date=date.today(),
                               amount=0, tax=0, currency="INR", gst_number="",
                               invoice_number="", category="Other", notes="", status="pending")
            return render(request, "expense.html", e=blank, is_new=True, flags=[],
                          error=str(exc), categories=db.CATEGORIES)
        e = db.add_expense(s, source="manual", **vals)
    return RedirectResponse(f"/expenses/{e.id}?saved=1", status_code=303)


@web.get("/expenses/{eid}", response_class=HTMLResponse)
def view_expense(request: Request, eid: int, saved: int = 0):
    if not authed(request):
        return need_login(request)
    with db.SessionLocal() as s:
        e = s.get(db.Expense, eid)
        if not e:
            return RedirectResponse("/", status_code=303)
        return render(request, "expense.html", e=e, is_new=False,
                      flags=db.compute_flags(s, [e])[e.id], error=None,
                      saved=bool(saved), categories=db.CATEGORIES)


@web.post("/expenses/{eid}")
async def save_expense(request: Request, eid: int):
    if not authed(request):
        return need_login(request)
    form = dict(await request.form())
    with db.SessionLocal() as s:
        e = s.get(db.Expense, eid)
        if not e:
            return RedirectResponse("/", status_code=303)
        try:
            vals = form_to_values(form)
        except (ValueError, KeyError) as exc:
            return render(request, "expense.html", e=e, is_new=False,
                          flags=db.compute_flags(s, [e])[e.id], error=str(exc),
                          categories=db.CATEGORIES)
        for k, v in vals.items():
            setattr(e, k, v)
        if form.get("status") in ("pending", "approved", "rejected"):
            e.status = form["status"]
        s.commit()
    return RedirectResponse(f"/expenses/{eid}?saved=1", status_code=303)


@web.post("/expenses/{eid}/status")
def set_status(request: Request, eid: int, status: str = Form(...), back: str = Form("/")):
    if not authed(request):
        return need_login(request)
    if status not in ("pending", "approved", "rejected"):
        return RedirectResponse("/", status_code=303)
    with db.SessionLocal() as s:
        e = s.get(db.Expense, eid)
        if e:
            e.status = status
            s.commit()
    return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/",
                            status_code=303)


@web.post("/expenses/{eid}/delete")
def delete_expense(request: Request, eid: int):
    if not authed(request):
        return need_login(request)
    with db.SessionLocal() as s:
        e = s.get(db.Expense, eid)
        if e:
            s.delete(e)
            s.commit()
    return RedirectResponse("/", status_code=303)


# ---------- screen 3: monthly summary ----------
@web.get("/summary", response_class=HTMLResponse)
def summary_page(request: Request, month: str = ""):
    if not authed(request):
        return need_login(request)
    with db.SessionLocal() as s:
        months = db.months_with_data(s)
        month = month or (months[0] if months else date.today().strftime("%Y-%m"))
        cats, total, tax = db.summary(s, month)
        return render(request, "summary.html", month=month, months=months, cats=cats,
                      total=total, tax=tax)


# ---------- screen 4: settings ----------
@web.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = ""):
    if not authed(request):
        return need_login(request)
    base = str(request.base_url)
    with db.SessionLocal() as s:
        limits = {l.category: l.monthly_limit for l in s.scalars(select(db.CategoryLimit)).all()}
        token = db.get_share_token(s)
    return render(request, "settings.html", categories=db.CATEGORIES, limits=limits,
                  mcp_url=f"{base}mcp/{MCP_SECRET}", share_url=f"{base}share/{token}", msg=msg)


@web.post("/settings/limits")
async def save_limits(request: Request):
    if not authed(request):
        return need_login(request)
    form = await request.form()
    with db.SessionLocal() as s:
        for cat in db.CATEGORIES:
            raw = (form.get(f"limit_{cat}") or "").strip().replace(",", "")
            row = s.get(db.CategoryLimit, cat)
            try:
                val = float(raw) if raw else 0
            except ValueError:
                val = 0
            if val > 0:
                if row:
                    row.monthly_limit = val
                else:
                    s.add(db.CategoryLimit(category=cat, monthly_limit=val))
            elif row:
                s.delete(row)
        s.commit()
    return RedirectResponse("/settings?msg=Limits+saved", status_code=303)


@web.post("/settings/share/reset")
def reset_share(request: Request):
    if not authed(request):
        return need_login(request)
    with db.SessionLocal() as s:
        db.reset_share_token(s)
    return RedirectResponse("/settings?msg=Accountant+link+replaced.+The+old+link+no+longer+works",
                            status_code=303)


# ---------- CSV ----------
def make_csv(rows, flags):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Date", "Vendor", "Category", "Amount", "Tax", "Currency",
                "GST number", "Invoice number", "Status", "Flags", "Notes"])
    for r in rows:
        w.writerow([r.id, r.expense_date.isoformat(), r.vendor, r.category, f"{r.amount:.2f}",
                    f"{(r.tax or 0):.2f}", r.currency, r.gst_number, r.invoice_number,
                    r.status, "; ".join(flags[r.id]), r.notes])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="chatledger.csv"'})


@web.get("/export.csv")
def export_csv(request: Request, month: str = "", category: str = "", status: str = ""):
    if not authed(request):
        return need_login(request)
    with db.SessionLocal() as s:
        rows = db.query_expenses(s, month=month or None, category=category or None,
                                 status=status or None)
        return make_csv(rows, db.compute_flags(s, rows))


# ---------- accountant link (read-only) ----------
def check_token(s, token):
    return secrets.compare_digest(token.encode(), db.get_share_token(s).encode())


@web.get("/share/{token}", response_class=HTMLResponse)
def shared_ledger(request: Request, token: str, month: str = "", category: str = ""):
    with db.SessionLocal() as s:
        if not check_token(s, token):
            return HTMLResponse("This link is not valid. Ask the owner for a new one.", status_code=404)
        # accountants only see expenses that are not rejected
        ctx = ledger_context(s, month, category, "")
        ctx["rows"] = [r for r in ctx["rows"] if r.status != "rejected"]
        return render(request, "ledger.html", shared=True, token=token, **ctx)


@web.get("/share/{token}/export.csv")
def shared_csv(token: str, month: str = "", category: str = ""):
    with db.SessionLocal() as s:
        if not check_token(s, token):
            return HTMLResponse("This link is not valid.", status_code=404)
        rows = [r for r in db.query_expenses(s, month=month or None, category=category or None)
                if r.status != "rejected"]
        return make_csv(rows, db.compute_flags(s, rows))


# ---------- gateway: secret MCP link + website on one port ----------
class Gateway:
    """/mcp/<MCP_SECRET> goes to the Claude actions. Everything else is the website."""

    def __init__(self, website, mcp_app, secret):
        self.website, self.mcp_app, self.secret = website, mcp_app, secret

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            parts = scope["path"].split("/")  # ['', 'mcp', '<secret>', ...]
            given = parts[2] if len(parts) > 2 else ""
            if secrets.compare_digest(given.encode(), self.secret.encode()):
                inner = dict(scope, path="/mcp", raw_path=b"/mcp")
                return await self.mcp_app(inner, receive, send)
            resp = Response("Not found", status_code=404)
            return await resp(scope, receive, send)
        return await self.website(scope, receive, send)


app = Gateway(web, mcp_asgi, MCP_SECRET)
