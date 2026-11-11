"""Database tables and the plain-rule checks. No AI in this file."""
import os
import secrets
from datetime import date, datetime
from collections import defaultdict

from sqlalchemy import (create_engine, Column, Integer, String, Float, Date,
                        DateTime, Text, select, func)
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///chatledger.db")
# Neon/Heroku style strings sometimes start with postgres://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()

CATEGORIES = ["Travel", "Meals", "Software", "Office", "Rent", "Utilities",
              "Marketing", "Professional fees", "Equipment", "Other"]


class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True)
    vendor = Column(String(200), nullable=False)
    expense_date = Column(Date, nullable=False)
    amount = Column(Float, nullable=False)          # total paid, tax included
    tax = Column(Float, default=0.0)
    currency = Column(String(8), default="INR")
    gst_number = Column(String(32), default="")
    invoice_number = Column(String(80), default="")
    category = Column(String(60), default="Other")
    notes = Column(Text, default="")
    status = Column(String(16), default="pending")  # pending / approved / rejected
    source = Column(String(16), default="claude")   # claude / manual
    created_at = Column(DateTime, default=datetime.utcnow)


class CategoryLimit(Base):
    __tablename__ = "category_limits"
    category = Column(String(60), primary_key=True)
    monthly_limit = Column(Float, nullable=False)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(60), primary_key=True)
    value = Column(Text, nullable=False)


def init_db():
    Base.metadata.create_all(engine)


def get_share_token(s):
    row = s.get(Setting, "share_token")
    if not row:
        row = Setting(key="share_token", value=secrets.token_urlsafe(24))
        s.add(row)
        s.commit()
    return row.value


def reset_share_token(s):
    row = s.get(Setting, "share_token")
    new = secrets.token_urlsafe(24)
    if row:
        row.value = new
    else:
        s.add(Setting(key="share_token", value=new))
    s.commit()
    return new


def parse_date(text):
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Could not read date '{text}'. Use YYYY-MM-DD.")


def month_key(d):
    return d.strftime("%Y-%m")


def compute_flags(s, expenses):
    """Plain rules. Returns {expense_id: [flag, ...]}.
    Rejected expenses are ignored when looking for duplicates and limits."""
    live = [e for e in s.scalars(select(Expense)).all() if e.status != "rejected"]
    limits = {l.category: l.monthly_limit for l in s.scalars(select(CategoryLimit)).all()}

    groups = defaultdict(list)
    for e in live:
        groups[(e.vendor.strip().lower(), round(e.amount, 2), e.expense_date)].append(e.id)

    spend = defaultdict(float)
    for e in live:
        spend[(e.category, month_key(e.expense_date))] += e.amount

    out = {}
    for e in expenses:
        flags = []
        if e.status != "rejected":
            ids = groups.get((e.vendor.strip().lower(), round(e.amount, 2), e.expense_date), [])
            if len(ids) > 1:
                others = ", ".join(f"#{i}" for i in ids if i != e.id)
                flags.append(f"Possible duplicate of {others}")
        if e.tax and e.tax > 0 and not (e.gst_number or "").strip():
            flags.append("Missing GST number (input credit at risk)")
        if not (e.invoice_number or "").strip():
            flags.append("Missing invoice number")
        lim = limits.get(e.category)
        if lim and e.status != "rejected":
            total = spend[(e.category, month_key(e.expense_date))]
            if total > lim:
                flags.append(f"{e.category} is over its monthly limit "
                             f"({total:,.0f} of {lim:,.0f})")
        out[e.id] = flags
    return out


def query_expenses(s, month=None, category=None, status=None):
    q = select(Expense).order_by(Expense.expense_date.desc(), Expense.id.desc())
    if category:
        q = q.where(Expense.category == category)
    if status:
        q = q.where(Expense.status == status)
    rows = s.scalars(q).all()
    if month:
        rows = [r for r in rows if month_key(r.expense_date) == month]
    return rows


def summary(s, month):
    rows = [r for r in query_expenses(s, month=month) if r.status != "rejected"]
    cats = defaultdict(lambda: {"count": 0, "total": 0.0, "tax": 0.0})
    for r in rows:
        c = cats[r.category]
        c["count"] += 1
        c["total"] += r.amount
        c["tax"] += r.tax or 0
    limits = {l.category: l.monthly_limit for l in s.scalars(select(CategoryLimit)).all()}
    result = []
    for name, c in sorted(cats.items(), key=lambda kv: -kv[1]["total"]):
        result.append({"category": name, **c, "limit": limits.get(name)})
    return result, sum(r.amount for r in rows), sum((r.tax or 0) for r in rows)


def months_with_data(s):
    ds = s.scalars(select(Expense.expense_date)).all()
    return sorted({month_key(d) for d in ds}, reverse=True)


def add_expense(s, *, vendor, expense_date, amount, tax=0.0, currency="INR",
                gst_number="", invoice_number="", category="Other", notes="",
                source="claude"):
    if category not in CATEGORIES:
        category = "Other"
    e = Expense(vendor=vendor.strip(), expense_date=expense_date, amount=float(amount),
                tax=float(tax or 0), currency=(currency or "INR").upper(),
                gst_number=(gst_number or "").strip().upper(),
                invoice_number=(invoice_number or "").strip(),
                category=category, notes=notes or "", source=source)
    s.add(e)
    s.commit()
    return e
