"""Validated journal model and exact-decimal, account-scoped position accounting.

Trades are ordered by execution date/time, numeric sequence, then stable id.
An unspecified SELL removes the same proportion of every surviving lot.  An
explicit lot_id removes only that lot.  No foreign-exchange conversion is made.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re
from uuid import uuid4


TABLES = ("accounts", "instruments", "trades", "dividends", "dividend_events")
ZERO = Decimal(0)
_DECIMAL = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SECRET = re.compile(r"token|password|secret|credential|authorization|apikey|privatekey", re.I)


class ValidationError(ValueError):
    """The journal cannot safely be interpreted as a consistent financial ledger."""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id():
    return str(uuid4())


def empty_state():
    return {"schema_version": 1, **{table: {} for table in TABLES}}


def _text(value, path, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValidationError(f"{path}: expected {'text' if allow_empty else 'nonempty text'}")
    if len(value) > 100_000 or "\x00" in value:
        raise ValidationError(f"{path}: invalid text")
    return value


def _currency(value, path):
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise ValidationError(f"{path}: expected a three-letter uppercase currency")


def _when(value, path, date_only=False):
    _text(value, path)
    try:
        if date_only:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            return datetime.combine(date.fromisoformat(value), datetime.min.time(), timezone.utc)
        if not re.match(r"^\d{4}-\d{2}-\d{2}(?:T|$)", value):
            raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValidationError(f"{path}: invalid ISO date or timestamp") from None


def decimal_value(value, path="value", positive=False, string_only=True):
    if string_only and not isinstance(value, str):
        raise ValidationError(f"{path}: decimal values must be stored as strings")
    if isinstance(value, bool) or isinstance(value, float):
        raise ValidationError(f"{path}: use exact decimal text")
    raw = str(value)
    if len(raw) > 128 or not _DECIMAL.fullmatch(raw):
        raise ValidationError(f"{path}: invalid decimal")
    try:
        result = Decimal(raw)
        if not result.is_finite() or result < 0 or (positive and result == 0):
            raise ValueError
        if result != 0 and (result.adjusted() > 40 or result.adjusted() < -40):
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{path}: expected a {'positive' if positive else 'nonnegative'} finite decimal") from None


def _safe_json(value, path="journal", depth=0):
    if depth > 12:
        raise ValidationError(f"{path}: excessive nesting")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"{path}: object keys must be text")
            # The first nested level contains record ids, not field names.
            if depth != 1 and _SECRET.search(re.sub(r"[^a-zA-Z]", "", key)):
                raise ValidationError(f"{path}: credential fields are not allowed in journal data")
            _safe_json(item, f"{path}/{key}", depth + 1)
    elif isinstance(value, list):
        for item in value:
            _safe_json(item, path, depth + 1)
    elif value is not None and not isinstance(value, (str, int, bool)):
        raise ValidationError(f"{path}: only JSON text, integers, booleans, lists and objects are supported")


def _reference(state, table, id, path):
    _text(id, path)
    record = state[table].get(id)
    if not record or record.get("deleted"):
        raise ValidationError(f"{path}: missing or deleted {table} reference")
    return record


def validate_state(state):
    """Raise ValidationError for malformed, inconsistent, or oversold journals."""
    if not isinstance(state, dict) or set(state) != {"schema_version", *TABLES}:
        raise ValidationError("Journal must contain only schema_version and the five journal tables")
    if type(state["schema_version"]) is not int or state["schema_version"] != 1:
        raise ValidationError("Unsupported journal schema version")
    _safe_json(state)
    for table in TABLES:
        if not isinstance(state[table], dict):
            raise ValidationError(f"{table}: expected an object keyed by record id")
        for id, row in state[table].items():
            path = f"{table}/{id}"
            _text(id, path)
            if not isinstance(row, dict) or row.get("id") != id:
                raise ValidationError(f"{path}: record id must match its key")
            _when(row.get("updated_at"), f"{path}/updated_at")
            if type(row.get("deleted")) is not bool:
                raise ValidationError(f"{path}/deleted: expected true or false")
            if row["deleted"]:
                continue
            _currency(row.get("currency"), f"{path}/currency")
            if table == "accounts":
                for name in ("name", "broker", "kind"):
                    _text(row.get(name), f"{path}/{name}", allow_empty=name == "broker")
            elif table == "instruments":
                for name in ("market", "code", "name", "asset_type", "sector"):
                    _text(row.get(name), f"{path}/{name}", allow_empty=name == "sector")
                if id != f"{row['market']}:{row['code']}":
                    raise ValidationError(f"{path}: instrument id must be market:code")
            else:
                instrument = _reference(state, "instruments", row.get("instrument_id"), f"{path}/instrument_id")
                if row["currency"] != instrument["currency"]:
                    raise ValidationError(f"{path}: currency differs from the instrument")
                if table in ("trades", "dividends"):
                    _reference(state, "accounts", row.get("account_id"), f"{path}/account_id")
                if table == "trades":
                    _when(row.get("executed_at"), f"{path}/executed_at")
                    if row.get("side") not in ("BUY", "SELL", "OPENING"):
                        raise ValidationError(f"{path}/side: expected BUY, SELL, or OPENING")
                    decimal_value(row.get("quantity"), f"{path}/quantity", positive=True)
                    for name in ("price", "fee", "tax"):
                        decimal_value(row.get(name), f"{path}/{name}")
                    if "sequence" in row and type(row["sequence"]) is not int:
                        raise ValidationError(f"{path}/sequence: expected an integer")
                    if "lot_id" in row and row["lot_id"] is not None:
                        _text(row["lot_id"], f"{path}/lot_id")
                elif table == "dividends":
                    _when(row.get("paid_date"), f"{path}/paid_date", date_only=True)
                    gross = decimal_value(row.get("gross"), f"{path}/gross")
                    tax = decimal_value(row.get("tax"), f"{path}/tax")
                    if tax > gross:
                        raise ValidationError(f"{path}: dividend tax exceeds gross income")
                else:
                    for name in ("record_date", "pay_date"):
                        if row.get(name):
                            _when(row[name], f"{path}/{name}", date_only=True)
                    decimal_value(row.get("amount_per_share"), f"{path}/amount_per_share")
                    if row.get("status") not in ("reference", "announced"):
                        raise ValidationError(f"{path}/status: expected reference or announced")
                    _text(row.get("source"), f"{path}/source")
            for name in ("source", "external_id", "memo"):
                if name in row and row[name] is not None:
                    _text(row[name], f"{path}/{name}", allow_empty=True)
    with localcontext() as context:
        context.prec = 50
        _positions(state)
    return state


def _positions(state):
    positions = {}

    def position(row):
        key = (row["account_id"], row["instrument_id"])
        if key not in positions:
            positions[key] = {"account_id": key[0], "instrument_id": key[1], "currency": row["currency"], "lots": {}, "realized_pnl": ZERO, "dividends_net": ZERO}
        return positions[key]

    trades = [row for row in state["trades"].values() if not row["deleted"]]
    trades.sort(key=lambda row: (_when(row["executed_at"], "executed_at"), row.get("sequence", 0), row["id"]))
    for row in trades:
        p = position(row)
        lots = p["lots"]
        quantity, price, fee, tax = (Decimal(row[name]) for name in ("quantity", "price", "fee", "tax"))
        if row["side"] in ("BUY", "OPENING"):
            lot_id = row.get("lot_id") or row["id"]
            if lot_id in lots:
                raise ValidationError(f"trades/{row['id']}: acquisition lot id is duplicated")
            lots[lot_id] = {"quantity": quantity, "cost": quantity * price + fee + tax}
            continue
        available = sum((lot["quantity"] for lot in lots.values()), ZERO)
        if quantity > available:
            raise ValidationError(f"trades/{row['id']}: sale exceeds available holding")
        if row.get("lot_id"):
            lot = lots.get(row["lot_id"])
            if lot is None or quantity > lot["quantity"]:
                raise ValidationError(f"trades/{row['id']}: sale exceeds the specified lot")
            removed_cost = lot["cost"] if quantity == lot["quantity"] else lot["cost"] * quantity / lot["quantity"]
            lot["quantity"] -= quantity
            lot["cost"] -= removed_cost
        elif quantity == available:
            # A full exit must zero lots exactly. Multiplying and dividing a
            # previously fractional lot can otherwise leave a phantom 1E-50.
            removed_cost = sum((lot["cost"] for lot in lots.values()), ZERO)
            for lot in lots.values():
                lot["quantity"] = lot["cost"] = ZERO
        else:
            active = [lot for lot in lots.values() if lot["quantity"]]
            removed_cost, remaining_quantity = ZERO, quantity
            for index, lot in enumerate(active):
                part = remaining_quantity if index == len(active) - 1 else quantity * lot["quantity"] / available
                cost = lot["cost"] if part == lot["quantity"] else lot["cost"] * part / lot["quantity"]
                lot["quantity"] -= part
                lot["cost"] -= cost
                removed_cost += cost
                remaining_quantity -= part
        p["realized_pnl"] += quantity * price - fee - tax - removed_cost
    for row in state["dividends"].values():
        if not row["deleted"]:
            position(row)["dividends_net"] += Decimal(row["gross"]) - Decimal(row["tax"])
    return positions


def portfolio(state, quotes=None, account_id=None):
    """Return position rows and separate currency totals using Decimal precision 50.

    quotes maps instrument ids to exact-decimal prices or {price, currency}
    objects.  Missing prices produce None for positive holdings' market value,
    unrealized gain, and total gain, including the affected currency totals.
    """
    validate_state(state)
    quotes = quotes or {}
    if not isinstance(quotes, dict):
        raise ValidationError("quotes must map instrument ids to prices")
    rows, totals = [], {}
    sum_fields = ("quantity", "cost", "market_value", "unrealized_pnl", "realized_pnl", "dividends_net", "total_pnl")
    with localcontext() as context:
        context.prec = 50
        positions = _positions(state)
        for key in sorted(positions):
            p = positions[key]
            if account_id is not None and p["account_id"] != account_id:
                continue
            quantity = sum((lot["quantity"] for lot in p["lots"].values()), ZERO)
            cost = sum((lot["cost"] for lot in p["lots"].values()), ZERO)
            quote = quotes.get(p["instrument_id"])
            if isinstance(quote, dict):
                if quote.get("currency", p["currency"]) != p["currency"]:
                    raise ValidationError("Quote currency differs from the instrument")
                quote = quote.get("price")
            price = None if quote is None else decimal_value(quote, "quote price", string_only=False)
            market_value = ZERO if quantity == 0 else (None if price is None else quantity * price)
            unrealized = None if market_value is None else market_value - cost
            total = None if unrealized is None else unrealized + p["realized_pnl"] + p["dividends_net"]
            row = {name: p[name] for name in ("account_id", "instrument_id", "currency", "realized_pnl", "dividends_net")}
            row.update(quantity=quantity, cost=cost, average_cost=cost / quantity if quantity else ZERO,
                       price=price, market_value=market_value, unrealized_pnl=unrealized, total_pnl=total)
            rows.append(row)
            bucket = totals.setdefault(p["currency"], {**{name: ZERO for name in sum_fields}, "unpriced_count": 0})
            bucket["unpriced_count"] += int(quantity > 0 and price is None)
            for name in sum_fields:
                bucket[name] = None if bucket[name] is None or row[name] is None else bucket[name] + row[name]
    return {"rows": rows, "totals": totals}
