import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

TZ = ZoneInfo("Asia/Kolkata")
LEDGER_URL = os.getenv("LEDGER_URL", "").strip()
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "12"))
MAX_WORKERS = int(os.getenv("FETCH_WORKERS", "12"))

app = FastAPI(title="Acme Appliances Ledger Agent", version="1.0.0")
ORDERS = []
RATES = {"USD": Decimal("1"), "EUR": Decimal("1.03"), "INR": Decimal("0.01243")}
DATA_READY = False
DATA_ERROR = None


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def endpoint(path: str, page=None) -> str:
    if not LEDGER_URL:
        raise RuntimeError("Set the LEDGER_URL environment variable to your assignment's questionData URL.")
    parsed = urlparse(LEDGER_URL)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["path"] = [path]
    if page is not None:
        query["page"] = [str(page)]
    else:
        query.pop("page", None)
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def get_json(url: str):
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.35 * (attempt + 1))
    raise RuntimeError(f"Could not retrieve ledger data: {last_error}")


def parse_export_response(response):
    # The export endpoint may return JSON or newline-delimited JSON.
    try:
        payload = response.json()
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            # A paginated /orders envelope is not a complete export.
            # Let load_orders() follow all pages instead of silently using page 1.
            if "pages" in payload and int(payload.get("pages") or 1) > 1:
                return []
            rows = payload.get("orders") or payload.get("data") or []
            if isinstance(rows, dict):
                rows = rows.get("orders", [])
        else:
            rows = []
        if rows and isinstance(rows, list) and any(isinstance(x, dict) and "id" in x for x in rows):
            return rows
    except Exception:
        pass

    rows = []
    for line in response.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and "id" in item:
            rows.append(item)
        elif isinstance(item, dict):
            nested = item.get("orders") or item.get("data")
            if isinstance(nested, list):
                rows.extend(x for x in nested if isinstance(x, dict) and "id" in x)
    return rows


def load_orders():
    # Prefer bulk export; if it is not a usable JSON/NDJSON response, use pagination.
    try:
        exported = parse_export_response(get_json(endpoint("/export")))
        if exported:
            return exported
    except Exception:
        pass

    first_response = get_json(endpoint("/orders", 1))
    first = first_response.json()
    rows = list(first.get("orders", []))
    total_pages = int(first.get("pages", 1))
    if total_pages > 1:
        def fetch_page(page):
            data = get_json(endpoint("/orders", page)).json()
            return data.get("orders", [])

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(fetch_page, p) for p in range(2, total_pages + 1)]
            for future in as_completed(futures):
                rows.extend(future.result())
    return rows


def parse_dt(value):
    if not value:
        return datetime.min.replace(tzinfo=TZ)
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except ValueError:
        return datetime.min.replace(tzinfo=TZ)


def load_data():
    global ORDERS, RATES, DATA_READY, DATA_ERROR
    try:
        raw_orders = load_orders()
        # Keep the latest updated_at row for each order ID.
        latest = {}
        for row in raw_orders:
            oid = str(row.get("id", ""))
            if not oid:
                continue
            if oid not in latest or parse_dt(row.get("updated_at")) > parse_dt(latest[oid].get("updated_at")):
                latest[oid] = row
        ORDERS = list(latest.values())

        try:
            rates_payload = get_json(endpoint("/rates")).json()
            raw_rates = rates_payload.get("usd_per_unit", {})
            if raw_rates:
                RATES = {str(k).upper(): Decimal(str(v)) for k, v in raw_rates.items()}
        except Exception:
            # Assignment data states the currency-rate endpoint is available.
            # Keep known fallback rates only if the rates endpoint temporarily fails.
            pass

        DATA_READY = True
        DATA_ERROR = None
    except Exception as exc:
        DATA_READY = False
        DATA_ERROR = str(exc)


@app.on_event("startup")
def startup_load():
    load_data()


def money_decimal(value):
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def order_date(row):
    return parse_dt(row.get("created_at")).date()


