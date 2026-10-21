#!/usr/bin/env python3
"""
Eitaa public-channel -> Telegram bridge.
Best-effort HTML scraper; Eitaa has no documented stable public-channel scraping API.
"""
import os
import re
import json
import time
import hashlib
import logging
import mimetypes
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bridge")

EITAA_URL = os.getenv("EITAA_URL", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [x.strip() for x in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if x.strip()]
STATE_PATH = Path(os.getenv("STATE_FILE", "data/seen.json"))
MAX_MEDIA_BYTES = int(os.getenv("MAX_MEDIA_MB", "49")) * 1024 * 1024
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "35"))
MAX_POSTS = int(os.getenv("MAX_POSTS_PER_RUN", "30"))
SEND_EXISTING = os.getenv("SEND_EXISTING_ON_FIRST_RUN", "true").strip().lower() in {"1","true","yes","on"}
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in {"1","true","yes","on"}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "fa,en-US;q=0.8,en;q=0.7",
})

MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".mp4", ".m4v", ".mov",
    ".webm", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".pdf", ".zip", ".rar",
    ".7z", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".apk",
    ".csv", ".mp3", ".3gp", ".amr", ".flac", ".mkv", ".heic",
}
MEDIA_HINTS = ("media", "file", "document", "photo", "video", "audio", "download", "attach")
SKIP_IMAGE_HINTS = ("avatar", "profile", "logo", "emoji", "sticker", "icon", "flag")


def canonical_channel_url(raw: str) -> str:
    if not raw:
        raise RuntimeError("EITAA_URL is missing.")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    u = urlparse(raw)
    if u.netloc.lower() not in {"eitaa.com", "www.eitaa.com"}:
        raise RuntimeError("EITAA_URL must be a public eitaa.com channel URL.")
    return raw.split("?")[0].rstrip("/")


