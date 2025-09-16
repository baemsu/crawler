import json

import logging

from datetime import datetime, timezone

from zoneinfo import ZoneInfo

 

import azure.functions as func

import requests

from bs4 import BeautifulSoup

import feedparser

 

AI_FEED = "https://techcrunch.com/category/artificial-intelligence/feed/"

#KST = ZoneInfo("Asia/Seoul")

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    from datetime import timedelta, timezone
    KST = timezone(timedelta(hours=9))  # tzdata 미탑재 환경 대응

 

HEADERS = {

    "User-Agent": (

        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "

        "AppleWebKit/537.36 (KHTML, like Gecko) "

        "Chrome/125.0 Safari/537.36"

    )

}

 

def parse_date_to_kst(entry):

    # feedparser의 published_parsed는 UTC 기준 struct_time

    if hasattr(entry, "published_parsed") and entry.published_parsed:

        dt_utc = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

    elif hasattr(entry, "updated_parsed") and entry.updated_parsed:

        dt_utc = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

    else:

        return None

    return dt_utc.astimezone(KST)

 

def fetch_article_text(url, max_paragraphs=8):

    try:

        r = requests.get(url, headers=HEADERS, timeout=15)

        r.raise_for_status()

    except Exception as e:

        logging.warning(f"Fetch fail {url}: {e}")

        return ""

 

    soup = BeautifulSoup(r.text, "html.parser")

 

    # 1) 가장 일반적인 'article' 범위에서 p 태그 수집

    container = soup.find("article")

    if not container:

        # 2) 백업: 기사 본문으로 보이는 영역들 탐색

        for selector in [

            "div.article-content",

            "div[data-component='ArticleBody']",

            "main article",

            "main",

        ]:

            container = soup.select_one(selector)

            if container:

                break

    if not container:

        container = soup  # 최후의 보루

 

    paras = []

    for p in container.find_all("p"):

        text = p.get_text(" ", strip=True)

        if not text:

            continue

        # 불필요한 구독/저작권/뉴스레터 문구 간단 필터링

        lowered = text.lower()

        if "subscribe" in lowered or "newsletter" in lowered:

            continue

        paras.append(text)

 

    if not paras:

        return ""

 

    # 너무 길어지지 않게 앞쪽 단락만 사용

    return "\n\n".join(paras[:max_paragraphs])

 

def main(req: func.HttpRequest) -> func.HttpResponse:

    try:

        # ?date=YYYY-MM-DD (KST 기준). 없으면 오늘(KST).

        date_str = req.params.get("date")

        if date_str:

            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()

        else:

            target_date = datetime.now(KST).date()

 

        # ?n=숫자 (최대 기사 수 제한, 기본 10)

        try:

            n = int(req.params.get("n", "10"))

        except ValueError:

            n = 10

        n = max(1, min(n, 30))

 

        feed = feedparser.parse(AI_FEED)

 

        items = []

        for e in feed.entries:

            kst_dt = parse_date_to_kst(e)

            if not kst_dt:

                continue

            if kst_dt.date() == target_date:

                items.append(

                    {

                        "title": e.title,

                        "url": e.link,

                        "published_at_kst": kst_dt.isoformat(),

                    }

                )

 

        # 오늘자 없으면 최신 상위 n개로 대체

        if not items:

            for e in feed.entries[:n]:

                kst_dt = parse_date_to_kst(e)

                items.append(

                    {

                        "title": e.title,

                        "url": e.link,

                        "published_at_kst": kst_dt.isoformat() if kst_dt else None,

                    }

                )

 

        # 각 기사 페이지로 들어가서 본문 추출 (한 단계 딥다이브)

        results = []

        for item in items[:n]:

            body = fetch_article_text(item["url"])

            results.append(

                {

                    **item,

                    "content": body,

                }

            )

 

        body_json = json.dumps(

            {

                "date_kst": target_date.isoformat(),

                "count": len(results),

                "results": results,

                "source": {

                    "category": "TechCrunch AI",

                    "feed": AI_FEED,

                },

            },

            ensure_ascii=False,

            indent=2,

        )

 

        return func.HttpResponse(

            body_json,

            status_code=200,

            mimetype="application/json; charset=utf-8",

        )

    except Exception as ex:

        logging.exception("Unhandled error")

        return func.HttpResponse(

            json.dumps({"error": str(ex)}, ensure_ascii=False),

            status_code=500,

            mimetype="application/json; charset=utf-8",

        )
