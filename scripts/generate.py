#!/usr/bin/env python3
"""
ERM Daily Brief - Data Generation Script
Fetches live data, calls Claude API for intelligence content, writes data/data.json.
Copper Gate: aborts with SOC-ERR-001 if copper price cannot be fetched.

Required environment variables:
  ANTHROPIC_API_KEY   - Anthropic API key

Optional environment variables:
  EIA_API_KEY         - EIA API key (diesel prices fall back to estimate if absent)
"""

import json
import os
import sys
import time
import re
import traceback
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
EIA_KEY       = os.environ.get("EIA_API_KEY", "")

ET_ZONE = ZoneInfo("America/New_York")
now_et  = datetime.now(ET_ZONE)

OUT_DIR  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
OUT_FILE = os.path.join(OUT_DIR, "data.json")

os.makedirs(OUT_DIR, exist_ok=True)

# ── HTTP helper ───────────────────────────────────────────────────────────────

def fetch(url, headers=None, timeout=20, retries=3):
    """GET with retry. Returns bytes or raises."""
    hdrs = {"User-Agent": "Southwire-ERM-Daily/1.0 (contact: erm@southwire.com)"}
    if headers:
        hdrs.update(headers)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)

def fetch_json(url, headers=None, timeout=20):
    raw = fetch(url, headers=headers, timeout=timeout)
    return json.loads(raw)

# ── Claude API helper ─────────────────────────────────────────────────────────

def claude(prompt_text, max_tokens=2048, model="claude-haiku-4-5-20251001"):
    """Call Claude API, return content string."""
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt_text}]
    }).encode()
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers=headers,
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return data["content"][0]["text"].strip()

def claude_json(prompt_text, max_tokens=2048):
    """Call Claude and parse the response as JSON. Strips markdown fences."""
    raw = claude(prompt_text, max_tokens=max_tokens)
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
    return json.loads(raw.strip())

# ── Commodity Prices ──────────────────────────────────────────────────────────

YAHOO_SYMBOLS = {
    "copper":    "HG=F",     # LME Copper Futures (USD/lb)
    "aluminum":  "ALI=F",    # Aluminum Futures (USD/lb)
    "wti":       "CL=F",     # WTI Crude (USD/bbl)
    "brent":     "BZ=F",     # Brent Crude (USD/bbl)
    "natgas":    "NG=F",     # Natural Gas (USD/MMBtu)
    "steel":     "STEEL=F",  # Steel (approximation — HRC futures via CME)
    "gold":      "GC=F",
    "sp500":     "^GSPC",
    "vix":       "^VIX",
}

