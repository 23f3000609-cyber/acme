
import re
import json
import sqlite3
import threading

import httpx
import pandas as pd
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware


# ============================================================
# 1. CONFIGURATION
# ============================================================

RATES_URL = "https://exam.sanand.workers.dev/questionData?email=23f3000609%40ds.study.iitm.ac.in&quizSign=JcOxfttdcmr8%2BnijockJLo2Ovxe%2BtMiUf2%2FVufz%2B5kw5FFuYeMG8HjzQWxe2Jow1vHIfl8JaVHDPABATzTMhgFLFJnO08FTK2i70746JNS2kpvHp6qDGv8wfdd%2BudUwTbym8yL1yc2MkukRkumh02EKUajMUeQnn8jzBHs5yNjHyH2Xgg7LyWI8dOeWB6mvOVXy7ch3TwTAhIJB1RPVM59pRJOpuvVTPCNgvQXxFt1qK7ML%2BCXHrOWXJ4j0yw3Ivoc4bTXXpjzcjWK%2FZYGHXG4Cw2NooqF2DJ4c7%2BNlK1rtiuar5v0DA1g32keEsgZ6RNyrC9488maYPX%2Bc1nHr56A%3D%3D&questionId=q-ledger-agent-server&path=%2Frates"

EXPORT_URL = "https://exam.sanand.workers.dev/questionData?email=23f3000609%40ds.study.iitm.ac.in&quizSign=JcOxfttdcmr8%2BnijockJLo2Ovxe%2BtMiUf2%2FVufz%2B5kw5FFuYeMG8HjzQWxe2Jow1vHIfl8JaVHDPABATzTMhgFLFJnO08FTK2i70746JNS2kpvHp6qDGv8wfdd%2BudUwTbym8yL1yc2MkukRkumh02EKUajMUeQnn8jzBHs5yNjHyH2Xgg7LyWI8dOeWB6mvOVXy7ch3TwTAhIJB1RPVM59pRJOpuvVTPCNgvQXxFt1qK7ML%2BCXHrOWXJ4j0yw3Ivoc4bTXXpjzcjWK%2FZYGHXG4Cw2NooqF2DJ4c7%2BNlK1rtiuar5v0DA1g32keEsgZ6RNyrC9488maYPX%2Bc1nHr56A%3D%3D&questionId=q-ledger-agent-server&path=%2Fexport"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    )
}

TIMEZONE = "Asia/Kolkata"

db_conn = sqlite3.connect(
    ":memory:",
    check_same_thread=False,
    timeout=30
)

db_lock = threading.Lock()
db_ready = False


# ============================================================
# 2. DATABASE INITIALIZATION
# ============================================================

