#!/usr/bin/env python3
"""Build data/intel.json for the markets page.

Keys are read from the environment and are never written to disk:
  ALPHAVANTAGE_API_KEY, ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY

Alpha Vantage's free tier allows 25 requests a day and 5 a minute. This script
stops at 20 calls, waits between them, and on a rate-limit Note/Information
response leaves yesterday's slice in place.

The published demo key only answers the documented example queries. Passing
ALPHAVANTAGE_API_KEY=demo (or --sample) builds a labelled sample from those
queries and does not call the watchlist, topics, or Alpaca.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TRANSCRIPTS = DATA / "transcripts"
INTEL_PATH = DATA / "intel.json"

WATCH = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM"]
TOPICS = ["financial_markets", "economy_macro", "technology"]
MAX_CALLS = 20
DELAY_S = 12.5
RECENT_DAYS = 50
UA = "dashboards-market-intel"

AV_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
ALPACA_ID = os.environ.get("ALPACA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET_KEY", "").strip()
SAMPLE = "--sample" in sys.argv or AV_KEY == "demo"

INSIDER_CODE = {
    "P": "Purchase",
    "S": "Sale",
    "A": "Award",
    "M": "Exercise",
    "F": "Tax",
    "G": "Gift",
    "I": "Plan",
    "J": "Other",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def label_for(score: float) -> str:
    # Alpha Vantage sentiment_score_definition (hyphenated, as the feed returns).
    if score <= -0.35:
        return "Bearish"
    if score <= -0.15:
        return "Somewhat-Bearish"
    if score < 0.15:
        return "Neutral"
    if score < 0.35:
        return "Somewhat-Bullish"
    return "Bullish"


def london_stamp(moment: datetime) -> str:
    local = moment.astimezone(ZoneInfo("Europe/London"))
    return local.strftime("%-d %b %Y, %H:%M ") + local.tzname()


def trim(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if dot >= int(limit * 0.55):
        return cut[: dot + 1]
    return cut.rsplit(" ", 1)[0] + "…"


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text or "").strip())
    return [p.strip() for p in parts if len(p.split()) >= 6]


def nice_name(value: str) -> str:
    value = (value or "").strip()
    if "," in value:
        last, first = [p.strip() for p in value.split(",", 1)]
        return f"{first.title()} {last.title()}".strip()
    return value


def compact_shares(value) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000:
        return f"{sign}{n / 1_000_000_000:.1f}bn"
    if n >= 1_000_000:
        return f"{sign}{n / 1_000_000:.1f}m"
    if n >= 10_000:
        return f"{sign}{n / 1_000:.0f}k"
    if n.is_integer():
        return f"{sign}{int(n)}"
    return f"{sign}{n:.1f}"


def money_range(lo, hi) -> str:
    def one(raw) -> str:
        try:
            n = float(raw)
        except (TypeError, ValueError):
            return ""
        if n >= 1_000_000:
            text = f"${n / 1_000_000:.1f}m"
            return text.replace(".0m", "m")
        if n >= 1_000:
            return f"${n / 1_000:.0f}k"
        return f"${n:.0f}"

    a, b = one(lo), one(hi)
    if a and b and a != b:
        return f"{a}–{b}"
    return a or b


def is_rate_limit(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    note = str(payload.get("Note") or "")
    info = str(payload.get("Information") or "")
    # A payload that also contains a feed or data is a real response.
    if any(k in payload for k in ("feed", "data", "trades", "holdings", "transcript", "top_gainers", "quarterlyEarnings")):
        return False
    blob = f"{note} {info}".lower()
    if "demo purposes" in blob:
        return False
    if not note and not info:
        return False
    if note:
        return True
    return any(
        phrase in blob
        for phrase in ("rate limit", "call frequency", "requests per day", "calls per minute", "api call volume", "spread out")
    )


class Budget:
    def __init__(self) -> None:
        self.calls = 0
        self.halted = False
        self.notes: list[str] = []

    def av(self, function: str, **params):
        if self.halted:
            return None, "halted"
        if self.calls >= MAX_CALLS:
            self.notes.append(f"Stopped at {MAX_CALLS} Alpha Vantage calls before {function}.")
            self.halted = True
            return None, "budget"
        if self.calls:
            time.sleep(DELAY_S)
        self.calls += 1
        shown = " ".join(f"{k}={v}" for k, v in params.items())
        log(f"Alpha Vantage {self.calls}/{MAX_CALLS} {function} {shown}")
        # apikey goes last. The public demo key only answers the documented example URLs, which put it last. A real key ignores order.
        query = {"function": function, **params, "apikey": AV_KEY}
        url = "https://www.alphavantage.co/query?" + urllib.parse.urlencode(query)
        payload, err = http_json(url, timeout=60)
        if err:
            self.notes.append(f"{function} failed ({err}).")
            return None, err
        if is_rate_limit(payload):
            self.halted = True
            self.notes.append(f"Alpha Vantage rate limit on {function}. Kept the previous data for anything not yet refreshed.")
            log("Rate limit. Later Alpha Vantage calls skipped.")
            return None, "rate-limit"
        data_keys = ("feed", "data", "trades", "holdings", "transcript", "top_gainers", "quarterlyEarnings")
        if isinstance(payload, dict) and any(k in payload for k in ("Information", "Note", "Error Message")) and not any(k in payload for k in data_keys):
            self.notes.append(f"{function} returned no rows.")
            log(f"{function} returned no rows.")
            return None, "empty"
        return payload, None


def http_json(url: str, headers: dict | None = None, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        exc.read()
        return None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - surface the class, never the URL
        return None, type(exc).__name__
    try:
        return json.loads(body.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "not-json"


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def av_time(value: str | None) -> str | None:
    if not value or len(value) < 15 or "T" not in value:
        return None
    day, clock = value.split("T", 1)
    if len(day) != 8 or len(clock) < 6:
        return None
    return f"{day[0:4]}-{day[4:6]}-{day[6:8]}T{clock[0:2]}:{clock[2:4]}:{clock[4:6]}Z"


def news_from_av(item: dict, sample: bool) -> dict | None:
    title = (item.get("title") or "").strip()
    url = (item.get("url") or "").strip()
    if not title:
        return None
    symbols = []
    for row in item.get("ticker_sentiment") or []:
        ticker = (row.get("ticker") or "").upper()
        if ticker and ":" not in ticker and ticker not in symbols:
            symbols.append(ticker)
        if len(symbols) == 4:
            break
    try:
        score = round(float(item.get("overall_sentiment_score")), 4)
    except (TypeError, ValueError):
        score = None
    label = item.get("overall_sentiment_label") or (label_for(score) if score is not None else None)
    if isinstance(label, str):
        label = label.replace("Somewhat_Bullish", "Somewhat-Bullish").replace("Somewhat_Bearish", "Somewhat-Bearish")
    digest = hashlib.sha1((url or title).encode()).hexdigest()[:12]
    return {
        "id": f"av-{digest}",
        "headline": title,
        "summary": trim(item.get("summary") or "", 320),
        "source": item.get("source") or "Alpha Vantage",
        "url": url,
        "symbols": symbols,
        "published_at": av_time(item.get("time_published")),
        "sentiment": score,
        "sentiment_label": label,
        "origin": "alphavantage",
        "sample": sample,
    }


def news_from_alpaca(item: dict) -> dict | None:
    title = (item.get("headline") or "").strip()
    url = (item.get("url") or "").strip()
    if not title:
        return None
    symbols = [s for s in (item.get("symbols") or []) if isinstance(s, str)][:4]
    source = item.get("source") or "Alpaca"
    if isinstance(source, str) and source.lower() == "benzinga":
        source = "Benzinga"
    return {
        "id": f"alpaca-{item.get('id')}",
        "headline": title,
        "summary": trim(item.get("summary") or "", 320),
        "source": source,
        "url": url,
        "symbols": symbols,
        "published_at": item.get("created_at"),
        "sentiment": None,
        "sentiment_label": None,
        "origin": "alpaca",
        "sample": False,
    }


def dedupe_news(items: list[dict], limit: int) -> list[dict]:
    seen = set()
    out = []
    for item in sorted(items, key=lambda row: row.get("published_at") or "", reverse=True):
        key = (item.get("url") or item["headline"]).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def mood_from(items: list[dict]) -> dict | None:
    scores = [row["sentiment"] for row in items if isinstance(row.get("sentiment"), (int, float))]
    if not scores:
        return None
    score = round(sum(scores) / len(scores), 4)
    return {
        "score": score,
        "label": label_for(score),
        "article_count": len(scores),
        "scale": "Bearish ≤ -0.35 < Somewhat-Bearish ≤ -0.15 < Neutral < 0.15 ≤ Somewhat-Bullish < 0.35 ≤ Bullish",
    }


def fetch_alpaca(notes: list[str]) -> list[dict]:
    if not ALPACA_ID or not ALPACA_SECRET:
        notes.append("Alpaca keys were not set, so headlines are Alpha Vantage only.")
        return []
    headers = {"APCA-API-KEY-ID": ALPACA_ID, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    url = "https://data.alpaca.markets/v1beta1/news?" + urllib.parse.urlencode(
        {"limit": "20", "sort": "desc", "symbols": ",".join(WATCH)}
    )
    log("Alpaca news")
    payload, err = http_json(url, headers)
    if err or not isinstance(payload, dict):
        notes.append(f"Alpaca news unavailable ({err or 'empty'}).")
        return []
    rows = []
    for item in payload.get("news") or []:
        row = news_from_alpaca(item)
        if row:
            rows.append(row)
    return rows


def ordinary_ticker(ticker: str) -> bool:
    # Warrants and when-issued symbols crowd the raw leaderboard and are not the move the panel is for.
    if not re.fullmatch(r"[A-Z]{1,5}", ticker or ""):
        return False
    return not (len(ticker) >= 4 and ticker.endswith("W"))


def movers_from(payload: dict) -> dict:
    def price_of(row: dict) -> float:
        try:
            return float(row.get("price"))
        except (TypeError, ValueError):
            return 0.0

    def rows(key: str, count: int) -> list[dict]:
        raw = [row for row in (payload.get(key) or []) if isinstance(row, dict) and row.get("ticker")]
        plain = [row for row in raw if ordinary_ticker(row["ticker"])]
        priced = [row for row in plain if price_of(row) >= 1]
        chosen = priced or plain or raw
        out = []
        for row in chosen[:count]:
            out.append(
                {
                    "ticker": row.get("ticker"),
                    "price": row.get("price"),
                    "change_amount": row.get("change_amount"),
                    "change_percentage": row.get("change_percentage"),
                }
            )
        return out

    return {
        "last_updated": payload.get("last_updated"),
        "gainers": rows("top_gainers", 5),
        "losers": rows("top_losers", 5),
        "active": rows("most_actively_traded", 4),
    }


def insider_rows(payload: dict, symbol: str) -> list[dict]:
    rows = [row for row in (payload.get("data") or []) if isinstance(row, dict)]
    preferred = [row for row in rows if row.get("acquisition_or_disposal") in ("P", "S")]
    chosen = preferred or rows
    chosen.sort(key=lambda row: row.get("transaction_date") or "", reverse=True)
    out = []
    for row in chosen[:5]:
        code = row.get("acquisition_or_disposal") or ""
        out.append(
            {
                "symbol": row.get("ticker") or symbol,
                "date": row.get("transaction_date"),
                "name": nice_name(row.get("executive") or ""),
                "title": row.get("executive_title") or "",
                "action": INSIDER_CODE.get(code, code or "Filing"),
                "code": code,
                "shares": compact_shares(row.get("shares")),
                "price": row.get("share_price"),
            }
        )
    return out


def congress_rows(payload: dict, symbol: str) -> list[dict]:
    rows = [row for row in (payload.get("trades") or []) if isinstance(row, dict)]
    rows.sort(key=lambda row: row.get("transaction_date") or "", reverse=True)
    out = []
    for row in rows[:5]:
        out.append(
            {
                "symbol": row.get("symbol") or symbol,
                "date": row.get("transaction_date"),
                "name": row.get("politician_canonical") or row.get("politician") or "",
                "party": row.get("party") or "",
                "state": row.get("state") or "",
                "chamber": (row.get("chamber") or "").title(),
                "action": (row.get("transaction_type") or "").title(),
                "amount": money_range(row.get("amount_min"), row.get("amount_max")),
            }
        )
    return out


def institutional_block(payload: dict, symbol: str) -> dict:
    holders = []
    for row in (payload.get("holdings") or [])[:4]:
        if not isinstance(row, dict):
            continue
        holders.append(
            {
                "name": (row.get("holder_name") or "").title(),
                "shares": compact_shares(row.get("shares_held")),
                "change": row.get("shares_changed_percentage") or "",
                "change_type": row.get("change_type") or "",
                "reported": row.get("last_reported") or "",
            }
        )
    return {
        "symbol": payload.get("symbol") or symbol,
        "ownership": payload.get("total_institutional_ownership_percentage") or "",
        "holders": payload.get("total_institutional_holders") or "",
        "increased": payload.get("holders_with_increased_holdings") or "",
        "decreased": payload.get("holders_with_decreased_holdings") or "",
        "top": holders,
    }


def role_of(title: str, speaker: str) -> str:
    blob = f"{title} {speaker}".lower()
    if re.search(r"\bceo\b", blob) or "chief executive" in blob:
        return "CEO"
    if re.search(r"\bcfo\b", blob) or "chief financial" in blob:
        return "CFO"
    if "operator" in blob:
        return "Operator"
    if "analyst" in blob:
        return "Analyst"
    if "investor relation" in blob:
        return "IR"
    return (title or "Speaker").strip()


def best_sentence(text: str) -> str:
    found = sentences(text)
    if not found:
        return trim(text, 280)
    return trim(max(found, key=lambda sentence: (len(re.findall(r"\d", sentence)), len(sentence.split()))), 280)


def extractive_summary(turns: list[dict]) -> dict:
    scored = []
    for turn in turns:
        content = (turn.get("content") or "").strip()
        try:
            score = float(turn.get("sentiment") or 0)
        except (TypeError, ValueError):
            score = 0.0
        scored.append(
            {
                "speaker": turn.get("speaker") or "",
                "title": turn.get("title") or "",
                "content": content,
                "score": score,
                "words": len(content.split()),
                "role": role_of(turn.get("title") or "", turn.get("speaker") or ""),
            }
        )
    if not scored:
        return {
            "method": "extractive",
            "llm": None,
            "sentiment_score": None,
            "sentiment_label": None,
            "turn_count": 0,
            "overview": "",
            "quotes": [],
            "bullish": None,
            "bearish": None,
        }
    mean = sum(turn["score"] for turn in scored) / len(scored)
    execs = [turn for turn in scored if turn["role"] in ("CEO", "CFO") and turn["words"] >= 30]
    pool = execs or [turn for turn in scored if turn["role"] not in ("Operator", "Analyst") and turn["words"] >= 30] or scored

    def weight(turn: dict) -> float:
        return abs(turn["score"]) * 1.6 + min(turn["words"], 220) / 220

    ranked = sorted(pool, key=weight, reverse=True)
    quotes = []
    seen_roles = set()
    for turn in ranked:
        if turn["role"] in seen_roles and len(quotes) < 2 and any(t["role"] != turn["role"] for t in ranked):
            continue
        quotes.append(
            {
                "role": turn["role"],
                "speaker": turn["speaker"],
                "title": turn["title"],
                "text": best_sentence(turn["content"]),
                "sentiment": round(turn["score"], 3),
                "sentiment_label": label_for(turn["score"]),
            }
        )
        seen_roles.add(turn["role"])
        if len(quotes) == 2:
            break

    opener = next((turn for turn in scored if turn["role"] == "CEO" and turn["words"] >= 40), ranked[0])
    overview = trim(" ".join(sentences(opener["content"])[:2]), 420)
    used = {quote["text"] for quote in quotes}

    def moment_fresh(turn: dict) -> dict:
        options = [trim(sentence, 280) for sentence in sentences(turn["content"])] or [trim(turn["content"], 280)]
        text = next((sentence for sentence in options if sentence not in used), options[0])
        used.add(text)
        return {
            "speaker": turn["speaker"],
            "title": turn["title"],
            "role": turn["role"],
            "text": text,
            "sentiment": round(turn["score"], 3),
            "sentiment_label": label_for(turn["score"]),
        }

    bullish_pool = execs or pool
    bull = max(bullish_pool, key=lambda turn: (turn["score"], turn["words"]))
    bear = min(bullish_pool, key=lambda turn: (turn["score"], -turn["words"]))
    return {
        "method": "extractive",
        "llm": None,
        "sentiment_score": round(mean, 4),
        "sentiment_label": label_for(mean),
        "turn_count": len(scored),
        "overview": overview,
        "quotes": quotes,
        "bullish": moment_fresh(bull),
        "bearish": moment_fresh(bear),
    }


def quarter_from_fiscal(fiscal: str) -> str | None:
    match = re.match(r"(\d{4})-(\d{2})-\d{2}", fiscal or "")
    if not match:
        return None
    month = int(match.group(2))
    return f"{match.group(1)}Q{(month - 1) // 3 + 1}"


def latest_earnings(payload: dict) -> dict | None:
    rows = [row for row in (payload.get("quarterlyEarnings") or []) if isinstance(row, dict) and row.get("fiscalDateEnding")]
    if not rows:
        return None
    row = max(rows, key=lambda item: item.get("fiscalDateEnding") or "")
    quarter = quarter_from_fiscal(row["fiscalDateEnding"])
    if not quarter:
        return None
    return {
        "quarter": quarter,
        "fiscal_date_ending": row.get("fiscalDateEnding"),
        "reported_date": row.get("reportedDate"),
        "reported_eps": row.get("reportedEPS"),
        "estimated_eps": row.get("estimatedEPS"),
        "surprise_percentage": row.get("surprisePercentage"),
    }


def reported_recently(reported: str | None, now: datetime) -> bool:
    if not reported:
        return False
    try:
        day = datetime.strptime(reported[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return timedelta(0) <= (now - day) <= timedelta(days=RECENT_DAYS) or (day - now) <= timedelta(days=2)


def transcript_path(symbol: str, quarter: str) -> Path:
    return TRANSCRIPTS / f"{symbol}-{quarter}.json"


def summarise_cached(record: dict, meta: dict, sample: bool) -> dict:
    summary = extractive_summary(record.get("transcript") or [])
    return {
        "symbol": record.get("symbol") or meta.get("symbol"),
        "quarter": record.get("quarter") or meta.get("quarter"),
        "reported_date": meta.get("reported_date"),
        "fiscal_date_ending": meta.get("fiscal_date_ending"),
        "reported_eps": meta.get("reported_eps"),
        "estimated_eps": meta.get("estimated_eps"),
        "surprise_percentage": meta.get("surprise_percentage"),
        "summary": summary,
        "sample": sample or bool(meta.get("sample")),
    }


def cache_transcript(symbol: str, quarter: str, payload: dict, meta: dict, sample: bool) -> dict | None:
    turns = payload.get("transcript")
    if not isinstance(turns, list) or not turns:
        return None
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    record = {
        "symbol": payload.get("symbol") or symbol,
        "quarter": payload.get("quarter") or quarter,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sample": sample,
        "reported_date": meta.get("reported_date"),
        "fiscal_date_ending": meta.get("fiscal_date_ending"),
        "reported_eps": meta.get("reported_eps"),
        "estimated_eps": meta.get("estimated_eps"),
        "surprise_percentage": meta.get("surprise_percentage"),
        "transcript": turns,
    }
    transcript_path(symbol, quarter).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return summarise_cached(record, record, sample)


def rotate(items: list[str], start: int, count: int) -> list[str]:
    return [items[(start + i) % len(items)] for i in range(count)]


def collect_news(budget: Budget, sample: bool) -> tuple[list[dict], list[float]]:
    articles = []
    scores = []

    def take(payload):
        if not isinstance(payload, dict):
            return
        for item in payload.get("feed") or []:
            row = news_from_av(item, sample)
            if not row:
                continue
            articles.append(row)
            if isinstance(row["sentiment"], float):
                scores.append(row["sentiment"])

    if sample:
        payload, _err = budget.av("NEWS_SENTIMENT", tickers="AAPL")
        take(payload)
        return articles, scores

    for topic in TOPICS:
        if budget.halted:
            break
        payload, _err = budget.av("NEWS_SENTIMENT", topics=topic, limit="30", sort="LATEST")
        take(payload)
    day = datetime.now(timezone.utc).toordinal()
    # A comma-separated ticker list means "mentioned together", so each name is its own call.
    for ticker in rotate(WATCH, (day * 3) % len(WATCH), 3):
        if budget.halted:
            break
        payload, _err = budget.av("NEWS_SENTIMENT", tickers=ticker, limit="20", sort="LATEST")
        take(payload)
    return articles, scores


def collect_earnings(budget: Budget, sample: bool, now: datetime) -> list[dict]:
    found = []
    if sample:
        cached = load_json(transcript_path("IBM", "2024Q1"))
        if cached and cached.get("transcript"):
            log("Transcript cache hit IBM 2024Q1")
            found.append(summarise_cached(cached, cached, True))
            return found
        payload, err = budget.av("EARNINGS_CALL_TRANSCRIPT", symbol="IBM", quarter="2024Q1")
        if payload:
            block = cache_transcript("IBM", "2024Q1", payload, {"sample": True}, True)
            if block:
                found.append(block)
        elif err:
            budget.notes.append("IBM 2024Q1 transcript was not available.")
        return found

    day = datetime.now(timezone.utc).toordinal()
    symbols = rotate(WATCH, (day * 2 + 1) % len(WATCH), 2)
    fetched = 0
    for symbol in symbols:
        if budget.halted:
            break
        payload, _err = budget.av("EARNINGS", symbol=symbol)
        meta = latest_earnings(payload) if isinstance(payload, dict) else None
        if not meta or not reported_recently(meta.get("reported_date"), now):
            continue
        quarter = meta["quarter"]
        cached = load_json(transcript_path(symbol, quarter))
        if cached and cached.get("transcript") and not cached.get("sample"):
            log(f"Transcript cache hit {symbol} {quarter}")
            found.append(summarise_cached(cached, {**cached, **meta}, False))
            continue
        if fetched >= 2 or budget.halted:
            continue
        transcript, _err = budget.av("EARNINGS_CALL_TRANSCRIPT", symbol=symbol, quarter=quarter)
        fetched += 1
        if not isinstance(transcript, dict):
            continue
        block = cache_transcript(symbol, quarter, transcript, meta, False)
        if block:
            found.append(block)
    return found


def collect_flows(budget: Budget, sample: bool) -> dict | None:
    if sample:
        symbols = [("IBM", "IBM"), ("AAPL", "AAPL")]
        # Insider and holdings are documented for IBM; congress for AAPL.
        plan = [
            ("insider", "INSIDER_TRANSACTIONS", "IBM"),
            ("congress", "CONGRESS_TRADES", "AAPL"),
            ("institutional", "INSTITUTIONAL_HOLDINGS", "IBM"),
        ]
    else:
        day = datetime.now(timezone.utc).toordinal()
        symbols = rotate(WATCH, (day * 2 + 1) % len(WATCH), 2)
        plan = []
        for symbol in symbols:
            plan.append(("insider", "INSIDER_TRANSACTIONS", symbol))
            plan.append(("congress", "CONGRESS_TRADES", symbol))
            plan.append(("institutional", "INSTITUTIONAL_HOLDINGS", symbol))

    insider, congress, institutional = [], [], []
    got = False
    for kind, function, symbol in plan:
        if budget.halted:
            break
        payload, _err = budget.av(function, symbol=symbol)
        if not isinstance(payload, dict):
            continue
        got = True
        if kind == "insider":
            insider.extend(insider_rows(payload, symbol))
        elif kind == "congress":
            congress.extend(congress_rows(payload, symbol))
        else:
            institutional.append(institutional_block(payload, symbol))
    if not got:
        return None
    covered = []
    for row in insider + congress + institutional:
        sym = row.get("symbol")
        if sym and sym not in covered:
            covered.append(sym)
    return {"symbols": covered, "insider": insider[:8], "congress": congress[:8], "institutional": institutional[:4]}


def merge_earnings(previous: list, fresh: list, sample: bool) -> list:
    merged = []
    if not sample:
        for row in previous or []:
            if isinstance(row, dict) and not row.get("sample"):
                merged.append(row)
    seen = {(row.get("symbol"), row.get("quarter")) for row in fresh}
    kept = [row for row in merged if (row.get("symbol"), row.get("quarter")) not in seen]
    return (fresh + kept)[:6]


def refuse_secrets(blob: str) -> None:
    for secret in (AV_KEY, ALPACA_ID, ALPACA_SECRET):
        if secret and secret != "demo" and secret in blob:
            raise SystemExit("Refusing to write intel.json because it contains an API key.")


def main() -> int:
    if not AV_KEY:
        print("ALPHAVANTAGE_API_KEY is not set.", file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc)
    previous = load_json(INTEL_PATH) or {}
    budget = Budget()
    if SAMPLE:
        budget.notes.append(
            "Sample from the Alpha Vantage demo key: AAPL headlines, IBM insider filings and the IBM 2024Q1 call, AAPL congressional trades, IBM institutional holdings. The scheduled job fills Alpaca and the rest of the watchlist."
        )
        log("Sample mode. Documented demo queries only.")

    articles, scores = collect_news(budget, SAMPLE)
    alpaca = [] if SAMPLE else fetch_alpaca(budget.notes)
    news = dedupe_news(alpaca + articles, 16)
    fresh_news = bool(news)
    if fresh_news:
        out_news = news
        out_mood = mood_from(articles) or (mood_from(news) if not SAMPLE else None)
        if out_mood is None and scores:
            score = round(sum(scores) / len(scores), 4)
            out_mood = {
                "score": score,
                "label": label_for(score),
                "article_count": len(scores),
                "scale": "Bearish ≤ -0.35 < Somewhat-Bearish ≤ -0.15 < Neutral < 0.15 ≤ Somewhat-Bullish < 0.35 ≤ Bullish",
            }
    else:
        out_news = previous.get("news") or []
        out_mood = previous.get("mood")
        if not budget.halted:
            budget.notes.append("No fresh headlines. Kept the previous news list.")

    movers_payload = None
    if not budget.halted:
        movers_payload, _err = budget.av("TOP_GAINERS_LOSERS")
    if isinstance(movers_payload, dict) and movers_payload.get("top_gainers"):
        out_movers = movers_from(movers_payload)
    elif previous.get("sample") and not SAMPLE:
        out_movers = None
    else:
        out_movers = previous.get("movers")
    if not (isinstance(out_movers, dict) and out_movers.get("gainers")) and not budget.halted:
        budget.notes.append("Top movers were not returned.")

    fresh_earnings = [] if budget.halted else collect_earnings(budget, SAMPLE, now)
    if SAMPLE:
        out_earnings = fresh_earnings or [row for row in (previous.get("earnings") or []) if row.get("sample")]
    else:
        prior_earnings = [] if previous.get("sample") else (previous.get("earnings") or [])
        out_earnings = merge_earnings(prior_earnings, fresh_earnings, False)

    flows = None if budget.halted else collect_flows(budget, SAMPLE)
    if flows:
        out_flows = flows
    elif SAMPLE or (previous.get("sample") and not fresh_news):
        out_flows = previous.get("flows")
    elif previous.get("sample"):
        out_flows = None
    else:
        out_flows = previous.get("flows")

    carried_sample = bool(previous.get("sample")) and not fresh_news and not SAMPLE
    document = {
        "schema": 1,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "updated_london": london_stamp(now),
        "timezone": "Europe/London",
        "sample": bool(SAMPLE or carried_sample),
        "stale": bool(budget.halted and not fresh_news),
        "alphavantage_calls": budget.calls,
        "notes": budget.notes,
        "watchlist": WATCH,
        "mood": out_mood,
        "news": out_news,
        "movers": out_movers,
        "earnings": out_earnings,
        "flows": out_flows,
    }
    blob = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    refuse_secrets(blob)
    DATA.mkdir(parents=True, exist_ok=True)
    INTEL_PATH.write_text(blob, encoding="utf-8")
    log(
        f"Wrote {INTEL_PATH.relative_to(ROOT)} "
        f"calls={budget.calls} news={len(out_news)} earnings={len(out_earnings or [])} sample={document['sample']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