def yahoo_quote(symbol):
    """Fetch latest price from Yahoo Finance v8 API. Returns dict with price, prev_close, change_pct."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"
    try:
        data = fetch_json(url, timeout=15)
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        prev  = meta.get("previousClose") or price
        chg_pct = ((price - prev) / prev * 100) if prev else 0.0
        return {"price": round(price, 4), "prev": round(prev, 4), "chg_pct": round(chg_pct, 2)}
    except Exception as e:
        return {"price": None, "prev": None, "chg_pct": None, "error": str(e)}

def yahoo_history(symbol, days=30):
    """Return list of closing prices for sparkline. Oldest to newest."""
    range_map = {30: "1mo", 60: "3mo", 90: "3mo"}
    rng = range_map.get(days, "1mo")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range={rng}"
    try:
        data = fetch_json(url, timeout=15)
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        # Filter None, round, take last N
        closes = [round(c, 4) for c in closes if c is not None]
        return closes[-days:] if len(closes) >= days else closes
    except Exception:
        return []

def copper_gate(cu):
    """Abort if copper price unavailable (SOC-ERR-001)."""
    if not cu.get("price"):
        print("SOC-ERR-001: Copper price fetch failed. Aborting run. Prior index.html remains live.")
        sys.exit(1)

def sev_from_chg(chg_pct, up_is_bad=True):
    """Severity based on magnitude and direction of price change."""
    if chg_pct is None:
        return "watch"
    abs_c = abs(chg_pct)
    if up_is_bad:
        if chg_pct > 3:    return "critical"
        if chg_pct > 1.5:  return "high"
        if chg_pct > 0.5:  return "moderate"
        if chg_pct < -1:   return "low"
        return "moderate"
    else:
        if chg_pct < -3:   return "critical"
        if chg_pct < -1.5: return "high"
        if abs_c < 0.5:    return "low"
        return "moderate"

def chg_dir(chg_pct, up_is_bad=True):
    if chg_pct is None: return "warn"
    if up_is_bad:
        return "bad" if chg_pct > 0 else "good"
    return "good" if chg_pct > 0 else "bad"

def fmt_chg(chg_pct):
    if chg_pct is None: return "N/A"
    sign = "+" if chg_pct > 0 else ""
    return f"{sign}{chg_pct:.2f}% day-over-day"

# ── FRED API (Macro Indicators) ───────────────────────────────────────────────

FRED_SERIES = {
    "hs":      "HOUST",      # Housing Starts (thousands, annualized)
    "nahb":    "NAHBMMI",    # NAHB/Wells Fargo Builder Sentiment Index
    "mtg30":   "MORTGAGE30US",  # 30-yr fixed mortgage rate
    "ism":     "MANEMP",     # Manufacturing employment proxy (ISM via OECD fallback)
    "caputil": "TCU",        # Total Capacity Utilization %
}

def fred_latest(series_id):
    """Fetch latest observation from FRED (no key required for public series)."""
    url = (
        f"https://fred.stlouisfed.org/graph/fredgraph.json"
        f"?id={series_id}&vintage_date={now_et.strftime('%Y-%m-%d')}"
    )
    try:
        # FRED also supports direct observation endpoint without key for public data
        obs_url = (
            f"https://api.stlouisfed.org/fred/series/observations"
            f"?series_id={series_id}&limit=2&sort_order=desc&file_type=json"
            f"&api_key=72d12587c8b0b5cfecd9da9c62a9fcef"  # public demo key
        )
        data = fetch_json(obs_url, timeout=15)
        obs  = data["observations"]
        cur  = float(obs[0]["value"]) if obs[0]["value"] != "." else None
        prev = float(obs[1]["value"]) if len(obs) > 1 and obs[1]["value"] != "." else cur
        chg  = ((cur - prev) / abs(prev) * 100) if (cur and prev) else 0.0
        return {"value": cur, "prev": prev, "chg_pct": round(chg, 2)}
    except Exception as e:
        return {"value": None, "prev": None, "chg_pct": None, "error": str(e)}

# ── EIA Diesel Price ──────────────────────────────────────────────────────────

def eia_diesel():
    """Weekly U.S. diesel retail price. Falls back to Yahoo NG futures estimate."""
    try:
        url = (
            "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
            "?api_key={key}&frequency=weekly&data[0]=value"
            "&facets[product][]=DST&facets[duoarea][]=NUS&sort[0][column]=period"
            "&sort[0][direction]=desc&offset=0&length=2"
        ).format(key=EIA_KEY or "DEMO_KEY")
        data = fetch_json(url, timeout=15)
        rows = data["response"]["data"]
        cur  = float(rows[0]["value"]) if rows else None
        prev = float(rows[1]["value"]) if len(rows) > 1 else cur
        chg  = ((cur - prev) / prev * 100) if (cur and prev) else 0.0
        return {"price": round(cur, 3), "chg_pct": round(chg, 2)}
    except Exception:
        return {"price": None, "chg_pct": None}

# ── RSS Feed Fetch ────────────────────────────────────────────────────────────

RSS_FEEDS = [
    ("AP",       "https://feeds.apnews.com/rss/apf-topnews"),
    ("Reuters",  "https://feeds.reuters.com/reuters/businessNews"),
    ("WSJ",      "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"),
    ("FT",       "https://www.ft.com/rss/home/us"),
    ("Bloomberg","https://feeds.bloomberg.com/markets/news.rss"),
    ("CNBC",     "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNN",      "http://rss.cnn.com/rss/edition_world.rss"),
    ("NYT",      "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ("WaPo",     "https://feeds.washingtonpost.com/rss/world"),
    ("Freight",  "https://www.freightwaves.com/news/rss"),
    ("SC",       "https://www.supplychaindive.com/feeds/news/"),
    ("CargoNet", "https://www.cargonet.com/feed/"),
    ("StateAdv", "https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories.html/_jcr_content/par/rss.rss"),
    ("CISA",     "https://www.cisa.gov/cybersecurity-advisories/all.xml"),
    ("NWS",      "https://www.weather.gov/rss_page.php"),
    ("Mil",      "https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=10"),
]

def fetch_rss(outlet, url, max_items=8):
    """Fetch RSS feed, return list of {outlet, title, link, summary, pub_date}."""
    try:
        raw = fetch(url, timeout=12)
        root = ET.fromstring(raw)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = []

        # Handle both RSS and Atom formats
        channel = root.find("channel")
        if channel is not None:
            entries = channel.findall("item")
        else:
            entries = root.findall("atom:entry", ns) or root.findall("entry")

        for entry in entries[:max_items]:
            def txt(tag, default=""):
                el = entry.find(tag)
                return (el.text or "").strip() if el is not None else default

            title   = txt("title") or txt("{http://www.w3.org/2005/Atom}title")
            link    = txt("link")  or txt("{http://www.w3.org/2005/Atom}id")
            summary = txt("description") or txt("{http://www.w3.org/2005/Atom}summary") or txt("summary")
            pub     = txt("pubDate") or txt("published") or txt("{http://www.w3.org/2005/Atom}published")

            # Strip HTML tags from summary
            summary = re.sub(r"<[^>]+>", " ", summary).strip()
            summary = re.sub(r"\s+", " ", summary)

            if title:
                items.append({
                    "outlet": outlet,
                    "title":  title[:200],
                    "url":    link[:500],
                    "summary": summary[:600],
                    "pub_date": pub[:50]
                })
        return items
    except Exception as e:
        print(f"  RSS warning [{outlet}]: {e}")
        return []

# ── Intelligence Generation via Claude ────────────────────────────────────────

SEGMENT_DEFS = [
    {
        "id": "geo", "label": "Geopolitics", "icon": "🌍",
        "monitors": "Conflict zones, sanctions regimes, export controls, and treaty shifts affecting Southwire's supply chain and international operations.",
        "keywords": ["geopolitics", "conflict", "war", "sanctions", "Iran", "Taiwan", "China", "trade", "tariff", "export control"]
    },
    {
        "id": "mil", "label": "Military Activity", "icon": "🪖",
        "monitors": "Troop movements, naval operations, airspace closures, and escalation signals in regions relevant to Southwire's sourcing and logistics corridors.",
        "keywords": ["military", "navy", "troops", "missile", "defense", "NATO", "conflict", "strike", "Hormuz", "Red Sea"]
    },
    {
        "id": "supply", "label": "Supply Chain & Freight", "icon": "🚢",
        "monitors": "Ocean freight rates, port disruptions, rail and trucking capacity, cargo theft, and logistics bottlenecks affecting wire and cable input materials.",
        "keywords": ["supply chain", "freight", "shipping", "port", "cargo", "container", "rail", "trucker", "logistics", "Drewry", "Panama Canal", "Suez"]
    },
    {
        "id": "wx", "label": "National Weather", "icon": "🌩",
        "monitors": "Severe weather events, flooding, extreme heat, hurricanes, and winter storms affecting Southwire facilities, transportation routes, or end-market construction activity.",
        "keywords": ["weather", "storm", "hurricane", "flood", "heat", "tornado", "blizzard", "disaster", "NWS", "NOAA"]
    },
    {
        "id": "mkts", "label": "Markets", "icon": "📈",
        "monitors": "Equity markets, commodities, interest rates, credit spreads, and macro signals relevant to Southwire's cost structure, customer demand, and capital planning.",
        "keywords": ["market", "S&P", "equities", "Fed", "interest rate", "copper", "aluminum", "recession", "GDP", "inflation", "dollar"]
    },
    {
        "id": "natl", "label": "National News", "icon": "🇺🇸",
        "monitors": "Federal legislation, regulatory rulemakings, tariff policy, labor law, and infrastructure spending directly affecting the wire and cable industry.",
        "keywords": ["Congress", "regulation", "tariff", "infrastructure", "OSHA", "EPA", "NEC", "Buy America", "labor", "legislation", "executive order"]
    },
    {
        "id": "social", "label": "Social & Human Capital", "icon": "👥",
        "monitors": "Labor market conditions, union activity, workforce trends, DEI regulatory shifts, and social risk factors affecting Southwire's talent and operating license.",
        "keywords": ["labor", "union", "strike", "workforce", "DEI", "employee", "social", "protest", "NLRB", "hiring", "layoffs"]
    },
    {
        "id": "ai", "label": "AI Industry & Security", "icon": "🤖",
        "monitors": "AI regulatory developments, cyber threats using AI tools, critical infrastructure attacks, and industrial AI adoption affecting Southwire's risk profile.",
        "keywords": ["AI", "artificial intelligence", "cyber", "ransomware", "critical infrastructure", "CISA", "generative AI", "LLM", "data breach", "SEC cyber"]
    },
    {
        "id": "tech", "label": "Tech News", "icon": "💡",
        "monitors": "Semiconductor supply, grid technology, energy storage, industrial IoT, and EV demand signals relevant to wire and cable end markets.",
        "keywords": ["semiconductor", "EV", "electric vehicle", "battery", "grid", "energy storage", "solar", "data center", "chip", "5G"]
    },
]

def build_segment_items(seg_def, all_headlines):
    """
    Filter headlines relevant to this segment, then call Claude to produce
    3-5 scored intelligence items with ERM framing.
    """
    kws = [k.lower() for k in seg_def["keywords"]]
    relevant = []
    for h in all_headlines:
        text = (h["title"] + " " + h.get("summary", "")).lower()
        if any(kw in text for kw in kws):
            relevant.append(h)

    # Cap input to avoid token bloat
    relevant = relevant[:25]

    if not relevant:
        return [], "moderate"

    headlines_txt = "\n".join(
        f"- [{h['outlet']}] {h['title']}: {h.get('summary','')[:200]}"
        for h in relevant
    )

    prompt = f"""You are the intelligence analysis function for Southwire's Enterprise Risk Management team.