def load_state():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        return {"version": 2, "initialized": False, "messages": {}}
    try:
        obj = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("State root is not an object")
        obj.setdefault("version", 2)
        obj.setdefault("initialized", False)
        obj.setdefault("messages", {})
        return obj
    except Exception:
        backup = STATE_PATH.with_suffix(".corrupt.json")
        try:
            STATE_PATH.replace(backup)
            log.warning("Unreadable state moved to %s; starting fresh", backup)
        except Exception:
            pass
        return {"version": 2, "initialized": False, "messages": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def normalize_text(text):
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def message_id_from_url(url, channel_name):
    try:
        path = unquote(urlparse(url).path).strip("/")
        bits = path.split("/")
        if len(bits) >= 2 and bits[0].lstrip("@").lower() == channel_name.lower():
            if bits[1].isdigit():
                return bits[1]
    except Exception:
        pass
    return None


def is_media_url(url, tag=None):
    if not url or url.startswith(("data:", "javascript:", "mailto:", "#")):
        return False
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    path = unquote(p.path).lower()
    ext = Path(path).suffix.lower()
    hint = (path + " " + (p.netloc or "")).lower()
    if any(x in hint for x in SKIP_IMAGE_HINTS):
        return False
    if ext in MEDIA_EXTS:
        return True
    if tag and tag.name in ("img", "video", "audio", "source"):
        return True
    if any(x in hint for x in MEDIA_HINTS):
        return True
    return False


def collect_media(container, page_url):
    found = []
    seen = set()
    # Explicit media elements first.
    for tag in container.find_all(["img", "video", "audio", "source"]):
        candidates = []
        for attr in ("src", "data-src", "data-original", "data-url", "poster"):
            val = tag.get(attr)
            if val:
                candidates.append(val)
        if tag.name == "video" or tag.name == "audio":
            for child in tag.find_all(["source"]):
                for attr in ("src", "data-src"):
                    if child.get(attr):
                        candidates.append(child.get(attr))
        for raw in candidates:
            url = urljoin(page_url, raw.strip())
            if is_media_url(url, tag) and url not in seen:
                seen.add(url)
                found.append(url)

    # Download links and media URLs embedded in anchors.
    for a in container.find_all("a", href=True):
        url = urljoin(page_url, a["href"].strip())
        if is_media_url(url, a) and url not in seen:
            seen.add(url)
            found.append(url)
    return found[:10]


def find_message_containers(soup, channel_name, page_url):
    """
    Discover message nodes from links like /channelname/123 and climb to a
    compact ancestor containing the post text/media. Eitaa's HTML is unofficial
    and markup may change, so several fallbacks are intentionally used.
    """
    by_id = {}
    for a in soup.find_all("a", href=True):
        mid = message_id_from_url(a["href"], channel_name)
        if not mid:
            continue
        node = a
        best = None
        # Prefer the smallest ancestor that contains content/media, not whole page.
        for _ in range(9):
            if node is None or getattr(node, "name", None) in ("body", "html"):
                break
            txt = normalize_text(node.get_text("\n", strip=True))
            media = collect_media(node, page_url)
            # A reasonable post usually has some text, media, or both.
            if (len(txt) >= 8 or media) and len(txt) < 12000:
                best = node
                if media or len(txt) >= 20:
                    break
            node = node.parent
        if best is None:
            best = a.parent
        # Keep the first/smallest candidate for a message ID.
        if mid not in by_id or len(str(best)) < len(str(by_id[mid])):
            by_id[mid] = best

    # Some pages use data attributes without a permalink.
    for node in soup.find_all(attrs={"data-message-id": True}):
        mid = str(node.get("data-message-id", "")).strip()
        if mid.isdigit() and mid not in by_id:
            by_id[mid] = node

    posts = []
    for mid, node in by_id.items():
        # Remove scripts/styles and common navigation/control elements.
        clone = BeautifulSoup(str(node), "html.parser")
        for junk in clone.find_all(["script", "style", "noscript", "button", "svg"]):
            junk.decompose()
        for el in clone.select(".date, .time, .message-date, .message-time, .views, .share"):
            el.decompose()
        txt = normalize_text(clone.get_text("\n", strip=True))
        media = collect_media(node, page_url)
        # Avoid accidentally forwarding only the permalink label.
        if len(txt) < 2 and not media:
            continue
        # Ignore likely page-level containers which accidentally captured many posts.
        if len(txt) > 12000:
            continue
        fingerprint = hashlib.sha256((txt + "\n" + "\n".join(media)).encode("utf-8")).hexdigest()
        posts.append({
            "id": str(mid),
            "text": txt,
            "media": media,
            "fingerprint": fingerprint,
            "source_url": f"https://eitaa.com/{channel_name}/{mid}",
        })
    # Deduplicate and sort oldest first, so first-run history is sent in order.
    unique = {}
    for p in posts:
        unique[p["id"]] = p
    return sorted(unique.values(), key=lambda p: int(p["id"]))[-MAX_POSTS:]


def fetch_posts(channel_url):
    r = SESSION.get(channel_url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    if len(r.text) < 500:
        raise RuntimeError(f"Eitaa returned unexpectedly short HTML ({len(r.text)} bytes).")
    soup = BeautifulSoup(r.text, "html.parser")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else "(no title)"
    log.info("Eitaa page title: %s", title)
    log.info("HTML length: %s", len(r.text))
    channel_name = urlparse(channel_url).path.strip("/").split("/")[0].lstrip("@")
    posts = find_message_containers(soup, channel_name, channel_url)
    log.info("Parsed %s Eitaa messages", len(posts))
    for p in posts:
        log.info("Message id=%s text_chars=%s media_count=%s", p["id"], len(p["text"]), len(p["media"]))
    if not posts:
        # Save diagnostic HTML in logs only as a short structural hint, never secrets.
        links = [a.get("href") for a in soup.find_all("a", href=True) if channel_name.lower() in a.get("href", "").lower()]
        log.error("No posts parsed. Matching links sample: %s", links[:15])
        raise RuntimeError("Could not parse any posts from the public Eitaa page. The page structure may have changed.")
    return posts


def tg_api(method, data=None, files=None):
    if DRY_RUN:
        log.info("[DRY_RUN] Telegram method=%s chat_id=%s", method, (data or {}).get("chat_id"))
        return {"ok": True, "result": {"message_id": 0}}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = SESSION.post(url, data=data or {}, files=files, timeout=REQUEST_TIMEOUT)
    try:
        body = response.json()
    except Exception:
        raise RuntimeError(f"Telegram {method} returned HTTP {response.status_code}: {response.text[:300]}")
    if response.status_code >= 400 or not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body}")
    return body


def send_text(chat_id, text, source_url=None):
    text = text.strip()
    # Telegram message text limit is 4096 chars. Split conservatively.
    if source_url and source_url not in text:
        text = (text + "\n\nمنبع: " + source_url).strip()
    if not text:
        return
    chunks = []
    while len(text) > 4000:
        cut = text.rfind("\n", 0, 4000)
        if cut < 1000:
            cut = 4000
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    for chunk in chunks:
        tg_api("sendMessage", {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"})


def download_media(url):
    r = SESSION.get(url, stream=True, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    declared = int(r.headers.get("Content-Length", "0") or 0)
    if declared and declared > MAX_MEDIA_BYTES:
        raise RuntimeError(f"Media exceeds configured size limit ({declared} bytes): {url}")
    content = bytearray()
    for part in r.iter_content(128 * 1024):
        if not part:
            continue
        content.extend(part)
        if len(content) > MAX_MEDIA_BYTES:
            raise RuntimeError(f"Media exceeds configured size limit ({MAX_MEDIA_BYTES} bytes): {url}")
    ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    name = Path(unquote(urlparse(url).path)).name or "eitaa_media"
    if "." not in name:
        ext = mimetypes.guess_extension(ctype) if ctype else None
        name += ext or ".bin"
    return bytes(content), name, ctype


def telegram_media_method(name, ctype, url):
    ext = Path(name).suffix.lower()
    path = urlparse(url).path.lower()
    if ctype.startswith("image/") or ext in {".jpg", ".jpeg", ".png", ".webp"}:
        # Telegram photo supports common formats; send unusual image formats as document.
        if ext in {".jpg", ".jpeg", ".png"} or ctype in {"image/jpeg", "image/png"}:
            return "sendPhoto", "photo"
        return "sendDocument", "document"
    if ctype.startswith("video/") or ext in {".mp4", ".m4v", ".mov"}:
        return "sendVideo", "video"
    if ctype.startswith("audio/") or ext in {".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac", ".amr"}:
        if ext in {".ogg", ".opus"}:
            return "sendVoice", "voice"
        return "sendAudio", "audio"
    if ext == ".gif" or "animation" in path:
        return "sendAnimation", "animation"
    return "sendDocument", "document"


def send_one_media(chat_id, url, caption=None):
    content, name, ctype = download_media(url)
    method, field = telegram_media_method(name, ctype, url)
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1000]
    with __import__("io").BytesIO(content) as bio:
        bio.name = name
        tg_api(method, data, files={field: (name, bio, ctype or "application/octet-stream")})


def deliver_post(chat_id, post):
    text = post.get("text", "").strip()
    media = post.get("media", [])
    source = post.get("source_url", "")
    # Keep original text intact even when there are media files; Telegram's caption
    # limit is shorter than its text-message limit, so send text as its own message.
    if text:
        send_text(chat_id, text, source)
    elif not media:
        send_text(chat_id, source or f"پست ایتا: {post['id']}")
    sent_media = 0
    for i, url in enumerate(media):
        try:
            send_one_media(chat_id, url, None)
            sent_media += 1
        except Exception:
            log.exception("Failed media download/send for post %s URL=%s", post["id"], url)
            # At least give the destination a usable source reference if direct media fails.
            try:
                tg_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"دریافت فایل پست ایتا ناموفق بود؛ لینک منبع:\n{source}\n{url}",
                    "disable_web_page_preview": "true",
                })
            except Exception:
                log.exception("Could not send fallback links for post %s", post["id"])
            raise
    log.info("Delivered post %s to %s (media %s/%s)", post["id"], chat_id, sent_media, len(media))


def main():
    global EITAA_URL
    EITAA_URL = canonical_channel_url(EITAA_URL)
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN secret is missing.")
    if not CHAT_IDS:
        raise RuntimeError("TELEGRAM_CHAT_IDS secret is missing (comma-separated chat IDs).")
    # Validate token and permissions before processing.
    if not DRY_RUN:
        me = tg_api("getMe", {})
        log.info("Telegram bot authenticated: @%s", me.get("result", {}).get("username", "?"))

    posts = fetch_posts(EITAA_URL)
    state = load_state()
    stored = state["messages"]
    errors = 0

    # On first run, process all posts currently visible; thereafter only unseen/changed posts.
    first_run = not state.get("initialized", False)
    if first_run and not SEND_EXISTING:
        for post in posts:
            stored[post["id"]] = {"fingerprint": post["fingerprint"], "sent_to": list(CHAT_IDS)}
        state["initialized"] = True
        save_state(state)
        log.info("First run configured to skip existing posts; saved %s visible IDs.", len(posts))
        return

    for post in posts:
        mid = post["id"]
        entry = stored.setdefault(mid, {"fingerprint": "", "sent_to": []})
        entry.setdefault("sent_to", [])
        # If post content changes, allow a new delivery. We cannot reliably delete old messages.
        if entry.get("fingerprint") and entry["fingerprint"] != post["fingerprint"]:
            log.info("Post %s fingerprint changed; retrying delivery to destinations.", mid)
            entry["sent_to"] = []
        entry["fingerprint"] = post["fingerprint"]
        stored[mid] = entry
        save_state(state)

        for chat_id in CHAT_IDS:
            if chat_id in entry["sent_to"]:
                continue
            try:
                deliver_post(chat_id, post)
                entry["sent_to"].append(chat_id)
                stored[mid] = entry
                save_state(state)
                log.info("Forwarded message %s to destination %s", mid, chat_id)
            except Exception:
                errors += 1
                log.exception("Failed forwarding message %s to %s", mid, chat_id)

    state["initialized"] = True
    state["last_run_epoch"] = int(time.time())
    save_state(state)
    if errors:
        raise RuntimeError(f"{errors} destination delivery attempt(s) failed. See logs above.")
    log.info("Forwarding run completed successfully.")


if __name__ == "__main__":
    main()
