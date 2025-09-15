# 필요한 패키지:
# pip install requests beautifulsoup4

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo  # Python 3.9+
import json
import re
import time

CATEGORY_URL = "https://techcrunch.com/category/artificial-intelligence/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

KST = ZoneInfo("Asia/Seoul")

def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r

def get_article_links(category_url=CATEGORY_URL, limit=50):
    """
    카테고리 페이지에서 기사 링크를 최대 limit개까지 수집.
    TechCrunch는 동적 로딩이 있으나 1페이지의 주요 기사 링크는 서버 렌더링됨. :contentReference[oaicite:1]{index=1}
    """
    html = fetch(category_url).text
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    # 1) h3 내부의 앵커들 우선
    for h3 in soup.find_all("h3"):
        a = h3.find("a", href=True)
        if a and is_article_url(a["href"]):
            links.add(normalize_link(a["href"]))

    # 2) 보강: 페이지 내 모든 a 중 연-월 패턴 포함 URL
    if len(links) < limit:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if is_article_url(href):
                links.add(normalize_link(href))
            if len(links) >= limit:
                break

    return list(links)[:limit]

def is_article_url(href: str) -> bool:
    """
    TechCrunch 기사 URL은 일반적으로 /YYYY/MM/ 형태를 가짐.
    """
    try:
        u = urlparse(href)
        path = u.path
        return ("techcrunch.com" in u.netloc or u.netloc == "") and re.search(r"/20\d{2}/\d{2}/", path)
    except Exception:
        return False

def normalize_link(href: str) -> str:
    return urljoin(CATEGORY_URL, href)

def parse_article(url: str):
    """
    기사 페이지에서 제목, 본문, 발행일시(UTC/KST 변환)를 파싱.
    발행일은 우선순위:
      1) <meta property="article:published_time" content="ISO8601">
      2) JSON-LD(NewsArticle/BlogPosting) 내 datePublished
      3) <time datetime="...">
      4) 본문 상단 날짜 텍스트(최후 수단, 정규식)
    본문은 다음 우선순위:
      1) JSON-LD의 articleBody
      2) 본문 컨테이너 내 <p>들을 연결
    """
    res = fetch(url)
    soup = BeautifulSoup(res.text, "html.parser")

    # 제목
    title = soup.find("h1")
    title_text = title.get_text(strip=True) if title else ""

    # 발행일
    published_dt = (
        get_meta_datetime(soup, "article:published_time")
        or get_ldjson_datetime(soup)
        or get_time_tag_datetime(soup)
        or get_text_datetime_fallback(soup)
    )

    # 본문
    body_text = (
        get_ldjson_article_body(soup)
        or extract_paragraphs(soup)
    )

    return {
        "url": url,
        "title": title_text,
        "published_utc": published_dt.astimezone(timezone.utc).isoformat() if published_dt else None,
        "published_kst": published_dt.astimezone(KST).isoformat() if published_dt else None,
        "body": body_text.strip()
    }

def get_meta_datetime(soup, prop):
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    if tag and tag.get("content"):
        try:
            return datetime.fromisoformat(tag["content"].replace("Z", "+00:00"))
        except Exception:
            pass
    return None

def get_ldjson_datetime(soup):
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
            # 단일 객체 또는 리스트 모두 처리
            candidates = data if isinstance(data, list) else [data]
            for obj in candidates:
                if isinstance(obj, dict) and obj.get("@type") in {"NewsArticle", "Article", "BlogPosting"}:
                    dp = obj.get("datePublished") or obj.get("dateCreated")
                    if dp:
                        return datetime.fromisoformat(dp.replace("Z", "+00:00"))
        except Exception:
            continue
    return None

def get_time_tag_datetime(soup):
    t = soup.find("time")
    if t and t.get("datetime"):
        try:
            return datetime.fromisoformat(t["datetime"].replace("Z", "+00:00"))
        except Exception:
            pass
    # 화면표시 텍스트에 '· Month DD, YYYY' 형태가 있을 수 있음. :contentReference[oaicite:2]{index=2}
    if t and t.get_text(strip=True):
        return parse_human_datetime(t.get_text(" ", strip=True))
    return None

def get_text_datetime_fallback(soup):
    # 기사 상단 영역에 '10:10 PM PDT · September 10, 2025' 같은 문자열이 있을 수 있음. :contentReference[oaicite:3]{index=3}
    text = soup.get_text(" ", strip=True)
    return parse_human_datetime(text)

def parse_human_datetime(text: str):
    # 예: "September 10, 2025" / "10:10 PM PDT · September 10, 2025"
    m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}", text)
    if m:
        try:
            d = datetime.strptime(m.group(0), "%B %d, %Y")
            # 시각 정보가 없으면 UTC 자정으로 가정
            return d.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None

def get_ldjson_article_body(soup):
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
            candidates = data if isinstance(data, list) else [data]
            for obj in candidates:
                if isinstance(obj, dict) and obj.get("@type") in {"NewsArticle", "Article", "BlogPosting"}:
                    body = obj.get("articleBody")
                    if body:
                        return body
        except Exception:
            continue
    return None

def extract_paragraphs(soup):
    # 기사 본문 컨테이너 추정: <article> 내부 p 수집(aside/figure/nav 등 제외)
    article = soup.find("article") or soup
    paragraphs = []
    for p in article.find_all("p"):
        # 광고/공유/캡션/네비 등 제외 가능성 높은 패턴 제거
        bad = p.find_parent(["aside", "figcaption", "nav", "footer"])
        if bad:
            continue
        txt = p.get_text(" ", strip=True)
        if len(txt) >= 2:
            paragraphs.append(txt)
    return "\n\n".join(paragraphs)

def is_today_kst(dt: datetime, today_kst: datetime):
    if not dt:
        return False
    return dt.astimezone(KST).date() == today_kst.date()

def crawl_today(category_url=CATEGORY_URL, today_kst=None, limit=40, sleep_sec=1.0):
    if today_kst is None:
        today_kst = datetime.now(KST)
    links = get_article_links(category_url, limit=limit)
    results = []
    for i, url in enumerate(links, 1):
        try:
            art = parse_article(url)
            if art["published_kst"] and is_today_kst(datetime.fromisoformat(art["published_kst"]), today_kst):
                results.append(art)
        except Exception as e:
            # 개별 기사 실패는 계속 진행
            # print(f"[WARN] fail {url}: {e}")
            pass
        time.sleep(sleep_sec)  # 예의상 천천히
    return results

if __name__ == "__main__":
    today_kst = datetime.now(KST)
    items = crawl_today(today_kst=today_kst, limit=50, sleep_sec=0.7)
    print(json.dumps({
        "date_kst": today_kst.strftime("%Y-%m-%d"),
        "count": len(items),
        "items": items
    }, ensure_ascii=False, indent=2))