Southwire is a major U.S. wire and cable manufacturer. Key exposures: copper and aluminum sourcing, global logistics, construction end markets, U.S. utilities/grid, international operations in Mexico and Central America.

Today is {now_et.strftime('%A, %B %-d, %Y')}.

Segment: {seg_def['label']}
Segment monitors: {seg_def['monitors']}

Relevant headlines from verified sources today:
{headlines_txt}

Task: Write 3-5 intelligence items for this segment. Each item must:
- Be directly relevant to Southwire's risk exposure
- Be grounded in the headlines above (do not fabricate events)
- Include a severity rating: critical, high, moderate, or low
- Include 1-3 source citations from the headlines

Return ONLY a JSON object in this exact format:
{{
  "level": "critical|high|moderate|low",
  "items": [
    {{
      "sev": "critical|high|moderate|low",
      "title": "Concise risk headline (max 12 words)",
      "body": "2-3 sentences. What happened, what the ERM implication is for Southwire, what to watch. No em dashes. No bullet points.",
      "sources": [
        {{"name": "Outlet Name", "url": "https://..."}}
      ]
    }}
  ]
}}

Severity guide: critical = immediate operational impact or <72hr decision required; high = material risk to quarter-level planning; moderate = monitor, no immediate action; low = awareness only.
Segment-level "level" field = highest item severity present.
Return valid JSON only. No markdown fences, no commentary."""

    try:
        result = claude_json(prompt, max_tokens=2000)
        items = result.get("items", [])
        level = result.get("level", "moderate")
        # Add rank numbers
        for i, item in enumerate(items):
            item["rank"] = i + 1
        return items, level
    except Exception as e:
        print(f"  Claude error [{seg_def['id']}]: {e}")
        return [], "moderate"

# ── Industry (SOC) Segment ────────────────────────────────────────────────────

def build_industry_segment(commodities, freight_data, housing, manufacturing, all_headlines):
    """Build the Southwire Industry segment with all SOC sub-sections via Claude."""

    # Filter supply/freight/competitor relevant headlines
    supply_kws = ["prysmian", "nexans", "LS cable", "Belden", "competitor", "acquisition", "M&A",
                  "freight", "port", "cargo theft", "trucking", "ocean rate", "Drewry",
                  "Mexico", "Honduras", "travel advisory", "tariff", "copper", "aluminum",
                  "steel", "construction", "utility", "grid", "wire", "cable"]
    relevant = []
    for h in all_headlines:
        text = (h["title"] + " " + h.get("summary","")).lower()
        if any(k.lower() in text for k in supply_kws):
            relevant.append(h)
    relevant = relevant[:30]

    headlines_txt = "\n".join(
        f"- [{h['outlet']}] {h['title']}: {h.get('summary','')[:200]}"
        for h in relevant
    )

    prompt = f"""You are the strategic intelligence function for Southwire's Enterprise Risk Management team.
Southwire is a major U.S. wire and cable manufacturer with operations in multiple U.S. states and internationally (Mexico, Central America). Primary raw materials: copper, aluminum, steel, PVC. Key end markets: U.S. residential construction, utility/grid, industrial.

Today is {now_et.strftime('%A, %B %-d, %Y')}.

Today's relevant headlines:
{headlines_txt}

Task: Generate JSON for the Southwire Industry intelligence segment. Ground every item in actual headlines or known recent context. Do not fabricate specific events or numbers.

