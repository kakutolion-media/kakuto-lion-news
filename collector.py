#!/usr/bin/env python3
"""格闘ライオンNEWS — 格闘技ニュース自動収集・サイト生成（完全無料スタック）

RSS収集 → 正規化・分類・重複排除 → data/items.json 更新 → site/index.html 生成
cron で30分毎に実行する想定。外部ライブラリ不要（stdlibのみ）。
"""
import json
import hashlib
import html
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = Path(__file__).parent
DATA_FILE = BASE / "data" / "items.json"
SITE_FILE = BASE / "site" / "index.html"
MAX_ITEMS = 800
JST = timezone(timedelta(hours=9))

# (name, url, genre)  genre: boxing / mma / general
FEEDS = [
    ("ボクシングニュース", "https://boxingnews.jp/feed", "boxing"),
    ("MMAPLANET", "https://mmaplanet.jp/feed", "mma"),
    ("バトル・ニュース", "https://battle-news.com/?feed=rss2", "general"),
    ("ゴング格闘技", "https://gonkaku.jp/feed", "general"),
    ("GoogleNews:RIZIN", "https://news.google.com/rss/search?q=RIZIN&hl=ja&gl=JP&ceid=JP:ja", "mma"),
    ("GoogleNews:UFC", "https://news.google.com/rss/search?q=UFC&hl=ja&gl=JP&ceid=JP:ja", "mma"),
    ("GoogleNews:ボクシング", "https://news.google.com/rss/search?q=%E3%83%9C%E3%82%AF%E3%82%B7%E3%83%B3%E3%82%B0&hl=ja&gl=JP&ceid=JP:ja", "boxing"),
    ("GoogleNews:K-1", "https://news.google.com/rss/search?q=K-1%20%E6%A0%BC%E9%97%98%E6%8A%80&hl=ja&gl=JP&ceid=JP:ja", "general"),
    ("GoogleNews:井上尚弥", "https://news.google.com/rss/search?q=%E4%BA%95%E4%B8%8A%E5%B0%9A%E5%BC%A5&hl=ja&gl=JP&ceid=JP:ja", "boxing"),
    ("GoogleNews:朝倉未来", "https://news.google.com/rss/search?q=%E6%9C%9D%E5%80%89%E6%9C%AA%E6%9D%A5&hl=ja&gl=JP&ceid=JP:ja", "mma"),
]

RESULT_KW = ["勝利", "KO", "TKO", "判定", "一本勝ち", "王座", "防衛", "戴冠", "失神",
             "圧勝", "辛勝", "快勝", "完勝", "敗北", "敗れ", "破る", "下す", "撃破",
             "初黒星", "陥落", "勝ち名乗り", "ダウン奪", "秒殺"]
RUMOR_KW = ["噂", "浮上", "可能性", "示唆", "匂わせ", "意欲", "希望", "オファー",
            "交渉", "移籍", "接近", "揺れる", "去就", "電撃", "衝撃発言", "挑発",
            "舌戦", "場外", "波紋", "説", "か？", "かも", "prospect"]
INFO_KW = ["決定", "発表", "対戦カード", "出場", "参戦", "開催", "追加", "決定戦",
           "契約", "調印", "会見", "計量", "前日", "チケット", "放送", "配信",
           "日程", "タイトルマッチ", "正式"]
BREAKING_KW = ["速報", "緊急", "BREAKING"]
# ボクモバ型（ボクシング/MMA/キック中心）のためプロレス系は収集対象外
EXCLUDE_KW = ["プロレス", "ぷろれす", "プロレスリング", "新日本", "スターダム", "全日本プロレス",
              "DDT", "ノア", "NOAH", "マリーゴールド", "デスマッチ", "レスラー"]
# プロレス混在メディア（バトル・ニュース等）はこのカテゴリ/語を含む記事のみ採用
FIGHT_WHITELIST = ["ボクシング", "キック", "MMA", "総合格闘技", "RIZIN", "K-1", "UFC",
                   "ONE", "Krush", "RISE", "ベアナックル", "修斗", "パンクラス", "DEEP",
                   "空手", "柔術", "グラップリング", "アマチュア格闘技", "ムエタイ"]
