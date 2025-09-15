# app.py
import os
import re
import json
import time
import logging
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import azure.functions as func
import requests
from bs4 import BeautifulSoup

app = func.FunctionApp()

# 기본 값 (환경변수로 덮어쓰기 가능)
DEFAULT_CATEGORY_URL = os.getenv("CATEGORY_URL", "https://techcrunch.com/category/artificial-intelligence/")
DEFAULT_LIMIT = int(os.getenv("LIMIT", "40"))
DEFAULT_SLEEP = float(os.getenv("SLEEP_SEC", "0.7"))
DEFAULT_TIMEOUT = int(os.getenv("TIMEOUT_SEC", "20"))
DEFAULT_UA = os.getenv("USER_AGENT",
                       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36")

HEADERS = {"User-Agent": DEFAULT_UA}
KST = ZoneInfo("Asia/Seoul")

def fetch(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r

def is_article_url(href: str) -> bool:
    try:
        u = urlparse(href)
        path = u.path or ""
        return (("techcrunch.com" in (u.netloc or "") or (u.netloc or "") == "")
                and re.search(r"/20\d{2}/\d{2}/", path) is not None)
    except Exception:
        return False

def normalize_link(href: str, base: str) -> str:
    return urljoin(base, href)

def get_article_links(category_url: str, limit: int = 50) -> list[str]:
    html = fetch(category_url).text
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    # 1) h3 내부 앵커
    for h3 in soup.find_all("h3"):
        a = h3.find("a", href=True)
        if a and is_article_url(a["href"]):
            links.add(normalize_link(a["href"], category_url))
            if len(links) >= limit:
                break

    # 2) 보강: 모든 a 스캔
    if len(links) < limit:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if is_article_url(href):
                links.add(normalize_link(href, category_url))
            if len(links) >= limit:
                break

    return list(links)[:limit]

def get_meta_datetime(soup: BeautifulSoup, prop: str):
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    if tag and tag.get("content"):
        try:
            return datetime.fromisoformat(tag["content"].replace("Z", "+00:00"))
        except Exception:
            pass
    return None

def parse_human_datetime(text: str):
    m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}", text)
    if m:
        try:
            d = datetime.strptime(m.group(0), "%B %d, %Y")
            return d.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None

def get_ldjson_datetime(soup: BeautifulSoup):
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
            candidates = data if isinstance(data, list) else [data]
            for obj in candidates:
                if isinstance(obj, dict) and obj.get("@type") in {"NewsArticle", "Article", "BlogPosting"}:
                    dp = obj.get("datePublished") or obj.get("dateCreated")
                    if dp:
                        return datetime.fromisoformat(str(dp).replace("Z", "+00:00"))
        except Exception:
            continue
    return None

def get_time_tag_datetime(soup: BeautifulSoup):
    t = soup.find("time")
    if t and t.get("datetime"):
        try:
            return datetime.fromisoformat(t["datetime"].replace("Z", "+00:00"))
        except Exception:
            pass
    if t and t.get_text(strip=True):
        return parse_human_datetime(t.get_text(" ", strip=True))
    return None

def get_text_datetime_fallback(soup: BeautifulSoup):
    text = soup.get_text(" ", strip=True)
    return parse_human_datetime(text)

def get_ldjson_article_body(soup: BeautifulSoup):
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
            candidates = data if isinstance(data, list) else [data]
            for obj in candidates:
                if isinstance(obj, dict) and obj.get("@type") in {"NewsArticle", "Article", "BlogPosting"}:
                    body = obj.get("articleBody")
                    if body:
                        return str(body)
        except Exception:
            continue
    return None

def extract_paragraphs(soup: BeautifulSoup) -> str:
    article = soup.find("article") or soup
    paragraphs = []
    for p in article.find_all("p"):
        if p.find_parent(["aside", "figcaption", "nav", "footer"]):
            continue
        txt = p.get_text(" ", strip=True)
        if len(txt) >= 2:
            paragraphs.append(txt)
    return "\n\n".join(paragraphs)

def parse_article(url: str) -> dict:
    res = fetch(url)
    soup = BeautifulSoup(res.text, "html.parser")

    title_tag = soup.find("h1")
    title_text = title_tag.get_text(strip=True) if title_tag else ""

    published_dt = (
        get_meta_datetime(soup, "article:published_time")
        or get_ldjson_datetime(soup)
        or get_time_tag_datetime(soup)
        or get_text_datetime_fallback(soup)
    )

    body_text = get_ldjson_article_body(soup) or extract_paragraphs(soup)

    return {
        "url": url,
        "title": title_text,
        "published_utc": published_dt.astimezone(timezone.utc).isoformat() if published_dt else None,
        "published_kst": published_dt.astimezone(KST).isoformat() if published_dt else None,
        "body": (body_text or "").strip()
    }

def is_today_kst(dt: datetime, today_kst: datetime) -> bool:
    if not dt:
        return False
    return dt.astimezone(KST).date() == today_kst.date()

def crawl_today(category_url: str, today_kst: datetime, limit: int, sleep_sec: float) -> list[dict]:
    links = get_article_links(category_url, limit=limit)
    results = []
    for url in links:
        try:
            art = parse_article(url)
            if art["published_kst"] and is_today_kst(datetime.fromisoformat(art["published_kst"]), today_kst):
                results.append(art)
        except Exception as e:
            logging.warning(f"[WARN] parse fail {url}: {e}")
        time.sleep(sleep_sec)
    return results

@app.function_name(name="tc_crawl_http")
@app.route(route="crawl", auth_level=func.AuthLevel.FUNCTION)
def tc_crawl_http(req: func.HttpRequest) -> func.HttpResponse:
    try:
        q_today = (req.params.get("today") or "1").strip()
        only_today = q_today == "1"
        category_url = (req.params.get("url") or DEFAULT_CATEGORY_URL).strip()
        limit = int(req.params.get("limit") or DEFAULT_LIMIT)
        sleep_sec = float(req.params.get("sleep") or DEFAULT_SLEEP)
    except Exception:
        only_today = True
        category_url = DEFAULT_CATEGORY_URL
        limit = DEFAULT_LIMIT
        sleep_sec = DEFAULT_SLEEP

    try:
        if only_today:
            today_kst = datetime.now(KST)
            items = crawl_today(category_url, today_kst, limit, sleep_sec)
            payload = {"date_kst": today_kst.strftime("%Y-%m-%d"), "count": len(items), "items": items}
        else:
            links = get_article_links(category_url, limit=limit)
            items = []
            for url in links:
                try:
                    items.append(parse_article(url))
                except Exception as ex:
                    logging.warning(f"[WARN] parse fail {url}: {ex}")
                time.sleep(sleep_sec)
            payload = {"count": len(items), "items": items}

        return func.HttpResponse(
            json.dumps({"ok": True, **payload}, ensure_ascii=False),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        logging.exception("crawl error")
        return func.HttpResponse(
            json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False),
            mimetype="application/json",
            status_code=500
        )