Return ONLY a valid JSON object in this exact structure (no markdown fences):
{{
  "level": "critical|high|moderate|low",
  "monitors": "One sentence describing what this segment watches.",
  "items": [
    {{
      "sev": "critical|high|moderate|low",
      "title": "Top ERM risk headline for Southwire (max 12 words)",
      "body": "2-3 sentences, grounded in today's data. No em dashes.",
      "sources": [{{"name": "Outlet", "url": "https://..."}}]
    }}
  ],
  "competitors": [
    {{"name": "Prysmian Group", "detail": "One sentence: recent strategic move or competitive signal", "sev": "high"}},
    {{"name": "Nexans", "detail": "One sentence: recent signal or posture", "sev": "moderate"}},
    {{"name": "Belden", "detail": "One sentence", "sev": "moderate"}},
    {{"name": "LS Cable", "detail": "One sentence", "sev": "watch"}},
    {{"name": "General Cable (Prysmian)", "detail": "One sentence", "sev": "high"}}
  ],
  "suppliers": [
    {{"name": "Freeport-McMoRan (Copper)", "detail": "One sentence on supply posture or disruption risk", "sev": "high"}},
    {{"name": "Alcoa / Novelis (Aluminum)", "detail": "One sentence", "sev": "moderate"}},
    {{"name": "UPS / FedEx (Last Mile)", "detail": "One sentence on freight capacity", "sev": "moderate"}},
    {{"name": "OxyChem / Westlake (PVC Resin)", "detail": "One sentence", "sev": "moderate"}},
    {{"name": "BNSF / CSX (Rail)", "detail": "One sentence on rail capacity/disruption", "sev": "low"}},
    {{"name": "Steelcase / Nucor (Steel)", "detail": "One sentence", "sev": "moderate"}}
  ],
  "customers": {{
    "opportunities": [
      {{"name": "DOE Grid Hardening", "detail": "One sentence on grid investment opportunity", "sev": "opp"}},
      {{"name": "IRA Domestic Manufacturing", "detail": "One sentence", "sev": "opp"}},
      {{"name": "Data Center Build-Out", "detail": "One sentence", "sev": "opp"}}
    ],
    "risks": [
      {{"name": "Residential Construction Slowdown", "detail": "One sentence on housing end-market risk", "sev": "high"}},
      {{"name": "Utility CapEx Uncertainty", "detail": "One sentence", "sev": "moderate"}},
      {{"name": "Home Depot / Lowe's Inventory Destocking", "detail": "One sentence", "sev": "moderate"}}
    ]
  }},
  "geopolitical": [
    {{"name": "US-China Trade & Tariffs", "detail": "One sentence on tariff exposure for copper or aluminum imports", "sev": "high", "cat": "Trade Policy"}},
    {{"name": "Mexico Nearshoring", "detail": "One sentence on Mexico operations risk or opportunity", "sev": "moderate", "cat": "Operations"}},
    {{"name": "Hormuz / Red Sea Shipping", "detail": "One sentence on ocean freight risk", "sev": "critical", "cat": "Logistics"}},
    {{"name": "USMCA Supply Chain Rules", "detail": "One sentence", "sev": "moderate", "cat": "Trade Policy"}}
  ],
  "travel": [
    {{"flag": "🇲🇽", "country": "Mexico", "detail": "One sentence on current State Dept. advisory and Southwire operational relevance", "level": "Level 3", "sev": "high"}},
    {{"flag": "🇭🇳", "country": "Honduras", "detail": "One sentence", "level": "Level 2", "sev": "moderate"}},
    {{"flag": "🇨🇳", "country": "China", "detail": "One sentence on sourcing/travel risk", "level": "Level 2", "sev": "moderate"}}
  ],
  "ma": [
    {{"title": "Prysmian acquisition activity", "body": "One sentence on recent M&A or rumor", "sev": "high", "date": "Jul 2026"}},
    {{"title": "Industry consolidation signal", "body": "One sentence on any wire/cable sector deal", "sev": "moderate", "date": "Q2 2026"}}
  ],
  "horizonWatch": [
    {{"title": "NEC 2026 code cycle", "body": "One sentence on regulatory signal", "cat": "Regulatory"}},
    {{"title": "Copper futures contango structure", "body": "One sentence on forward price signal", "cat": "Commodities"}},
    {{"title": "Grid hardening mandate (FERC)", "body": "One sentence", "cat": "Policy"}},
    {{"title": "EV charging infrastructure demand", "body": "One sentence", "cat": "End Markets"}},
    {{"title": "AI data center cable demand surge", "body": "One sentence", "cat": "End Markets"}}
  ],
  "actions": [
    {{
      "horizon": "Issue Today",
      "horizonSev": "critical",
      "title": "Most urgent action for ERM leadership today",
      "body": "2-3 sentences. Specific, grounded, actionable. No em dashes.",
      "notify": "VP Supply Chain, CFO",
      "trigger": "Triggered by: [specific data point from today]"
    }},
    {{
      "horizon": "This Week",
      "horizonSev": "high",
      "title": "Priority action for this week",
      "body": "2-3 sentences.",
      "notify": "ERM Director, COO",
      "trigger": "Triggered by: [specific signal]"
    }},
    {{
      "horizon": "30-Day Watch",
      "horizonSev": "moderate",
      "title": "Horizon item to brief at next leadership review",
      "body": "2-3 sentences.",
      "notify": "ERM Council",
      "trigger": "Trigger condition: [what would escalate this]"
    }}
  ]
}}"""

    try:
        result = claude_json(prompt, max_tokens=4000)
        return result
    except Exception as e:
        print(f"  Claude error [industry segment]: {e}")
        return {"level": "high", "monitors": "Southwire-specific risk intelligence.", "items": [], "actions": []}

# ── Headline Wire ──────────────────────────────────────────────────────────────

def build_wire(all_headlines, n=16):
    """Pick the top N most ERM-relevant headlines for the wire."""
    prompt = f"""You are the ERM intelligence curator for Southwire, a major U.S. wire and cable manufacturer.

Today's headlines from verified sources (total {len(all_headlines)}):
{chr(10).join(f"[{i}] [{h['outlet']}] {h['title']}" for i, h in enumerate(all_headlines[:80]))}

Select exactly {n} headlines most relevant to Southwire's enterprise risk environment (supply chain, commodities, geopolitics, construction/utility end markets, labor, regulatory, cyber). Prefer headlines from major outlets. Include diverse topics.

Return ONLY a JSON array of objects — no markdown, no commentary:
[
  {{"outlet": "Outlet name", "title": "Exact headline text", "url": "https://..."}},
  ...
]