def parse_date_filters(question):
    q = question.lower()
    start = None
    end = None

    # Explicit ISO date range: "from 2026-03-01 to 2026-03-31" or "between ... and ..."
    iso_dates = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", q)
    if len(iso_dates) >= 2:
        try:
            start = date.fromisoformat(iso_dates[0])
            end = date.fromisoformat(iso_dates[1])
            return start, end
        except ValueError:
            pass
    elif len(iso_dates) == 1:
        try:
            start = date.fromisoformat(iso_dates[0])
            end = start
            return start, end
        except ValueError:
            pass

    # Quarter filters.
    quarter = re.search(r"\bq([1-4])\s*(20\d{2})\b|\b(first|second|third|fourth)\s+quarter\s+(20\d{2})\b", q)
    if quarter:
        if quarter.group(1):
            qnum, year = int(quarter.group(1)), int(quarter.group(2))
        else:
            qnum = {"first": 1, "second": 2, "third": 3, "fourth": 4}[quarter.group(3)]
            year = int(quarter.group(4))
        month = (qnum - 1) * 3 + 1
        start = date(year, month, 1)
        end = date(year + (month + 2) // 12, ((month + 2) % 12) + 1, 1) - timedelta(days=1)
        return start, end

    # Named month and optional year.
    months = {
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
        "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
        "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    }
    for name, month in months.items():
        match = re.search(r"\b" + re.escape(name) + r"\s*(20\d{2})?\b|\b(20\d{2})\s+" + re.escape(name) + r"\b", q)
        if match:
            year_text = match.group(1) or match.group(2)
            if year_text:
                year = int(year_text)
                start = date(year, month, 1)
                end = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
                return start, end
            # A month without a year means that month across all years.
            return ("month", month), None

    year_match = re.search(r"\b(20\d{2})\b", q)
    if year_match:
        year = int(year_match.group(1))
        if 2000 <= year <= 2099:
            return date(year, 1, 1), date(year, 12, 31)

    today = datetime.now(TZ).date()
    if "last month" in q or "previous month" in q:
        first_this_month = today.replace(day=1)
        end = first_this_month - timedelta(days=1)
        return end.replace(day=1), end
    if "this month" in q or "current month" in q:
        return today.replace(day=1), today
    if "last year" in q or "previous year" in q:
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    return None, None


def extract_filters(question):
    q = question.lower()
    filters = {"region": None, "product": None, "customer": None, "status": None}
    regions = sorted({str(o.get("region", "")) for o in ORDERS if o.get("region")}, key=len, reverse=True)
    products = sorted({str(o.get("product", "")) for o in ORDERS if o.get("product")}, key=len, reverse=True)
    customers = sorted({str(o.get("customer", "")) for o in ORDERS if o.get("customer")}, key=len, reverse=True)

    for region in regions:
        if re.search(r"\b" + re.escape(region.lower()) + r"\b", q):
            filters["region"] = region
            break
    for product in products:
        if product.lower() in q:
            filters["product"] = product
            break
    for customer in customers:
        if customer.lower() in q:
            filters["customer"] = customer
            break

    for status in ("paid", "refunded", "void", "pending", "cancelled", "canceled"):
        if re.search(r"\b" + status + r"\b", q):
            filters["status"] = status
            break

    start, end = parse_date_filters(question)
    filters["start_date"] = start
    filters["end_date"] = end

    target_currency = "USD"
    currency_patterns = {
        "USD": r"\b(usd|us dollars?|dollars?)\b",
        "EUR": r"\b(eur|euros?)\b",
        "INR": r"\b(inr|rupees?|rs\.?)\b",
    }
    for currency, pattern in currency_patterns.items():
        if re.search(pattern, q):
            target_currency = currency
            break
    filters["target_currency"] = target_currency
    return filters


def matches_filters(row, filters):
    if filters["region"] and str(row.get("region", "")).lower() != filters["region"].lower():
        return False
    if filters["product"] and str(row.get("product", "")).lower() != filters["product"].lower():
        return False
    if filters["customer"] and str(row.get("customer", "")).lower() != filters["customer"].lower():
        return False
    if filters["status"] and str(row.get("status", "")).lower() != filters["status"].lower():
        return False

    d = order_date(row)
    start, end = filters["start_date"], filters["end_date"]
    if isinstance(start, tuple) and start[0] == "month":
        if d.month != start[1]:
            return False
    else:
        if start and d < start:
            return False
        if end and d > end:
            return False
    return True


def usd_amount(row):
    amount = money_decimal(row.get("amount", 0))
    currency = str(row.get("currency", "USD")).upper()
    rate = RATES.get(currency)
    if rate is None:
        return Decimal("0")
    return amount * rate


def amount_in_target(row, target_currency):
    usd = usd_amount(row)
    rate = RATES.get(target_currency, Decimal("1"))
    if rate == 0:
        return Decimal("0")
    return usd / rate


def rounded_money(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def question_answer(question):
    if not DATA_READY:
        raise RuntimeError(DATA_ERROR or "Ledger data is not loaded.")
    q = question.lower()
    filters = extract_filters(question)
    matching = [row for row in ORDERS if matches_filters(row, filters)]

    # Determine metric intent.
    asks_refund = any(word in q for word in ("refund", "refunded", "reimburse"))
    asks_revenue = any(word in q for word in ("revenue", "sales", "turnover", "earned", "income", "spent", "spending", "value of"))
    asks_average = any(word in q for word in ("average", "avg", "mean"))
    asks_count = any(word in q for word in ("how many", "number of", "count", "quantity of", "count of"))
    asks_units = any(word in q for word in ("units sold", "items sold", "quantity sold", "total units", "how many units"))
    asks_customer_count = "customer" in q and asks_count
    asks_order_count = "order" in q and asks_count
    asks_product_count = "product" in q and asks_count and not asks_units
    asks_top = any(word in q for word in ("most", "highest", "top", "best", "largest", "maximum", "max "))
    asks_lowest = any(word in q for word in ("least", "lowest", "smallest", "minimum", "min "))
    target_currency = filters["target_currency"]

    if asks_refund:
        subset = [r for r in matching if str(r.get("status", "")).lower() == "refunded"]
        if asks_count or "number of refunds" in q or "how many refunds" in q:
            return len(subset)
        total = sum((amount_in_target(r, target_currency) for r in subset), Decimal("0"))
        return float(rounded_money(total))

    # Revenue and sales should use current paid orders only, unless a status was explicitly requested.
    if asks_revenue or asks_average or asks_units:
        subset = matching if filters["status"] else [r for r in matching if str(r.get("status", "")).lower() == "paid"]
        if asks_units:
            return sum(int(r.get("qty", 0) or 0) for r in subset)
        if asks_average:
            if not subset:
                return 0
            total = sum((amount_in_target(r, target_currency) for r in subset), Decimal("0"))
            return float(rounded_money(total / Decimal(len(subset))))
        total = sum((amount_in_target(r, target_currency) for r in subset), Decimal("0"))
        return float(rounded_money(total))

    if asks_customer_count:
        return len({r.get("customer") for r in matching if r.get("customer")})
    if asks_order_count:
        return len(matching)
    if asks_product_count:
        return len({r.get("product") for r in matching if r.get("product")})

    # Rankings / "which product/customer/region..." questions.
    if asks_top or asks_lowest or re.search(r"\b(which|what)\s+(product|customer|region)\b", q):
        if "customer" in q:
            group_field = "customer"
        elif "region" in q:
            group_field = "region"
        else:
            group_field = "product"

        # For spending/revenue rankings, consider paid orders; for refunds, refunded orders.
        if asks_refund:
            rank_rows = [r for r in matching if str(r.get("status", "")).lower() == "refunded"]
        elif any(w in q for w in ("revenue", "sales", "spent", "spending", "amount", "value")):
            rank_rows = [r for r in matching if str(r.get("status", "")).lower() == "paid"]
        else:
            rank_rows = [r for r in matching if str(r.get("status", "")).lower() == "paid"]

        if not rank_rows:
            return ""
        group_values = {}
        for row in rank_rows:
            key = row.get(group_field)
            if not key:
                continue
            if group_field == "product" and any(w in q for w in ("units", "quantity", "popular", "purchased", "sold")):
                value = Decimal(str(row.get("qty", 0) or 0))
            elif group_field == "region" and not any(w in q for w in ("revenue", "sales", "spent", "amount", "value")):
                value = Decimal("1")
            elif group_field == "customer" and not any(w in q for w in ("revenue", "sales", "spent", "amount", "value")):
                value = Decimal("1")
            else:
                value = amount_in_target(row, target_currency)
            group_values[key] = group_values.get(key, Decimal("0")) + value
        if not group_values:
            return ""
        chooser = min if asks_lowest else max
        return chooser(group_values.items(), key=lambda kv: kv[1])[0]

    # General count fallbacks for questions like "how many refunds/orders/customers".
    if "refund" in q:
        return sum(1 for r in matching if str(r.get("status", "")).lower() == "refunded")
    if "customer" in q and any(w in q for w in ("unique", "distinct", "different")):
        return len({r.get("customer") for r in matching if r.get("customer")})
    if "order" in q:
        return len(matching)

    # If a filter identifies one product/customer, return its paid revenue as the most useful default.
    subset = [r for r in matching if str(r.get("status", "")).lower() == "paid"]
    total = sum((amount_in_target(r, target_currency) for r in subset), Decimal("0"))
    return float(rounded_money(total))


@app.get("/")
def health():
    return {
        "service": "Acme Appliances Ledger Agent",
        "ready": DATA_READY,
        "orders_loaded": len(ORDERS),
        "error": DATA_ERROR,
    }


@app.post("/")
def answer(request: QuestionRequest):
    try:
        result = question_answer(request.question.strip())
        return {"answer": result}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