def init_database():
    global db_ready

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:

        # Fetch currency conversion rates
        print("Fetching currency rates from /rates...")

        rates_response = client.get(
            RATES_URL,
            headers=HEADERS
        )
        rates_response.raise_for_status()

        rates_data = rates_response.json()
        usd_rates = rates_data["usd_per_unit"]

        # Normalize currency codes
        usd_rates = {
            str(currency).upper(): float(rate)
            for currency, rate in usd_rates.items()
        }

        if "USD" not in usd_rates:
            raise ValueError("USD conversion rate is missing.")

        print("Currency rates loaded:", usd_rates)

        # Fetch the raw order ledger
        print("Fetching raw order ledger from /export...")

        export_response = client.get(
            EXPORT_URL,
            headers=HEADERS
        )
        export_response.raise_for_status()

        content = export_response.text.strip()

        if not content:
            raise ValueError("The export endpoint returned no data.")

        # Support newline-delimited JSON and JSON arrays
        try:
            parsed = json.loads(content)

            if isinstance(parsed, list):
                rows = parsed
            elif isinstance(parsed, dict):
                if isinstance(parsed.get("data"), list):
                    rows = parsed["data"]
                elif isinstance(parsed.get("orders"), list):
                    rows = parsed["orders"]
                else:
                    rows = [parsed]
            else:
                raise ValueError("Unexpected export JSON format.")

        except json.JSONDecodeError:
            rows = [
                json.loads(line)
                for line in content.splitlines()
                if line.strip()
            ]

        print(f"Total raw records downloaded: {len(rows)}")

    if not rows:
        raise ValueError("No order records were downloaded.")

    df = pd.DataFrame(rows)

    required_columns = {
        "id",
        "created_at",
        "updated_at",
        "currency",
        "amount",
        "qty",
        "status",
        "region",
        "product",
        "customer",
    }

    missing = required_columns - set(df.columns)

    if missing:
        raise ValueError(
            f"Export data is missing required columns: {sorted(missing)}"
        )

    # Parse timestamps as timezone-aware UTC
    df["created_at_utc"] = pd.to_datetime(
        df["created_at"],
        utc=True,
        errors="coerce"
    )

    df["updated_at_utc"] = pd.to_datetime(
        df["updated_at"],
        utc=True,
        errors="coerce"
    )

    if df["created_at_utc"].isna().any():
        raise ValueError("Some created_at timestamps are invalid.")

    if df["updated_at_utc"].isna().any():
        raise ValueError("Some updated_at timestamps are invalid.")

    # Sort by update time and keep the latest record per order ID
    df = (
        df.sort_values("updated_at_utc", kind="mergesort")
        .drop_duplicates(subset=["id"], keep="last")
        .copy()
    )

    print(f"Total verified active orders: {len(df)}")

    # Convert business dates to Indian Standard Time
    ist_created = df["created_at_utc"].dt.tz_convert(TIMEZONE)

    df["created_date"] = ist_created.dt.strftime("%Y-%m-%d")
    df["created_month"] = ist_created.dt.strftime("%Y-%m")
    df["created_year"] = ist_created.dt.year

    # Standardize monetary values into USD
    df["currency"] = df["currency"].astype(str).str.upper()
    df["amount"] = pd.to_numeric(df["amount"], errors="raise")
    df["qty"] = pd.to_numeric(df["qty"], errors="raise")

    df["rate_to_usd"] = df["currency"].map(usd_rates)

    if df["rate_to_usd"].isna().any():
        currencies = (
            df.loc[df["rate_to_usd"].isna(), "currency"]
            .unique()
            .tolist()
        )
        raise ValueError(
            f"Missing USD conversion rates for currencies: {currencies}"
        )

    df["amount_usd"] = df["amount"] * df["rate_to_usd"]

    # Remove temporary timestamp columns before storing
    df_sql = df.drop(
        columns=["created_at_utc", "updated_at_utc"]
    )

    # Create the in-memory SQLite orders table
    with db_lock:
        df_sql.to_sql(
            "orders",
            db_conn,
            index=False,
            if_exists="replace"
        )

    db_ready = True
    print("In-memory SQLite database ready.")


# ============================================================
# 3. QUESTION PARSING CONFIGURATION
# ============================================================

MONTH_MAP = {
    "january": "01",
    "february": "02",
    "march": "03",
    "april": "04",
    "may": "05",
    "june": "06",
    "july": "07",
    "august": "08",
    "september": "09",
    "october": "10",
    "november": "11",
    "december": "12",
}

PRODUCTS = [
    "Air Fryer",
    "Rice Cooker",
    "Juicer",
    "Toaster",
    "Mixer",
    "Kettle",
    "Blender",
    "Grinder",
]

REGIONS = [
    "North",
    "South",
    "East",
    "West",
    "Central",
]


# ============================================================
# 4. NATURAL-LANGUAGE QUESTION TO SQL
# ============================================================

