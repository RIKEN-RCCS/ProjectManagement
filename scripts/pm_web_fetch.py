#!/usr/bin/env python3
"""
pm_web_fetch.py — 外部Webページ・RSSフィードを取得し data/web_articles.db に保存する。

web_sources.yaml に定義されたソースを処理し、キーワードフィルタを通過した記事のみ保存する。
保存したコンテンツは pm_embed.py --web-only で FTS5 インデックスに組み込まれ、
/argus-ask で検索可能になる。

使い方:
  python3 scripts/pm_web_fetch.py                    # 全ソース差分取得
  python3 scripts/pm_web_fetch.py --source "Top500"  # 特定ソースのみ
  python3 scripts/pm_web_fetch.py --dry-run          # 保存せず件数確認
  python3 scripts/pm_web_fetch.py --full-refetch     # 全件再取得（URL重複無視）
  python3 scripts/pm_web_fetch.py --list             # 保存済み記事一覧
"""

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.robotparser
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
WEB_SOURCES_YAML = DATA_DIR / "web_sources.yaml"
WEB_ARTICLES_DB = DATA_DIR / "web_articles.db"

FETCH_DELAY_SEC = 2.0
REQUEST_TIMEOUT = 20
MAX_CONTENT_CHARS = 8000

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; pm_web_fetch/1.0; +https://github.com/RIKEN-RCCS/ProjectManagement)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.9",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name   TEXT NOT NULL,
    url           TEXT NOT NULL UNIQUE,
    title         TEXT,
    published_at  TEXT,
    fetched_at    TEXT NOT NULL,
    content       TEXT,
    summary       TEXT,
    target_indices TEXT
);