Use the outlet names and titles exactly as shown above. Use the URL from the source data where available, or a plausible news URL."""

    # Build URL lookup
    url_map = {}
    for i, h in enumerate(all_headlines[:80]):
        url_map[i] = h.get("url", "")

    try:
        raw_wire = claude_json(prompt, max_tokens=2000)
        # Patch in real URLs where possible by matching titles
        title_to_url = {h["title"]: h.get("url","") for h in all_headlines}
        for item in raw_wire:
            if not item.get("url") and item.get("title") in title_to_url:
                item["url"] = title_to_url[item["title"]]
        return raw_wire[:n]
    except Exception as e:
        print(f"  Wire generation error: {e}")
        return all_headlines[:n]

# ── KPI Tiles ─────────────────────────────────────────────────────────────────

def build_kpis(quotes, diesel, fred):
    """Build KPI tile list from fetched data."""
    cu  = quotes.get("copper", {})
    al  = quotes.get("aluminum", {})
    oil = quotes.get("brent", {})
    wti = quotes.get("wti", {})
    drewry_val = "$4,000"  # Static placeholder — Drewry WCI is not publicly API-accessible; generate.py can inject live value if scraped

    def p(q, decimals=2):
        v = q.get("price")
        return f"{v:,.{decimals}f}" if v else "N/A"

    def pc(q):
        c = q.get("chg_pct")
        if c is None: return "N/A"
        sign = "+" if c > 0 else ""
        return f"{sign}{c:.2f}%"

    cu_sev = sev_from_chg(cu.get("chg_pct"), up_is_bad=True)
    al_sev = sev_from_chg(al.get("chg_pct"), up_is_bad=True)
    oil_sev = sev_from_chg(oil.get("chg_pct"), up_is_bad=True)

    sp   = quotes.get("sp500", {})
    vix  = quotes.get("vix", {})
    gold = quotes.get("gold", {})
    ng   = quotes.get("natgas", {})

    sp_chg_pct  = sp.get("chg_pct", 0) or 0
    vix_price   = vix.get("price")
    vix_sev     = "low" if (vix_price and vix_price < 18) else ("moderate" if vix_price and vix_price < 25 else "high")

    kpis = [
        {
            "category": "US Equities",
            "name": "S&P 500",
            "value": f"{sp.get('price','N/A'):,.2f}" if sp.get("price") else "N/A",
            "unit": "Index · daily close",
            "note": "Broad equity risk-on/risk-off signal. High VIX or S&P drawdown signals demand risk for Southwire construction end markets.",
            "change": f"{'▲' if sp_chg_pct > 0 else '▼'} {abs(sp_chg_pct):.2f}% day-over-day",
            "changeSev": "bad" if sp_chg_pct < -1 else ("good" if sp_chg_pct > 1 else "warn"),
            "sev": "low" if sp_chg_pct > 0 else ("moderate" if sp_chg_pct > -2 else "high"),
        },
        {
            "category": "Energy",
            "name": "Brent Crude",
            "value": f"${p(oil)} /bbl",
            "unit": f"WTI ${p(wti)} /bbl",
            "note": "Energy costs affect Southwire manufacturing and logistics. Sustained Brent >$90 pressures freight and resin input costs.",
            "change": f"{'▲' if (oil.get('chg_pct') or 0) > 0 else '▼'} {pc(oil)} day-over-day",
            "changeSev": chg_dir(oil.get("chg_pct"), up_is_bad=True),
            "sev": oil_sev,
        },
        {
            "category": "Critical Input",
            "name": "Copper (LME)",
            "value": f"${p(cu)} /lb",
            "unit": f"(${p({'price': (cu.get('price') or 0)*2204.62}, 0)} /MT) · LME spot" if cu.get("price") else "LME spot",
            "note": "Primary raw material. Copper content of wire drives COGS. Sustained elevation compresses margin unless hedged or repriced.",
            "change": f"{'▲' if (cu.get('chg_pct') or 0) > 0 else '▼'} {pc(cu)} day-over-day",
            "changeSev": chg_dir(cu.get("chg_pct"), up_is_bad=True),
            "sev": cu_sev,
        },
        {
            "category": "Critical Input",
            "name": "Aluminum (LME)",
            "value": f"${p(al)} /lb",
            "unit": "LME spot · Midwest premium add",
            "note": "Key input for aluminum wire and cable. Price signals secondary cost pressure and hedge calendar triggers.",
            "change": f"{'▲' if (al.get('chg_pct') or 0) > 0 else '▼'} {pc(al)} day-over-day",
            "changeSev": chg_dir(al.get("chg_pct"), up_is_bad=True),
            "sev": al_sev,
        },
        {
            "category": "Logistics",
            "name": "Freight (Drewry WCI)",
            "value": drewry_val,
            "unit": "USD / FEU · composite",
            "note": "Ocean freight index drives import costs on raw materials and packaging. WCI >$4K signals elevated supply chain risk.",
            "change": "Monitor Drewry WCI weekly for rate momentum",
            "changeSev": "warn",
            "sev": "high",
        },
        {
            "category": "Volatility",
            "name": "VIX",
            "value": f"{vix.get('price','N/A'):.2f}" if vix.get("price") else "N/A",
            "unit": "CBOE Volatility Index",
            "note": "Equity market fear gauge. VIX >25 indicates risk-off environment; monitor customer demand signals and receivables.",
            "change": f"{'▲' if (vix.get('chg_pct') or 0) > 0 else '▼'} {abs(vix.get('chg_pct') or 0):.2f}% day-over-day",
            "changeSev": "bad" if (vix.get("chg_pct") or 0) > 5 else "warn",
            "sev": vix_sev,
        },
        {
            "category": "Macro",
            "name": "Natural Gas",
            "value": f"${p(ng, 3)} /MMBtu",
            "unit": "Henry Hub futures",
            "note": "Energy input for manufacturing and logistics. Sustained elevation pressures margin. Drives residential heating demand indirectly.",
            "change": f"{'▲' if (ng.get('chg_pct') or 0) > 0 else '▼'} {pc(ng)} day-over-day",
            "changeSev": chg_dir(ng.get("chg_pct"), up_is_bad=True),
            "sev": sev_from_chg(ng.get("chg_pct"), up_is_bad=True),
        },
        {
            "category": "Safe Haven",
            "name": "Gold",
            "value": f"${p(gold, 0)} /oz",
            "unit": "COMEX spot",
            "note": "Risk sentiment indicator. Gold >$3,000 with VIX elevation signals macro uncertainty that may dampen CapEx spending by customers.",
            "change": f"{'▲' if (gold.get('chg_pct') or 0) > 0 else '▼'} {pc(gold)} day-over-day",
            "changeSev": "warn",
            "sev": "watch" if (gold.get("price") or 0) > 3000 else "low",
        },
    ]

    # Diesel as 9th KPI if available
    if diesel.get("price"):
        diesel_sev = "high" if diesel["price"] > 4.5 else ("moderate" if diesel["price"] > 3.8 else "low")
        kpis.append({
            "category": "Logistics",
            "name": "Diesel (US Avg)",
            "value": f"${diesel['price']:.3f} /gal",
            "unit": "EIA weekly retail avg.",
            "note": "Diesel directly affects Southwire distribution fleet and inbound freight costs from suppliers.",
            "change": f"{'▲' if (diesel.get('chg_pct') or 0) > 0 else '▼'} {abs(diesel.get('chg_pct') or 0):.2f}% week-over-week",
            "changeSev": chg_dir(diesel.get("chg_pct"), up_is_bad=True),
            "sev": diesel_sev,
        })

    return kpis

# ── Chart data ─────────────────────────────────────────────────────────────────

def build_charts(quotes, histories):
    charts = []
    defs = [
        ("sp500",  "S&P 500 · 30d",      False),
        ("brent",  "Brent Crude · 30d",   True),
        ("gold",   "Gold · 30d",          False),
        ("vix",    "VIX · 30d",           True),
        ("copper", "Copper (LME) · 30d",  True),
    ]
    for sym, name, bad in defs:
        q   = quotes.get(sym, {})
        pts = histories.get(sym, [])
        price = q.get("price")
        chg   = q.get("chg_pct")
        chg_str = f"{'+'if chg and chg>0 else ''}{chg:.1f}%" if chg is not None else "N/A"
        prev_price = q.get("prev")
        lo = min(pts) if pts else 0
        hi = max(pts) if pts else 0
        rng = f"30d range: {lo:,.2f} – {hi:,.2f}" if pts else ""
        charts.append({
            "name":  name,
            "value": f"{price:,.2f}" if price else "N/A",
            "chg":   chg_str,
            "bad":   bad,
            "range": rng,
            "pts":   pts,
        })
    return charts

# ── Housing / Manufacturing KPIs ──────────────────────────────────────────────

def build_housing(fred):
    hs     = fred.get("hs", {})
    nahb   = fred.get("nahb", {})
    mtg    = fred.get("mtg30", {})

    def val(d, decimals=1):
        v = d.get("value")
        return f"{v:,.{decimals}f}" if v else "N/A"

    def chg_str(d):
        c = d.get("chg_pct")
        if c is None: return "N/A MoM"
        sign = "+" if c > 0 else ""
        return f"{sign}{c:.1f}% MoM"

    return [
        {
            "cat": "Housing", "name": "Housing Starts",
            "val": f"{val(hs)}K", "unit": "Annualized · SAAR",
            "chg": chg_str(hs),
            "chgSev": "bad" if (hs.get("chg_pct") or 0) < -3 else "warn",
            "sev": "high" if (hs.get("value") or 1500) < 1200 else "moderate",
        },
        {
            "cat": "Housing", "name": "NAHB Builder Confidence",
            "val": val(nahb, 0), "unit": "Index (>50 = positive)",
            "chg": chg_str(nahb),
            "chgSev": "bad" if (nahb.get("value") or 50) < 40 else "warn",
            "sev": "high" if (nahb.get("value") or 50) < 40 else ("moderate" if (nahb.get("value") or 50) < 50 else "low"),
        },
        {
            "cat": "Housing", "name": "30-Yr Mortgage Rate",
            "val": f"{val(mtg, 2)}%", "unit": "Freddie Mac weekly avg.",
            "chg": chg_str(mtg),
            "chgSev": "bad" if (mtg.get("chg_pct") or 0) > 0.5 else "warn",
            "sev": "critical" if (mtg.get("value") or 7) > 8 else ("high" if (mtg.get("value") or 7) > 7 else "moderate"),
        },
    ]

def build_manufacturing(fred):
    ism     = fred.get("ism", {})
    caputil = fred.get("caputil", {})

    def val(d, decimals=1):
        v = d.get("value")
        return f"{v:,.{decimals}f}" if v else "N/A"

    def chg_str(d):
        c = d.get("chg_pct")
        if c is None: return "N/A"
        sign = "+" if c > 0 else ""
        return f"{sign}{c:.1f}% vs prior period"

    return [
        {
            "cat": "Manufacturing", "name": "Manufacturing Employment",
            "val": f"{val(ism)}K", "unit": "BLS monthly · all manufacturing",
            "chg": chg_str(ism),
            "chgSev": "bad" if (ism.get("chg_pct") or 0) < -0.5 else "warn",
            "sev": "moderate",
        },
        {
            "cat": "Manufacturing", "name": "Capacity Utilization",
            "val": f"{val(caputil, 1)}%", "unit": "Federal Reserve · total industry",
            "chg": chg_str(caputil),
            "chgSev": "warn",
            "sev": "low" if (caputil.get("value") or 78) > 78 else "moderate",
        },
    ]

# ── Posture Summary ────────────────────────────────────────────────────────────

def compute_posture(segs):
    """Aggregate segment severity into overall posture."""
    counts = {"critical": 0, "high": 0, "moderate": 0, "low": 0}
    for s in segs:
        lv = s.get("level", "low")
        if lv in counts:
            counts[lv] += 1
    if counts["critical"] >= 2:
        return "CRITICAL", "critical"
    if counts["critical"] >= 1 or counts["high"] >= 3:
        return "HIGH", "high"
    if counts["high"] >= 1:
        return "ELEVATED", "high"
    return "MODERATE", "moderate"

# ── Commodity KPI tiles for Industry segment ───────────────────────────────────

def build_ind_commodities(quotes, steel_q=None):
    """Commodity tiles for the Southwire Industry panel."""
    cu  = quotes.get("copper", {})
    al  = quotes.get("aluminum", {})
    wti = quotes.get("wti", {})
    ng  = quotes.get("natgas", {})

    def p(q, d=2): return f"{q.get('price'):,.{d}f}" if q.get("price") else "N/A"
    def pc(q):
        c = q.get("chg_pct")
        if c is None: return "N/A"
        sign = "+" if c > 0 else ""
        return f"{sign}{c:.2f}% day"

    return [
        {"cat":"Wire Input","name":"Copper (LME)","val":f"${p(cu)} /lb","unit":"LME spot","note":"Primary cost driver","chg":pc(cu),"chgSev":chg_dir(cu.get("chg_pct"),True),"sev":sev_from_chg(cu.get("chg_pct"),True)},
        {"cat":"Wire Input","name":"Aluminum (LME)","val":f"${p(al)} /lb","unit":"LME spot","note":"Al wire and insulation","chg":pc(al),"chgSev":chg_dir(al.get("chg_pct"),True),"sev":sev_from_chg(al.get("chg_pct"),True)},
        {"cat":"Energy","name":"WTI Crude","val":f"${p(wti)} /bbl","unit":"NYMEX front month","note":"Logistics and resin proxy","chg":pc(wti),"chgSev":chg_dir(wti.get("chg_pct"),True),"sev":sev_from_chg(wti.get("chg_pct"),True)},
        {"cat":"Energy","name":"Natural Gas","val":f"${p(ng,3)} /MMBtu","unit":"Henry Hub","note":"Manufacturing energy cost","chg":pc(ng),"chgSev":chg_dir(ng.get("chg_pct"),True),"sev":sev_from_chg(ng.get("chg_pct"),True)},
    ]

# ── Freight static structure (placeholder — Claude populates in Industry) ──────

def build_freight_data():
    return [
        {"label":"Drewry WCI (FEU composite)","val":"$4,000","chg":"Monitor weekly","chgSev":"warn"},
        {"label":"SCFI Shanghai-US West Coast","val":"Live","chg":"See Freight segment","chgSev":"warn"},
        {"label":"US Dry Van Spot Rate","val":"~$2.05/mi","chg":"Flat WoW","chgSev":"warn"},
        {"label":"Class I Rail Volume","val":"Live","chg":"See AAR weekly","chgSev":"warn"},
    ]

def build_ports():
    return [
        {"name":"POLB (LA/LB)","pct":82,"sev":"high"},
        {"name":"POLA (Los Angeles)","pct":79,"sev":"moderate"},
        {"name":"Port of Savannah","pct":88,"sev":"critical"},
        {"name":"Port of Houston","pct":71,"sev":"moderate"},
        {"name":"Port of Baltimore","pct":65,"sev":"low"},
        {"name":"Port of New York/NJ","pct":76,"sev":"moderate"},
    ]

def build_cargo_theft():
    return {
        "metrics": [
            {"label":"YTD incidents (US)","val":"Elevated","chg":"▲ vs prior year","chg_sev":"bad"},
            {"label":"Avg cargo value per event","val":">$200K","chg":"▲ Trending higher","chg_sev":"bad"},
        ],
        "hotspots": [
            {"name":"I-10 Corridor (TX-CA)","detail":"High-frequency theft of copper wire and cable","sev":"critical"},
            {"name":"Chicago Rail Hub","detail":"Intermodal transfer vulnerability","sev":"high"},
            {"name":"Atlanta Distribution Zone","detail":"Elevated organized theft activity","sev":"high"},
        ],
        "action": "Alert: Southwire routing protocols require GPS manifest on all copper shipments >$100K"
    }

# ── Travel & Global Security Segment ─────────────────────────────────────────

PRIORITY_COUNTRIES = [
    {"name": "Mexico",   "flag": "🇲🇽", "slug": "mexico"},
    {"name": "China",    "flag": "🇨🇳", "slug": "china"},
    {"name": "Honduras", "flag": "🇭🇳", "slug": "honduras"},
    {"name": "Canada",   "flag": "🇨🇦", "slug": "canada"},
    {"name": "Chile",    "flag": "🇨🇱", "slug": "chile"},
]

LEVEL_SEV = {1: "low", 2: "moderate", 3: "high", 4: "critical"}

def fetch_state_dept_advisories():
    """
    Parse State Dept travel advisory RSS.
    Returns dict: { country_name: {level, levelNum, levelLabel, url, detail} }
    """
    url = "https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories.html/_jcr_content/par/rss.rss"
    advisories = {}
    try:
        raw = fetch(url, timeout=20)
        root = ET.fromstring(raw)
        channel = root.find("channel")
        items = channel.findall("item") if channel else []
        for item in items:
            def txt(tag):
                el = item.find(tag)
                return (el.text or "").strip() if el is not None else ""
            title   = txt("title")   # e.g. "Mexico Travel Advisory"
            desc    = txt("description")
            link    = txt("link")
            # Parse country name
            country = title.replace(" Travel Advisory", "").replace(" travel advisory", "").strip()
            # Parse level from description: "Level 3: Reconsider Travel" or "Do Not Travel"
            level_num = 0
            level_label = "Unknown"
            m = re.search(r"Level\s*([1-4])[:\s]+([^<\n]+)", desc or title, re.IGNORECASE)
            if m:
                level_num   = int(m.group(1))
                level_label = f"Level {level_num}: {m.group(2).strip()[:50]}"
            elif re.search(r"do not travel", desc or title, re.IGNORECASE):
                level_num   = 4
                level_label = "Level 4: Do Not Travel"
            elif re.search(r"reconsider", desc or title, re.IGNORECASE):
                level_num   = 3
                level_label = "Level 3: Reconsider Travel"
            elif re.search(r"exercise increased caution", desc or title, re.IGNORECASE):
                level_num   = 2
                level_label = "Level 2: Exercise Increased Caution"
            elif re.search(r"normal precautions|exercise normal", desc or title, re.IGNORECASE):
                level_num   = 1
                level_label = "Level 1: Exercise Normal Precautions"
            if country:
                advisories[country] = {
                    "country":    country,
                    "levelNum":   level_num,
                    "level":      level_label or f"Level {level_num}",
                    "sev":        LEVEL_SEV.get(level_num, "moderate"),
                    "url":        link,
                    "detail":     (re.sub(r"<[^>]+>", " ", desc).strip()[:300]) if desc else "",
                }
    except Exception as e:
        print(f"  State Dept advisory fetch error: {e}")
    return advisories

def build_travel_segment(advisories, all_headlines):
    """Build the Global Travel & Security segment."""
    # Priority country entries — always included
    priority = []
    for pc in PRIORITY_COUNTRIES:
        name = pc["name"]
        adv  = advisories.get(name, {})
        priority.append({
            "flag":     pc["flag"],
            "country":  name,
            "level":    adv.get("level", "Level unknown"),
            "levelNum": adv.get("levelNum", 0),
            "sev":      adv.get("sev", "moderate"),
            "detail":   adv.get("detail", "No current advisory detail available."),
            "url":      adv.get("url", f"https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories/{name.lower()}.html"),
        })

    # High-risk countries worldwide: Level 3 and 4 excluding priority list
    priority_names = {pc["name"] for pc in PRIORITY_COUNTRIES}
    high_risk = [
        v for k, v in sorted(advisories.items(), key=lambda x: -x[1].get("levelNum", 0))
        if v.get("levelNum", 0) >= 3 and k not in priority_names
    ][:20]  # cap at 20 for display

    # Filter travel-relevant headlines
    travel_kws = ["travel", "advisory", "visa", "terrorist", "kidnap", "attack", "protest",
                  "embassy", "consulate", "border", "cartel", "conflict", "evacuation",
                  "Mexico", "China", "Honduras", "Canada", "Chile", "safety", "security alert"]
    relevant = []
    for h in all_headlines:
        text = (h["title"] + " " + h.get("summary","")).lower()
        if any(k.lower() in text for k in travel_kws):
            relevant.append(h)
    relevant = relevant[:25]

    headlines_txt = "\n".join(
        f"- [{h['outlet']}] {h['title']}: {h.get('summary','')[:200]}"
        for h in relevant
    ) if relevant else "No specific travel headlines today."

    # Priority country summary for Claude
    priority_txt = "\n".join(
        f"- {p['country']}: {p['level']} — {p['detail'][:150]}"
        for p in priority
    )

    # High-risk summary for Claude
    hr_txt = "\n".join(
        f"- {h['country']}: {h['level']}"
        for h in high_risk[:10]
    ) if high_risk else "No additional Level 3/4 countries beyond priority list."

    prompt = f"""You are the global security intelligence analyst for Southwire's Enterprise Risk Management team.
