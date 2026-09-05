#!/usr/bin/env python3
"""Cash dashboard backend — serves monthly data from a published Google Sheets CSV."""

from __future__ import annotations

import csv
import io
import os
import time
from datetime import date, datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

DEFAULT_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vTSh-PJ8uff324g2SKnIxq4UnXCWVJjl-wer-CDciNP3xXHTFCdX_FIumP5NA2I8XpWqRXL2gHoMKTq/"
    "pub?gid=2116695106&single=true&output=csv"
)
CACHE_TTL_SECONDS = 300

IT_MONTHS = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}

app = FastAPI(title="Cash Dashboard")

_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}


def parse_amount(raw: str) -> float:
    """Parse Italian euro amounts like '€ 2.648' or bare '9849'."""
    if raw is None:
        return 0.0
    text = str(raw).strip()
    if not text:
        return 0.0
    text = text.replace("€", "").replace("\xa0", "").replace(" ", "")
    # Italian thousands separator is '.'; no decimal cents in this sheet
    text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_month_label(label: str) -> tuple[int, int] | None:
    """Parse 'maggio 2022' → (2022, 5)."""
    parts = label.strip().lower().split()
    if len(parts) != 2:
        return None
    month = IT_MONTHS.get(parts[0])
    try:
        year = int(parts[1])
    except ValueError:
        return None
    if month is None:
        return None
    return year, month


def month_key(label: str) -> tuple[int, int]:
    parsed = parse_month_label(label)
    return parsed if parsed else (0, 0)


def snapshot(months: list[str], income: list[float], expenses: list[float],
             liquidity: list[float], securities: list[float], index: int) -> dict[str, Any]:
    return {
        "month": months[index],
        "income": income[index],
        "expenses": expenses[index],
        "liquidity": liquidity[index],
        "securities": securities[index],
        "balance": income[index] - expenses[index],
        "wealth": liquidity[index] + securities[index],
    }


def parse_csv(content: str, today: date | None = None) -> dict[str, Any]:
    reader = csv.DictReader(io.StringIO(content))
    months: list[str] = []
    income: list[float] = []
    expenses: list[float] = []
    liquidity: list[float] = []
    securities: list[float] = []

    for row in reader:
        mese = (row.get("Mese") or "").strip()
        if not mese:
            continue
        months.append(mese)
        income.append(parse_amount(row.get("Totale entrate", "")))
        expenses.append(parse_amount(row.get("Totale uscite", "")))
        liquidity.append(parse_amount(row.get("Totale liquidità", "")))
        securities.append(parse_amount(row.get("Titoli", "")))

    if not months:
        raise ValueError("No monthly rows found in spreadsheet")

    today = today or date.today()
    current = (today.year, today.month)

    # Months up to and including the current calendar month = actual;
    # later rows in the same columns = future estimates from the sheet.
    is_forecast: list[bool] = []
    forecast_start: int | None = None
    for i, label in enumerate(months):
        key = month_key(label)
        future = key > current
        is_forecast.append(future)
        if future and forecast_start is None:
            forecast_start = i

    actual_end = (forecast_start - 1) if forecast_start is not None else (len(months) - 1)
    if actual_end < 0:
        actual_end = 0

    latest = snapshot(months, income, expenses, liquidity, securities, actual_end)

    forecast_latest = None
    if forecast_start is not None:
        forecast_latest = snapshot(
            months, income, expenses, liquidity, securities, len(months) - 1
        )

    return {
        "months": months,
        "income": income,
        "expenses": expenses,
        "liquidity": liquidity,
        "securities": securities,
        "is_forecast": is_forecast,
        "forecast_start": forecast_start,
        "latest": latest,
        "forecast_latest": forecast_latest,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def fetch_cash_data(*, force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    if (
        not force
        and _cache["data"] is not None
        and (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS
    ):
        return _cache["data"]

    url = os.environ.get("SHEETS_CSV_URL", DEFAULT_CSV_URL)
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = response.text
    except httpx.HTTPError as exc:
        if _cache["data"] is not None:
            return _cache["data"]
        raise HTTPException(status_code=502, detail=f"Failed to fetch sheet: {exc}") from exc

    try:
        data = parse_csv(content)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    _cache["data"] = data
    _cache["fetched_at"] = now
    return data


@app.get("/")
async def get_dashboard():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/cash")
async def get_cash(refresh: bool = False):
    return await fetch_cash_data(force=refresh)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