CREATE TABLE IF NOT EXISTS fetch_state (
    source_name TEXT PRIMARY KEY,
    last_fetched TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_articles_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def load_web_sources(config_path: Path) -> list[dict]:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return [s for s in (cfg.get("sources") or []) if s.get("enabled", True)]


def is_relevant(text: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    t = text.lower()
    return any(k.lower() in t for k in keywords)


def _http_get(url: str, timeout: int = REQUEST_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def check_robots(url: str) -> bool:
    """robots.txt を確認し、User-Agent pm_web_fetch のクロールが許可されているか返す。"""
    parsed = urllib.parse.urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
        return rp.can_fetch("pm_web_fetch", url)
    except Exception:
        return True  # 取得失敗時は許可とみなす


# --- RSS / Atom パーサー ---

_NS_ATOM = "http://www.w3.org/2005/Atom"
_NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"
_NS_DC = "http://purl.org/dc/elements/1.1/"


def _text(elem, *tags) -> str:
    for tag in tags:
        child = elem.find(tag)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def fetch_rss(url: str, timeout: int = REQUEST_TIMEOUT) -> list[dict]:
    """RSS 2.0 / Atom フィードを取得し記事リストを返す。"""
    raw = _http_get(url, timeout)
    root = ET.fromstring(raw)

    articles: list[dict] = []

    # Atom
    if root.tag == f"{{{_NS_ATOM}}}feed" or "Atom" in root.tag:
        for entry in root.findall(f"{{{_NS_ATOM}}}entry"):
            title = _text(entry, f"{{{_NS_ATOM}}}title")
            link_el = entry.find(f"{{{_NS_ATOM}}}link[@rel='alternate']")
            if link_el is None:
                link_el = entry.find(f"{{{_NS_ATOM}}}link")
            link = link_el.get("href", "") if link_el is not None else ""
            published = _text(entry, f"{{{_NS_ATOM}}}published", f"{{{_NS_ATOM}}}updated")
            summary = _text(entry, f"{{{_NS_ATOM}}}summary", f"{{{_NS_ATOM}}}content")
            if title or link:
                articles.append({"title": title, "url": link,
                                  "published_at": published[:10] if published else None,
                                  "summary": summary[:2000] if summary else ""})
        return articles

    # RSS 2.0
    channel = root.find("channel")
    if channel is None:
        channel = root

    for item in channel.findall("item"):
        title = _text(item, "title")
        link = _text(item, "link")
        pub_date = _text(item, "pubDate")
        description = _text(item, "description")
        content = item.find(f"{{{_NS_CONTENT}}}encoded")
        content_text = content.text.strip() if (content is not None and content.text) else ""

        # 日付を YYYY-MM-DD に正規化
        published_at = None
        if pub_date:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(pub_date)
                published_at = dt.strftime("%Y-%m-%d")
            except Exception:
                published_at = pub_date[:10] if len(pub_date) >= 10 else None

        summary = description or content_text
        # HTMLタグを除去
        summary = re.sub(r"<[^>]+>", " ", summary).strip()
        summary = re.sub(r"\s+", " ", summary)[:2000]

        if title or link:
            articles.append({"title": title, "url": link,
                              "published_at": published_at,
                              "summary": summary})

    return articles


# --- HTML フェッチ ---

def _extract_text_lxml(html_bytes: bytes) -> str:
    """lxml でHTMLからボイラープレートを除去してテキストを抽出する。"""
    try:
        import lxml.html
        try:
            import lxml_html_clean
            cleaner = lxml_html_clean.Cleaner(
                scripts=True, javascript=True, style=True,
                links=False, meta=False, page_structure=False,
                processing_instructions=True, embedded=True,
                frames=True, forms=False, annoying_tags=True,
                remove_unknown_tags=False, safe_attrs_only=False,
            )
            doc = lxml.html.fromstring(html_bytes)
            doc = cleaner.clean_html(doc)
        except ImportError:
            doc = lxml.html.fromstring(html_bytes)
        return doc.text_content()
    except ImportError:
        return _extract_text_stdlib(html_bytes)


def _extract_text_stdlib(html_bytes: bytes) -> str:
    """stdlib html.parser で <p> / <li> テキストを抽出するフォールバック。"""
    from html.parser import HTMLParser

    class _Extractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self._skip = False
            self.parts: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "nav", "footer", "header"):
                self._skip = True

        def handle_endtag(self, tag):
            if tag in ("script", "style", "nav", "footer", "header"):
                self._skip = False

        def handle_data(self, data):
            if not self._skip:
                t = data.strip()
                if t:
                    self.parts.append(t)

    parser = _Extractor()
    parser.feed(html_bytes.decode("utf-8", errors="replace"))
    return " ".join(parser.parts)


def fetch_html_article(url: str, timeout: int = REQUEST_TIMEOUT) -> str:
    """単一HTMLページのテキスト本文を取得する。"""
    raw = _http_get(url, timeout)
    text = _extract_text_lxml(raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_CONTENT_CHARS]


def fetch_html_index(source: dict, timeout: int = REQUEST_TIMEOUT) -> list[dict]:
    """HTMLインデックスページからリンクを抽出し、各記事を取得する。"""
    url = source["url"]
    link_pattern = re.compile(source.get("link_pattern", ""), re.IGNORECASE)
    max_articles = source.get("max_articles", 50)
    keywords = source.get("keywords", [])

    raw = _http_get(url, timeout)
    try:
        import lxml.html
        doc = lxml.html.fromstring(raw)
        doc.make_links_absolute(url)
        hrefs = [a.get("href", "") for a in doc.findall(".//a") if a.get("href")]
    except ImportError:
        # フォールバック: 正規表現でリンク抽出
        hrefs_raw = re.findall(r'href=["\']([^"\']+)["\']', raw.decode("utf-8", errors="replace"))
        hrefs = [urllib.parse.urljoin(url, h) for h in hrefs_raw]

    # パターンでフィルタ & 重複排除
    seen: set[str] = set()
    filtered: list[str] = []
    for href in hrefs:
        path = urllib.parse.urlparse(href).path
        if link_pattern.search(path) and href not in seen:
            seen.add(href)
            filtered.append(href)
        if len(filtered) >= max_articles * 3:
            break

    articles: list[dict] = []
    for article_url in filtered[:max_articles * 3]:
        if len(articles) >= max_articles:
            break
        try:
            if not check_robots(article_url):
                continue
            time.sleep(FETCH_DELAY_SEC)
            content = fetch_html_article(article_url, timeout)
            # タイトルを本文先頭から推定（簡易）
            title_match = re.search(r"([^\n。．]{10,80})", content)
            title = title_match.group(1).strip() if title_match else article_url
            if not is_relevant(title + " " + content, keywords):
                continue
            articles.append({
                "title": title,
                "url": article_url,
                "published_at": None,
                "summary": content[:500],
                "content": content,
            })
        except Exception as e:
            logging.getLogger("pm_web_fetch").warning(f"  記事取得失敗: {article_url} — {e}")

    return articles


# --- ソース処理 ---

def process_source(
    source: dict,
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    full_refetch: bool = False,
    logger: logging.Logger,
) -> int:
    name = source["name"]
    url = source["url"]
    src_type = source.get("type", "rss")
    keywords = source.get("keywords", [])
    max_articles = source.get("max_articles", 50)
    target_indices = json.dumps(source.get("target_indices", []))

    logger.info(f"  [{name}] 取得中: {url}")

    if not check_robots(url):
        logger.warning(f"  [{name}] robots.txt により拒否されました。スキップします")
        return 0

    try:
        if src_type == "rss":
            articles = fetch_rss(url)
        elif src_type == "html_index":
            articles = fetch_html_index(source)
        else:
            logger.warning(f"  [{name}] 未対応のtype: {src_type}")
            return 0
    except Exception as e:
        logger.error(f"  [{name}] 取得エラー: {e}")
        return 0

    now = now_iso()
    saved = 0

    for article in articles:
        article_url = article.get("url", "").strip()
        if not article_url:
            continue

        title = article.get("title", "")
        summary = article.get("summary", "")
        content = article.get("content", "")

        # キーワードフィルタ
        check_text = f"{title} {summary} {content}"
        if not is_relevant(check_text, keywords):
            continue

        if dry_run:
            logger.info(f"    [DRY-RUN] {title[:60]} ({article_url})")
            saved += 1
            if saved >= max_articles:
                break
            continue

        try:
            if full_refetch:
                conn.execute(
                    """INSERT OR REPLACE INTO articles
                       (source_name, url, title, published_at, fetched_at, content, summary, target_indices)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (name, article_url, title, article.get("published_at"),
                     now, content[:MAX_CONTENT_CHARS], summary, target_indices),
                )
            else:
                conn.execute(
                    """INSERT OR IGNORE INTO articles
                       (source_name, url, title, published_at, fetched_at, content, summary, target_indices)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (name, article_url, title, article.get("published_at"),
                     now, content[:MAX_CONTENT_CHARS], summary, target_indices),
                )
            if conn.execute("SELECT changes()").fetchone()[0] > 0:
                saved += 1
                logger.info(f"    保存: {title[:60]}")
        except Exception as e:
            logger.warning(f"    保存失敗: {article_url} — {e}")

        if saved >= max_articles:
            break

        if src_type == "rss":
            time.sleep(0.2)  # RSS では短いsleep

    if not dry_run:
        conn.execute(
            "INSERT OR REPLACE INTO fetch_state (source_name, last_fetched) VALUES (?,?)",
            (name, now),
        )
        conn.commit()

    logger.info(f"  [{name}] 完了: {saved} 件{'（DRY-RUN）' if dry_run else ''}")
    return saved


def list_articles(conn: sqlite3.Connection, index_name: str | None = None) -> None:
    where = ""
    params: list = []
    if index_name:
        where = "WHERE target_indices LIKE ?"
        params.append(f'%"{index_name}"%')

    rows = conn.execute(
        f"""SELECT source_name, title, url, published_at, fetched_at
            FROM articles {where}
            ORDER BY fetched_at DESC LIMIT 100""",
        params,
    ).fetchall()

    if not rows:
        print("登録済み記事はありません。")
        return

    print(f"{'ソース':<20} {'タイトル':<50} {'公開日':<12} {'取得日':<20}")
    print("-" * 110)
    for r in rows:
        title = (r["title"] or "")[:48]
        print(f"{(r['source_name'] or ''):<20} {title:<50} {(r['published_at'] or ''):<12} {r['fetched_at'][:19]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="外部WebコンテンツをFetch して web_articles.db に保存する")
    parser.add_argument("--source", help="特定ソースのみ処理（web_sources.yaml の name 値）")
    parser.add_argument("--dry-run", action="store_true", help="DB保存なし・件数確認のみ")
    parser.add_argument("--full-refetch", action="store_true", help="全件再取得（既存URLも上書き）")
    parser.add_argument("--list", action="store_true", help="保存済み記事一覧を表示して終了")
    parser.add_argument("--index-name", help="--list 時のフィルタ")
    parser.add_argument("--config", default=str(WEB_SOURCES_YAML), help="web_sources.yaml のパス")
    parser.add_argument("--data-dir", default=str(DATA_DIR), help="data/ ディレクトリのパス")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("pm_web_fetch")

    data_dir = Path(args.data_dir)
    config_path = Path(args.config)
    db_path = data_dir / "web_articles.db"

    if not config_path.exists():
        logger.error(f"web_sources.yaml が見つかりません: {config_path}")
        sys.exit(1)

    conn = open_articles_db(db_path)

    if args.list:
        list_articles(conn, args.index_name)
        conn.close()
        return

    sources = load_web_sources(config_path)
    if args.source:
        sources = [s for s in sources if s["name"] == args.source]
        if not sources:
            logger.error(f"ソース '{args.source}' が見つかりません")
            logger.error(f"定義済み: {[s['name'] for s in load_web_sources(config_path)]}")
            sys.exit(1)

    if args.dry_run:
        logger.info("[DRY-RUN] DB保存は行いません")

    total = 0
    for source in sources:
        total += process_source(
            source, conn,
            dry_run=args.dry_run,
            full_refetch=args.full_refetch,
            logger=logger,
        )
        if source.get("type") == "rss":
            time.sleep(FETCH_DELAY_SEC)

    if args.dry_run:
        logger.info(f"\n[DRY-RUN] 合計 {total} 件（DB保存なし）")
    else:
        total_db = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        logger.info(f"\n完了: 今回 {total} 件取得 / DB合計 {total_db} 件")

    conn.close()


if __name__ == "__main__":
    main()