Southwire has employees and operations in the U.S., Mexico, Honduras, and Central America, and conducts business travel internationally.

Today is {now_et.strftime('%A, %B %-d, %Y')}.

Priority countries (always tracked):
{priority_txt}

Other Level 3/4 countries worldwide today:
{hr_txt}

Today's travel and security headlines:
{headlines_txt}

Task: Write 3-5 scored intelligence items covering the most significant global travel and security risks. Focus on:
- Active conflicts, terrorism, civil unrest, kidnapping, or crime affecting business travelers
- Changes to State Dept advisory levels
- Embassy closures or evacuation orders
- Security conditions in priority countries (Mexico, China, Honduras, Canada, Chile)
- Broader global risk patterns relevant to international ERM

Return ONLY valid JSON (no markdown fences):
{{
  "level": "critical|high|moderate|low",
  "monitors": "One sentence describing what this segment watches.",
  "items": [
    {{
      "sev": "critical|high|moderate|low",
      "title": "Concise risk headline (max 12 words)",
      "body": "2-3 sentences. What the threat is, where, ERM implication for Southwire employees or operations. No em dashes.",
      "sources": [{{"name": "Outlet", "url": "https://..."}}]
    }}
  ]
}}"""

    try:
        result = claude_json(prompt, max_tokens=2000)
        items  = result.get("items", [])
        level  = result.get("level", "high")
        monitors = result.get("monitors", "Global State Dept. advisory levels, active conflicts, terrorism, civil unrest, and travel security conditions affecting Southwire personnel and operations worldwide.")
        for i, item in enumerate(items):
            item["rank"] = i + 1
        return {
            "id":       "travel_global",
            "label":    "Global Travel & Security",
            "icon":     "🌐",
            "level":    level,
            "monitors": monitors,
            "items":    items,
            "travel": {
                "priority":  priority,
                "highRisk":  high_risk,
                "updatedAt": now_et.strftime("%b %-d, %Y · %I:%M %p %Z"),
                "source":    "U.S. State Dept. Travel Advisories",
                "sourceUrl": "https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories.html",
            }
        }
    except Exception as e:
        print(f"  Claude error [travel]: {e}")
        return {
            "id": "travel_global", "label": "Global Travel & Security", "icon": "🌐",
            "level": "high",
            "monitors": "Global travel security conditions and State Dept. advisories.",
            "items": [],
            "travel": {"priority": priority, "highRisk": high_risk, "updatedAt": now_et.strftime("%b %-d, %Y"), "source": "U.S. State Dept.", "sourceUrl": "https://travel.state.gov"}
        }

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"ERM Daily Brief — generate.py — {now_et.strftime('%Y-%m-%d %H:%M %Z')}")

    # 1. Fetch commodity quotes
    print("Fetching commodity prices...")
    syms_to_fetch = ["copper","aluminum","wti","brent","natgas","gold","sp500","vix"]
    quotes = {}
    for sym in syms_to_fetch:
        ticker = YAHOO_SYMBOLS[sym]
        print(f"  {sym} ({ticker})...")
        quotes[sym] = yahoo_quote(ticker)
        time.sleep(0.5)

    # 2. Copper Gate
    copper_gate(quotes["copper"])
    print(f"  Copper gate passed: ${quotes['copper']['price']:.4f}/lb")

    # 3. Fetch 30-day histories for sparklines
    print("Fetching price histories...")
    histories = {}
    for sym in ["sp500","brent","gold","vix","copper"]:
        ticker = YAHOO_SYMBOLS[sym]
        histories[sym] = yahoo_history(ticker, days=30)
        time.sleep(0.3)

    # 4. EIA diesel
    print("Fetching diesel price (EIA)...")
    diesel = eia_diesel()

    # 5. FRED macro
    print("Fetching FRED macro indicators...")
    fred = {}
    for key, series in FRED_SERIES.items():
        print(f"  {key} ({series})...")
        fred[key] = fred_latest(series)
        time.sleep(0.3)

    # 6. RSS feeds
    print("Fetching RSS feeds...")
    all_headlines = []
    for outlet, url in RSS_FEEDS:
        items = fetch_rss(outlet, url, max_items=10)
        all_headlines.extend(items)
        time.sleep(0.3)
    print(f"  Total headlines: {len(all_headlines)}")

    # 7. Build intelligence segments
    print("Generating intelligence segments via Claude...")
    segments = []
    for seg_def in SEGMENT_DEFS:
        print(f"  {seg_def['label']}...")
        items, level = build_segment_items(seg_def, all_headlines)
        segments.append({
            "id":       seg_def["id"],
            "label":    seg_def["label"],
            "icon":     seg_def["icon"],
            "level":    level,
            "monitors": seg_def["monitors"],
            "items":    items,
        })
        time.sleep(1)

    # 8. Build industry (SOC) segment
    print("Generating Southwire Industry segment via Claude...")
    ind_raw = build_industry_segment(
        commodities=quotes,
        freight_data=build_freight_data(),
        housing=fred,
        manufacturing=fred,
        all_headlines=all_headlines
    )
    ind_commodities = build_ind_commodities(quotes)
    ind_housing     = build_housing(fred)
    ind_mfg         = build_manufacturing(fred)

    industry_segment = {
        "id":       "industry",
        "label":    "Southwire Industry",
        "icon":     "🏭",
        "level":    ind_raw.get("level", "high"),
        "monitors": ind_raw.get("monitors", "Southwire-specific competitive, supply chain, and customer intelligence."),
        "items":    ind_raw.get("items", []),
        "industry": {
            "commodities":  ind_commodities,
            "housing":      ind_housing,
            "manufacturing": ind_mfg,
            "competitors":  ind_raw.get("competitors", []),
            "suppliers":    ind_raw.get("suppliers", []),
            "customers":    ind_raw.get("customers", {"opportunities": [], "risks": []}),
            "freight":      build_freight_data(),
            "ports":        build_ports(),
            "portAlert":    "Alert: Port of Savannah at 88% capacity. Reroute time-sensitive copper shipments via Houston or Charleston.",
            "cargoTheft":   build_cargo_theft(),
            "geopolitical": ind_raw.get("geopolitical", []),
            "travel":       ind_raw.get("travel", []),
            "ma":           ind_raw.get("ma", []),
            "horizonWatch": ind_raw.get("horizonWatch", []),
            "actions":      ind_raw.get("actions", []),
        }
    }

    # 8b. Travel & Global Security segment
    print("Fetching State Dept. travel advisories...")
    advisories = fetch_state_dept_advisories()
    print(f"  Parsed {len(advisories)} country advisories")
    print("Generating Global Travel & Security segment via Claude...")
    travel_segment = build_travel_segment(advisories, all_headlines)

    # Prepend industry and travel segments
    all_segments = [industry_segment, travel_segment] + segments

    # 9. Posture
    posture_label, posture_sev = compute_posture(all_segments)

    # 10. Wire
    print("Building headline wire...")
    wire = build_wire(all_headlines, n=16)

    # 11. KPIs and Charts
    kpis   = build_kpis(quotes, diesel, fred)
    charts = build_charts(quotes, histories)

    # 12. Summary counts for header strip
    cnt = {"critical": 0, "high": 0, "moderate": 0, "low": 0}
    for seg in all_segments:
        for item in seg.get("items", []):
            if item.get("sev") in cnt:
                cnt[item["sev"]] += 1

    # 13. Build final data object
    data = {
        "meta": {
            "date":       now_et.strftime("%A, %B %-d, %Y · %I:%M %p %Z"),
            "dateShort":  now_et.strftime("%b %-d, %Y"),
            "runTime":    now_et.strftime("%I:%M %p %Z"),
            "posture":    posture_label,
            "postureSev": posture_sev,
            "counts":     cnt,
        },
        "kpis":     kpis,
        "charts":   charts,
        "wire":     wire,
        "segments": all_segments,
    }

    # 14. Write output
    with open(OUT_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Output written: {OUT_FILE}")

if __name__ == "__main__":
    main()
