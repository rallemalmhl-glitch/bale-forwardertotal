#!/usr/bin/env python3
"""انتقال پست‌های کانال عمومی تلگرام به کانال بله (اجرا روی GitHub Actions).

- پست‌ها از صفحه‌ی عمومی https://t.me/s/<channel> خوانده می‌شوند (بدون نیاز به API تلگرام).
- متن تبلیغ ربات پیش‌بینی حذف می‌شود.
- پست‌های تبلیغاتی (به‌ویژه شرط‌بندی) ارسال نمی‌شوند.
- آخرین پست پردازش‌شده در state.json ذخیره می‌شود تا پست تکراری ارسال نشود.
"""
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

TG_CHANNEL = os.getenv("TG_CHANNEL", "total_fut").strip().lstrip("@")
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
FIRST_RUN_SEND = int(os.getenv("FIRST_RUN_SEND", "0") or 0)
EXTRA_AD_KEYWORDS = [k.strip() for k in os.getenv("EXTRA_AD_KEYWORDS", "").split(",") if k.strip()]
MAX_MEDIA_BYTES = 45 * 1024 * 1024
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# ───────────────────────── حذف متن ثابت ─────────────────────────

# اگر هر خطی این عبارت‌ها را (بعد از نرمال‌سازی) داشته باشد، کل خط حذف می‌شود.
REMOVE_LINE_KEYS = [
    "totalpishbinibot",
    "پیشبینیبازیهایلیگبرتر",
]

FOOTER_INLINE_RE = re.compile(
    r"🎖️?\s*پیش[\u200c\s]*بینی[\u200c\s]+بازی[\u200c\s]*های[\u200c\s]+لیگ[\u200c\s]+برتر"
    r"[^\n|]*\|?\s*@?Total_pishbinibot",
    re.IGNORECASE,
)


def squash(s: str) -> str:
    """نرمال‌سازی برای مقایسه: حذف فاصله/نیم‌فاصله، یکسان‌سازی ی و ک، حروف کوچک."""
    s = s.replace("ي", "ی").replace("ك", "ک")
    s = re.sub(r"[\u200c\u200d\u200e\u200f\ufe0f\s]+", "", s)
    return s.lower()


def clean_text(text: str) -> str:
    text = FOOTER_INLINE_RE.sub("", text)
    kept = []
    for line in text.split("\n"):
        n = squash(line)
        if any(k in n for k in REMOVE_LINE_KEYS):
            continue
        kept.append(line.rstrip())
    out = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# ───────────────────────── تشخیص تبلیغ ─────────────────────────

AD_PATTERNS = [
    r"(?<![\u0600-\u06FF])بت(?![\u0600-\u06FF])",  # کلمه‌ی مستقل «بت» (نه «ثبت»)
    r"شرط[\u200c\s]*بند",
    r"بتینگ",
    r"کازینو",
    r"بونوس",
    r"کد[\u200c\s]*پروموشن",
    r"کد[\u200c\s]*معرف",
    r"برداشت[\u200c\s]*آنی",
    r"سایت[\u200c\s]*شرط",
    r"تبلیغ",
    r"1xbet|bet365|betwinner|melbet|mostbet|1win|pin-?up|parimatch|linebet|22bet|betting|casino",
    r"\bbonus\b|promo[\s_-]?code",
]
AD_RE = re.compile("|".join(AD_PATTERNS), re.IGNORECASE)
AD_HOST_RE = re.compile(r"bet|1win|pinup|casino|gambl|parimatch", re.IGNORECASE)


def is_ad(text: str, links: list[str]) -> bool:
    t = text.replace("ي", "ی").replace("ك", "ک")
    if AD_RE.search(t):
        return True
    sq = squash(t)
    if any(squash(k) in sq for k in EXTRA_AD_KEYWORDS):
        return True
    for href in links:
        host = urlparse(href).netloc
        if host and host not in ("t.me", "telegram.me") and AD_HOST_RE.search(host):
            return True
    return False


# ───────────────────────── خواندن از تلگرام ─────────────────────────


@dataclass
class Post:
    id: int
    text: str = ""
    links: list = field(default_factory=list)
    photos: list = field(default_factory=list)
    videos: list = field(default_factory=list)
    audios: list = field(default_factory=list)


def element_to_text(el) -> tuple[str, list[str]]:
    if el is None:
        return "", []
    links = []
    for br in el.find_all("br"):
        br.replace_with("\n")
    for a in el.find_all("a", href=True):
        href = a["href"]
        label = a.get_text().strip()
        if href.startswith("http"):
            links.append(href)
            if label and not label.startswith(("@", "#", "http")) and "t.me" not in href:
                a.replace_with(f"{label} ({href})")
    return el.get_text().strip(), links


