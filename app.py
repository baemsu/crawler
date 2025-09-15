import os, json, logging
from datetime import datetime, timedelta
from dateutil import tz
import azure.functions as func
import requests
from bs4 import BeautifulSoup
import feedparser

app = func.FunctionApp()

TC_FEED_URL = os.getenv("TC_FEED_URL", "https://techcrunch.com/category/artificial-intelligence/feed/")
MAX_ITEMS   = int(os.getenv("MAX_ITEMS", "10"))
TZ_NAME     = os.getenv("TZ_NAME", "Asia/Seoul")
USE_RSS_ONLY = os.getenv("USE_RSS_ONLY", "true").lower() == "true"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tc-crawl-func/1.0)"}
TIMEOUT = 15

def fetch_from_rss():
    d = feedparser.parse(TC_FEED_URL)
    items = []
    for e in d.entries[:MAX_ITEMS]:
        published = None
        if hasattr(e, "published_parsed") and e.published_parsed:
            published = datetime(*e.published_parsed[:6])
        link = getattr(e, "link", "")
        title = getattr(e, "title", "").strip()
        summary = BeautifulSoup(getattr(e, "summary", ""), "lxml").get_text(" ", strip=True) if hasattr(e, "summary") else ""
        items.append({
            "title": title,
            "url": link,
            "summary_hint": summary[:400],
            "published_utc": published.isoformat() + "Z" if published else None
        })
    return items

def fetch_article_body(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for sel in ["div.article-content", "div#root", "div.wp-block-post-content", "article"]:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(" ", strip=True)
                if len(text) > 200:
                    return text
        return soup.get_text(" ", strip=True)[:4000]
    except Exception as ex:
        logging.warning(f"fetch_article_body failed: {ex}")
        return None

def today_filter(items, tz_name=TZ_NAME):
    tzinfo = tz.gettz(tz_name)
    now_local = datetime.utcnow().replace(tzinfo=tz.gettz("UTC")).astimezone(tzinfo)
    start = datetime(now_local.year, now_local.month, now_local.day, tzinfo=tzinfo)
    end = start + timedelta(days=1)
    out = []
    for it in items:
        pu = it.get("published_utc")
        if not pu:
            out.append(it)
            continue
        try:
            dt = datetime.fromisoformat(pu.replace("Z","+00:00")).astimezone(tzinfo)
            if start <= dt < end:
                out.append(it)
        except Exception:
            out.append(it)
    return out

def crawl_articles(todays_only=False):
    base = fetch_from_rss()
    if todays_only:
        base = today_filter(base)
    # RSS만 쓰려면 content=summary_hint, 아니면 본문 보강
    enriched = []
    for it in base:
        body = None if USE_RSS_ONLY else fetch_article_body(it["url"])
        enriched.append({**it, "content": body or it.get("summary_hint")})
    return enriched

# -------- HTTP Trigger: /api/crawl --------
@app.function_name(name="tc_crawl_http")
@app.route(route="crawl", auth_level=func.AuthLevel.FUNCTION)
def tc_crawl_http(req: func.HttpRequest) -> func.HttpResponse:
    try:
        only_today = (req.params.get("today") == "1") or (req.get_json(silent=True) or {}).get("today") == 1
    except Exception:
        only_today = False
    items = crawl_articles(todays_only=only_today)
    body = json.dumps({"ok": True, "count": len(items), "items": items}, ensure_ascii=False)
    return func.HttpResponse(body, mimetype="application/json", status_code=200)