MIXED_FEEDS = {"バトル・ニュース"}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (KakutoLionNews/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def text_of(el, tag):
    node = el.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


EXCERPT_MAX = 90  # 「引用」の範囲に収めるため要約は短く抑える（法務リスク低減）


def trim_excerpt(s: str) -> str:
    if not s:
        return s
    # 句点があれば最初の一文だけを使う（本文の実質的な再現を避ける）
    m = re.search(r"[。！？]", s[:EXCERPT_MAX + 20])
    if m and m.end() <= EXCERPT_MAX + 20:
        return s[:m.end()]
    return s[:EXCERPT_MAX].rstrip() + ("…" if len(s) > EXCERPT_MAX else "")


def classify(title: str, desc: str) -> str:
    t = title + " " + desc[:100]
    if any(k in title for k in BREAKING_KW):
        return "breaking"
    if any(k in t for k in RESULT_KW):
        return "result"
    if any(k in t for k in RUMOR_KW):
        return "rumor"
    if any(k in t for k in INFO_KW):
        return "info"
    return "news"


def norm_title(title: str) -> str:
    # Google Newsの「タイトル - 媒体名」から媒体名を落とし、記号・空白を除去して
    # 媒体横断（サンスポ/dメニュー等の同一配信記事）でも重複判定できる形にする
    t = re.sub(r"\s*[-–|]\s*[^-–|]{1,25}$", "", title)
    t = re.sub(r"[（(][^（）()]{1,25}[）)]\s*$", "", t)  # Yahoo転載の「（ゴング格闘技）」等
    t = re.sub(r"[【】\[\]「」『』〝〟“”\"'、。，．,.…・:：;；!！?？\s　]+", "", t.lower())
    return t[:60]


def parse_feed(name: str, raw: bytes, genre: str) -> list:
    items = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return items
    ns_atom = "{http://www.w3.org/2005/Atom}"
    channel = root.find("channel")
    entries = channel.findall("item") if channel is not None else root.findall(f"{ns_atom}entry")
    for it in entries[:40]:
        title = text_of(it, "title") or text_of(it, f"{ns_atom}title")
        link = text_of(it, "link")
        if not link:
            ln = it.find(f"{ns_atom}link")
            link = ln.get("href", "") if ln is not None else ""
        if not title or not link:
            continue
        # Google Newsは媒体名が<source>に入る
        src = text_of(it, "source") or name
        pub = text_of(it, "pubDate") or text_of(it, f"{ns_atom}updated")
        try:
            dt = parsedate_to_datetime(pub) if pub and "," in pub else datetime.fromisoformat(pub.replace("Z", "+00:00"))
        except Exception:
            dt = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        desc = trim_excerpt(strip_html(text_of(it, "description") or text_of(it, f"{ns_atom}summary")))
        title_clean = re.sub(r"\s*[-–|]\s*" + re.escape(src) + r"\s*$", "", strip_html(title))
        cats = " ".join(c.text or "" for c in it.findall("category"))
        haystack = f"{title_clean} {desc} {cats}"
        if name in MIXED_FEEDS and not any(k in haystack for k in FIGHT_WHITELIST):
            continue
        if any(k in haystack for k in EXCLUDE_KW):
            continue
        items.append({
            "id": hashlib.md5(norm_title(title).encode()).hexdigest()[:12],
            "title": title_clean,
            "link": link,
            "source": src,
            "genre": genre,
            "cat": classify(title_clean, desc),
            "desc": desc,
            "ts": dt.astimezone(JST).isoformat(),
        })
    return items


def collect() -> list:
    old = []
    if DATA_FILE.exists():
        # 正規化ルール変更時にも重複が残らないよう、既存分もidを再計算してマージ
        migrated = {}
        for i in json.loads(DATA_FILE.read_text()):
            i["id"] = hashlib.md5(norm_title(i["title"]).encode()).hexdigest()[:12]
            i["desc"] = trim_excerpt(i.get("desc", ""))
            migrated.setdefault(i["id"], i)
        old = list(migrated.values())
    seen = {i["id"] for i in old}
    added = 0
    for name, url, genre in FEEDS:
        try:
            for item in parse_feed(name, fetch(url), genre):
                if item["id"] not in seen:
                    seen.add(item["id"])
                    old.append(item)
                    added += 1
        except Exception as e:
            print(f"[warn] {name}: {e}", file=sys.stderr)
    old.sort(key=lambda i: i["ts"], reverse=True)
    old = old[:MAX_ITEMS]
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(old, ensure_ascii=False, indent=0))
    print(f"collected: +{added} new, total {len(old)}")
    return old


def build_site(items: list):
    data_json = json.dumps(items, ensure_ascii=False)
    updated = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    tpl = (BASE / "template.html").read_text()
    out = tpl.replace("__DATA__", data_json).replace("__UPDATED__", updated)
    SITE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SITE_FILE.write_text(out)
    print(f"site built: {SITE_FILE} ({len(items)} items)")
    # 入口リンク集ページ（bio用ハブ）を site/link/index.html に配置。
    # 元ファイル link_page.html はリポジトリ管理下。site/ はgitignoreのためCIで毎回コピーする。
    link_src = BASE / "link_page.html"
    if link_src.exists():
        link_dst = BASE / "site" / "link" / "index.html"
        link_dst.parent.mkdir(parents=True, exist_ok=True)
        link_dst.write_text(link_src.read_text())
        print(f"link hub built: {link_dst}")


if __name__ == "__main__":
    build_site(collect())