def fetch_posts() -> list[Post]:
    r = requests.get(f"https://t.me/s/{TG_CHANNEL}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    posts = []
    for w in soup.select("div.tgme_widget_message"):
        data_post = w.get("data-post", "")
        if "/" not in data_post:
            continue
        try:
            pid = int(data_post.split("/")[-1])
        except ValueError:
            continue
        text_el = w.select_one("div.tgme_widget_message_text.js-message_text") or None
        text, links = element_to_text(text_el)
        p = Post(id=pid, text=text, links=links)
        for a in w.select("a.tgme_widget_message_photo_wrap"):
            m = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", a.get("style", ""))
            if m:
                p.photos.append(m.group(1))
        p.videos = [v["src"] for v in w.select("video[src]")]
        p.audios = [a["src"] for a in w.select("audio[src]")]
        posts.append(p)
    return posts


# ───────────────────────── ارسال به بله ─────────────────────────


class Bale:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://tapi.bale.ai/bot{token}"
        self.chat_id = chat_id

    def call(self, method: str, data=None, files=None, retries=3):
        data = dict(data or {})
        data["chat_id"] = self.chat_id
        for attempt in range(1, retries + 1):
            r = requests.post(f"{self.base}/{method}", data=data, files=files, timeout=180)
            try:
                j = r.json()
            except ValueError:
                j = {}
            if r.ok and j.get("ok", True):
                return j
            if r.status_code == 429:
                wait = j.get("parameters", {}).get("retry_after", 5)
                time.sleep(min(int(wait), 60))
                continue
            if attempt == retries:
                raise RuntimeError(f"{method} failed [{r.status_code}]: {r.text[:300]}")
            time.sleep(2 * attempt)

    def send_text(self, text: str):
        for i in range(0, len(text), 4000):
            self.call("sendMessage", {"text": text[i : i + 4000]})

    def send_media(self, kind: str, url: str, caption: str = ""):
        """kind: photo | video | audio. فایل دانلود و آپلود می‌شود."""
        resp = requests.get(url, headers=HEADERS, timeout=120, stream=True)
        resp.raise_for_status()
        size = int(resp.headers.get("Content-Length", 0))
        if size > MAX_MEDIA_BYTES:
            raise RuntimeError("media too large")
        content = resp.content
        if len(content) > MAX_MEDIA_BYTES:
            raise RuntimeError("media too large")
        ext = {"photo": "jpg", "video": "mp4", "audio": "ogg"}[kind]
        method = {"photo": "sendPhoto", "video": "sendVideo", "audio": "sendAudio"}[kind]
        data = {"caption": caption} if caption else {}
        try:
            self.call(method, data, files={kind: (f"file.{ext}", content)})
        except Exception as e:
            print(f"  {method} failed ({e}); trying sendDocument")
            self.call("sendDocument", data, files={"document": (f"file.{ext}", content)})


def send_post(bale: Bale, post: Post, text: str):
    media = (
        [("photo", u) for u in post.photos]
        + [("video", u) for u in post.videos]
        + [("audio", u) for u in post.audios]
    )
    sent_any = False
    caption_fits = len(text) <= 1000
    for i, (kind, url) in enumerate(media):
        last = i == len(media) - 1
        cap = text if (last and caption_fits) else ""
        try:
            bale.send_media(kind, url, cap)
            sent_any = True
            if last and caption_fits:
                return
        except Exception as e:  # اگر مدیا نرفت، حداقل متن ارسال شود
            print(f"  media skipped: {e}")
    if text:
        bale.send_text(text)
    elif not sent_any and media:
        print("  nothing could be sent for this post")


# ───────────────────────── حالت و اجرای اصلی ─────────────────────────


def load_last_id():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text()).get("last_id")
        except ValueError:
            return None
    return None


def save_last_id(last_id: int):
    STATE_FILE.write_text(json.dumps({"last_id": last_id}, indent=2) + "\n")


def merge_albums(posts):
    """چند پست پشت‌سرهم با متن یکسان (آلبوم عکس/ویدیوی تلگرام) را در یک
    پست ادغام می‌کند تا کپشن مشترک فقط یک‌بار فرستاده شود."""
    merged = []
    i, n = 0, len(posts)
    while i < n:
        p = posts[i]
        text = clean_text(p.text)
        group_ids = [p.id]
        photos, videos, audios = list(p.photos), list(p.videos), list(p.audios)
        j = i + 1
        while j < n and text and clean_text(posts[j].text) == text:
            group_ids.append(posts[j].id)
            photos.extend(posts[j].photos)
            videos.extend(posts[j].videos)
            audios.extend(posts[j].audios)
            j += 1
        rep = Post(id=p.id, text=p.text, links=p.links, photos=photos, videos=videos, audios=audios)
        merged.append((group_ids, rep))
        i = j
    return merged


def main() -> int:
    token = os.environ.get("BALE_BOT_TOKEN")
    chat_id = os.environ.get("BALE_CHAT_ID")
    if not token or not chat_id:
        print("BALE_BOT_TOKEN and BALE_CHAT_ID must be set", file=sys.stderr)
        return 1

    posts = sorted(fetch_posts(), key=lambda p: p.id)
    if not posts:
        print("no posts found (channel not public, or page layout changed?)")
        return 0

    last_id = load_last_id()
    if last_id is None:
        ids = [p.id for p in posts]
        n = FIRST_RUN_SEND
        last_id = ids[-n - 1] if 0 < n < len(ids) else (ids[-1] if n == 0 else 0)
        save_last_id(last_id)
        print(f"first run: baseline last_id={last_id}")

    bale = Bale(token, chat_id)
    new_posts = [p for p in posts if p.id > last_id]
    groups = merge_albums(new_posts)
    print(f"{len(new_posts)} new post(s) in {len(groups)} group(s)")

    for group_ids, p in groups:
        text = clean_text(p.text)
        has_media = bool(p.photos or p.videos or p.audios)
        if len(group_ids) > 1:
            print(f"#{group_ids[0]}-{group_ids[-1]}: album of {len(group_ids)} merged")
        if is_ad(text, p.links):
            print(f"#{p.id}: ad -> skipped")
        elif not text and not has_media:
            print(f"#{p.id}: empty after cleaning -> skipped")
        else:
            try:
                send_post(bale, p, text)
                print(f"#{p.id}: sent")
            except Exception as e:
                print(f"#{p.id}: FAILED ({e}); will retry next run", file=sys.stderr)
                return 1
        last_id = max(group_ids)
        save_last_id(last_id)
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
