"""Native, model-independent stock quote receipts shared by chat and schedules."""

import math
import re


def stock_quote(query):
    from web_search_mcp import _stock_request, google_stock_quote
    symbol, _ = _stock_request(str(query or "")[:500])
    if not symbol:
        return {"error": "quote_invalid_ticker"}
    try:
        value = google_stock_quote(str(query or "")[:500])
    except Exception:
        return {"symbol": symbol, "error": "quote_unavailable"}
    price = value.get("price") if isinstance(value, dict) else None
    valid = (
        isinstance(value, dict) and not value.get("error") and value.get("symbol") == symbol
        and isinstance(price, (int, float)) and not isinstance(price, bool)
        and math.isfinite(price) and price > 0
        and bool(re.fullmatch(r"[A-Z]{3}", str(value.get("currency") or "")))
        and bool(str(value.get("source") or "").strip())
        and bool(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", str(value.get("retrieved_at") or "")))
    )
    if not valid:
        return {"symbol": symbol, "error": "quote_unavailable"}
    quote = {key: value[key] for key in ("symbol", "exchange", "currency", "price", "change", "change_percent", "volume", "source", "source_url", "retrieved_at") if key in value}
    quote["source"] = " ".join(str(quote["source"]).split())[:80].replace("[[", "[ [").replace("]]", "] ]")
    for key in ("change", "change_percent", "volume"):
        number = quote.get(key)
        if not isinstance(number, (int, float)) or isinstance(number, bool) or not math.isfinite(number):
            quote.pop(key, None)
    return quote