def parse_question_to_sql(question: str):
    q_low = question.lower().strip()
    conditions = []

    # Status filter
    if re.search(r"\brefund(?:ed|s)?\b", q_low):
        conditions.append("LOWER(status) = 'refunded'")

    elif re.search(
        r"\b(void|voided|cancel|cancelled|canceled)\b",
        q_low
    ):
        conditions.append("LOWER(status) = 'void'")

    else:
        conditions.append("LOWER(status) = 'paid'")

    # Region filter
    for region in REGIONS:
        if re.search(rf"\b{re.escape(region.lower())}\b", q_low):
            conditions.append(
                f"LOWER(region) = '{region.lower()}'"
            )
            break

    # Product filter
    for product in PRODUCTS:
        pattern = rf"\b{re.escape(product.lower())}s?\b"

        if re.search(pattern, q_low):
            conditions.append(
                f"LOWER(product) = '{product.lower()}'"
            )
            break

    # Customer ID filter, for example C1234
    customer_match = re.search(
        r"\b(c\d{4})\b",
        q_low
    )

    if customer_match:
        customer_id = customer_match.group(1).upper()
        conditions.append(f"customer = '{customer_id}'")

    # Year filter
    year_match = re.search(r"\b(202\d)\b", q_low)
    year_str = year_match.group(1) if year_match else "2026"

    # Month filter
    month_num = None

    for month_name, month_value in MONTH_MAP.items():
        if re.search(rf"\b{month_name}\b", q_low):
            month_num = month_value
            break

    if month_num:
        conditions.append(
            f"created_month = '{year_str}-{month_num}'"
        )

    elif year_match and "month" not in q_low:
        conditions.append(
            f"created_year = {int(year_str)}"
        )

    where_clause = " AND ".join(conditions) or "1=1"

    # --------------------------------------------------------
    # Determine the question's intent
    # --------------------------------------------------------

    # Best-selling product
    if any(
        phrase in q_low
        for phrase in [
            "which product",
            "what product",
            "top-selling product",
            "top selling product",
            "best-selling product",
            "best selling product",
            "product earned the most",
        ]
    ):
        metric = (
            "SUM(qty)"
            if any(
                word in q_low
                for word in ["qty", "quantity", "units"]
            )
            else "SUM(amount_usd)"
        )

        sql = f"""
            SELECT product
            FROM orders
            WHERE {where_clause}
            GROUP BY product
            ORDER BY {metric} DESC, product ASC
            LIMIT 1
        """

        return sql, "string"

    # Top customer
    if any(
        phrase in q_low
        for phrase in [
            "which customer",
            "what customer",
            "top customer",
            "customer spent the most",
            "who spent the most",
        ]
    ):
        sql = f"""
            SELECT customer
            FROM orders
            WHERE {where_clause}
            GROUP BY customer
            ORDER BY SUM(amount_usd) DESC, customer ASC
            LIMIT 1
        """

        return sql, "string"

    # Top region
    if any(
        phrase in q_low
        for phrase in [
            "which region",
            "what region",
            "top region",
            "region earned the most",
            "region generated the most",
        ]
    ):
        sql = f"""
            SELECT region
            FROM orders
            WHERE {where_clause}
            GROUP BY region
            ORDER BY SUM(amount_usd) DESC, region ASC
            LIMIT 1
        """

        return sql, "string"

    # Number of distinct customers
    if any(
        phrase in q_low
        for phrase in [
            "distinct customer",
            "different customer",
            "unique customer",
            "how many customer",
            "number of customers",
            "count of customers",
        ]
    ):
        sql = f"""
            SELECT COUNT(DISTINCT customer)
            FROM orders
            WHERE {where_clause}
        """

        return sql, "int"

    # Average order value
    if any(
        word in q_low
        for word in [
            "average",
            "avg",
            "mean",
        ]
    ):
        sql = f"""
            SELECT ROUND(COALESCE(AVG(amount_usd), 0), 2)
            FROM orders
            WHERE {where_clause}
        """

        return sql, "money"

    # Total quantity / units
    if any(
        phrase in q_low
        for phrase in [
            "how many units",
            "total quantity",
            "total units",
            "quantity sold",
            "units sold",
        ]
    ):
        sql = f"""
            SELECT COALESCE(SUM(qty), 0)
            FROM orders
            WHERE {where_clause}
        """

        return sql, "int"

    # Count orders
    if any(
        phrase in q_low
        for phrase in [
            "how many orders",
            "count the orders",
            "count orders",
            "number of orders",
            "how many refunded",
            "count of orders",
            "order count",
        ]
    ):
        sql = f"""
            SELECT COUNT(*)
            FROM orders
            WHERE {where_clause}
        """

        return sql, "int"

    # Default: total revenue in USD
    sql = f"""
        SELECT ROUND(COALESCE(SUM(amount_usd), 0), 2)
        FROM orders
        WHERE {where_clause}
    """

    return sql, "money"


# ============================================================
# 5. FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title="Ledger Agent API",
    description="Natural-language questions over a currency-normalized order ledger.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup_event():
    init_database()


# Health-check endpoint
@app.get("/")
def health_check():
    return {
        "status": "ok" if db_ready else "initializing",
        "database_ready": db_ready,
    }


# Answer questions using POST /
@app.post("/")
async def answer_question(request: Request):
    if not db_ready:
        raise HTTPException(
            status_code=503,
            detail="Order database is not ready."
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Request body must contain valid JSON."
        )

    question = body.get("question", "")

    if not isinstance(question, str) or not question.strip():
        raise HTTPException(
            status_code=400,
            detail="Provide a non-empty question."
        )

    print(f"\n[INCOMING QUESTION] {question}")

    try:
        sql_query, answer_type = parse_question_to_sql(question)

        print(f"[SQL EXECUTED] {sql_query.strip()}")

        # Protect the shared SQLite connection during concurrent requests
        with db_lock:
            cursor = db_conn.cursor()
            try:
                cursor.execute(sql_query)
                row = cursor.fetchone()
            finally:
                cursor.close()

        value = row[0] if row and row[0] is not None else 0

        if answer_type == "string":
            result = str(value)

        elif answer_type == "int":
            result = int(value)

        else:
            result = round(float(value), 2)

        print(f"[RESPONSE] {result}")

        return {"answer": result}

    except HTTPException:
        raise

    except Exception as exc:
        print(f"[ERROR] {exc}")

        raise HTTPException(
            status_code=500,
            detail="Unable to process the question."
        )
