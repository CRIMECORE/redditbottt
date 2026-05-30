import asyncio
import os
import sys
import json
import base64
import logging
import urllib.parse
import datetime
import xml.etree.ElementTree as ET
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from supabase import create_client, Client as SupabaseClient

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from subreddits import CATEGORIES, ALL_SUBS
from profile import CREATOR_PROFILE, COMPETITORS, OUR_ACCOUNTS

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TELEGRAM_TOKEN          = os.getenv("TELEGRAM_TOKEN")
SCRAPECREATORS_API_KEY  = os.getenv("SCRAPECREATORS_API_KEY")
OPENROUTER_API_KEY      = os.getenv("OPENROUTER_API_KEY")
SUPABASE_URL            = os.getenv("SUPABASE_URL")
SUPABASE_KEY            = os.getenv("SUPABASE_KEY")
SHEET_ID                = os.getenv("SHEET_ID")
GOOGLE_API_KEY          = os.getenv("GOOGLE_API_KEY")

_REQUIRED_ENV = [
    "TELEGRAM_TOKEN", "SCRAPECREATORS_API_KEY", "OPENROUTER_API_KEY",
    "SUPABASE_URL", "SUPABASE_KEY", "SHEET_ID", "GOOGLE_API_KEY",
]
_missing = [k for k in _REQUIRED_ENV if not os.getenv(k)]
if _missing:
    raise EnvironmentError(f"Отсутствуют переменные окружения: {', '.join(_missing)}")

SCRAPECREATORS_BASE = "https://api.scrapecreators.com/v1/reddit"
OPENROUTER_BASE     = "https://openrouter.ai/api/v1/chat/completions"
REDDIT_HEADERS      = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
FINDSUBS_KEYWORDS   = ["cosplay", "anime", "fetish", "gamer girl", "latex", "alternative", "goth", "NSFW"]

BANNED_SUBS = frozenset({"ResidentEvil", "DunderMifflin"})
BANNED_MODS = frozenset({"louise mania", "mad dickson", "pessimist", "rick spanish"})

# Subreddits targeting specific hair/looks we don't match — filter by name keyword
EXCLUDE_KEYWORDS = frozenset({"ginger", "redhead", "auburn", "brunette", "blonde_only"})
DRAWN_CONTENT_SUBS = frozenset({
    "AnimeArt", "hentai", "animeart", "GoneWildAnime", "AnimeGirlsNSFW",
    "anime", "AnimeGirls", "HentaiAi", "rule34", "rule34ai", "hentai_gif",
    "ecchi", "doujinshi", "Moescape", "AmateurRoomPorn",
})

# ── Daily plan: 3 tracked girls ───────────────────────────────────────────────
DAILY_TRACKED_GIRLS = ["fallenemoangel3", "llqpw", "BarelyVisibleTaboo"]

CALLBACK_DAILY_HOT     = "daily_hot"
CALLBACK_DAILY_GOOD    = "daily_good"
CALLBACK_DAILY_NEUTRAL = "daily_neutral"
CALLBACK_DAILY_READY   = "daily_ready"

_CACHE_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_cache.json")
_SUB_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sub_cache.json")
_SUB_CACHE_TTL  = 43200  # 12 hours — subreddit details don't change that fast

def _load_cache() -> dict[int, dict]:
    try:
        with open(_CACHE_FILE, encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except Exception:
        return {}

def _save_cache(cache: dict[int, dict]) -> None:
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in cache.items()}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("Failed to save daily cache: %s", e)

_daily_cache: dict[int, dict] = _load_cache()

def _load_sub_cache() -> dict[str, dict]:
    try:
        with open(_SUB_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_sub_cache() -> None:
    try:
        with open(_SUB_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_sub_details_cache, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("Failed to save sub cache: %s", e)

_sub_details_cache: dict[str, dict] = _load_sub_cache()

# Reverse map: subreddit_name_lower -> category name
_SUB_CATEGORY: dict[str, str] = {
    sub.lower(): cat for cat, subs in CATEGORIES.items() for sub in subs
}

BANGKOK_TZ    = pytz.timezone("Asia/Bangkok")
CHAT_IDS_FILE = Path(__file__).parent / "chat_ids.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
    encoding="utf-8",
)
logger = logging.getLogger(__name__)


# ─── Profile helpers ──────────────────────────────────────────────────────────

def _compact_profile() -> str:
    p = CREATOR_PROFILE
    content = "; ".join(p["content_types"])
    avoid   = "; ".join(p["avoid"])
    return (
        f"CREATOR: Blonde OnlyFans/Fansly model, wigs (multi-color), alt/egirl/goth/cosplay aesthetics. "
        f"Content: {content}. "
        f"Audience: men 18-35, anime/gaming/alt/fetish. Goal: $7-8k/mo Reddit traffic. "
        f"AVOID on Reddit: {avoid}."
    )


def build_system_prompt(role: str) -> str:
    return f"{role}\n\n{_compact_profile()}"


# ─── Account helpers ──────────────────────────────────────────────────────────

def _get_account(acc_type: str) -> dict:
    return next((a for a in OUR_ACCOUNTS if a["type"] == acc_type), {})


def _accounts_block() -> str:
    parts = []
    for acc in OUR_ACCOUNTS:
        parts.append(
            f"{acc['type']} аккаунт @{acc['username']}: "
            f"{acc['post_karma']:,} кармы постов. "
            f"Контент: {acc['content']}. "
            f"Использовать для: {acc['use_for']}."
        )
    return "НАШИ АККАУНТЫ:\n" + "\n".join(parts)


def _accounts_short() -> str:
    sfw  = _get_account("SFW")
    nsfw = _get_account("NSFW")
    return (
        f"SFW @{sfw.get('username')} ({sfw.get('post_karma',0):,} karma) | "
        f"NSFW @{nsfw.get('username')} ({nsfw.get('post_karma',0):,} karma)"
    )


# ─── Token usage tracking ─────────────────────────────────────────────────────

_tok: dict[str, int] = {"in": 0, "out": 0, "calls": 0}

def reset_tokens() -> None:
    _tok["in"] = _tok["out"] = _tok["calls"] = 0

def token_summary() -> str:
    total = _tok["in"] + _tok["out"]
    cost  = _tok["in"] * 3 / 1_000_000 + _tok["out"] * 15 / 1_000_000
    return (
        f"Токены: {_tok['in']:,} вх + {_tok['out']:,} исх = {total:,} всего\n"
        f"Стоимость: ~${cost:.4f} | Вызовов API: {_tok['calls']}"
    )


_profile_block = _compact_profile


# ─── Supabase ─────────────────────────────────────────────────────────────────

_sb: SupabaseClient | None = None

def get_sb() -> SupabaseClient:
    global _sb
    if _sb is None:
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _sb


def db_upsert_subreddits(rows: list[dict]) -> int:
    if not rows:
        return 0
    try:
        result = get_sb().table("subreddits").upsert(rows, on_conflict="name").execute()
        return len(result.data) if result.data else 0
    except Exception as e:
        logger.error("db_upsert_subreddits failed: %s", e)
        return 0


def db_get_active_sub_names() -> list[str]:
    try:
        result = (
            get_sb().table("subreddits")
            .select("name")
            .eq("active", True)
            .order("subscribers", desc=True)
            .execute()
        )
        return [row["name"] for row in (result.data or [])]
    except Exception as e:
        logger.error("db_get_active_sub_names failed: %s", e)
        return []


def db_seed_from_file() -> int:
    today = datetime.date.today().isoformat()
    rows = [
        {
            "name": sub,
            "category": _SUB_CATEGORY.get(sub.lower(), "other"),
            "subscribers": 0,
            "added_date": today,
            "active": True,
        }
        for sub in ALL_SUBS
    ]
    return db_upsert_subreddits(rows)


def db_check_ready() -> bool:
    try:
        get_sb().table("subreddits").select("name").limit(1).execute()
        return True
    except Exception:
        return False


# ─── Chat ID persistence ──────────────────────────────────────────────────────

def load_chat_ids() -> set[int]:
    if CHAT_IDS_FILE.exists():
        return set(json.loads(CHAT_IDS_FILE.read_text(encoding="utf-8")))
    return set()

def save_chat_id(chat_id: int) -> None:
    ids = load_chat_ids()
    ids.add(chat_id)
    CHAT_IDS_FILE.write_text(json.dumps(list(ids)), encoding="utf-8")


# ─── Reddit helpers ───────────────────────────────────────────────────────────

def fetch_reddit_posts(subreddit: str, limit: int = 5) -> list[dict]:
    url = f"{SCRAPECREATORS_BASE}/subreddit"
    headers = {"x-api-key": SCRAPECREATORS_API_KEY}
    params  = {"subreddit": subreddit, "type": "hot", "limit": limit}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    posts = data.get("posts") or data.get("data") or []
    return posts[:limit]


def fetch_top_posts_24h(subreddit: str, limit: int = 5) -> list[dict]:
    url = f"{SCRAPECREATORS_BASE}/subreddit"
    headers = {"x-api-key": SCRAPECREATORS_API_KEY}
    params  = {"subreddit": subreddit, "type": "top", "t": "day", "limit": limit}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    posts = data.get("posts") or data.get("data") or []
    return posts[:limit]


def fetch_top_posts_week(subreddit: str, limit: int = 10) -> list[dict]:
    url = f"{SCRAPECREATORS_BASE}/subreddit"
    headers = {"x-api-key": SCRAPECREATORS_API_KEY}
    params  = {"subreddit": subreddit, "type": "top", "t": "week", "limit": limit}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    posts = data.get("posts") or data.get("data") or []
    return posts[:limit]


def fetch_subreddit_rules(subreddit: str) -> str:
    """Return rules + description from cache (populated by fetch_subreddit_info)."""
    key    = subreddit.lower()
    now    = datetime.datetime.now(datetime.timezone.utc).timestamp()
    cached = _sub_details_cache.get(key)
    if not cached or (now - cached.get("_ts", 0)) >= _SUB_CACHE_TTL:
        fetch_subreddit_info(subreddit)
        cached = _sub_details_cache.get(key)
    if not cached:
        return "Rules unavailable"
    rules = cached.get("rules", "").strip()
    desc  = cached.get("description", "").strip()[:500]
    parts = []
    if rules: parts.append(rules)
    if desc:  parts.append(f"Описание: {desc}")
    return "\n\n".join(parts) if parts else "No rules found"


def fetch_subreddit_mods(subreddit: str) -> list[str]:
    try:
        r = requests.get(
            f"https://www.reddit.com/r/{subreddit}/about/moderators.json",
            headers=REDDIT_HEADERS, timeout=10,
        )
        data = r.json().get("data", {}).get("children", [])
        return [m.get("name", "").lower() for m in data]
    except Exception:
        return []


def fetch_subreddit_info(subreddit: str) -> dict:
    """Return subreddit details via ScrapeCreators with 12h persistent cache."""
    key    = subreddit.lower()
    now    = datetime.datetime.now(datetime.timezone.utc).timestamp()
    cached = _sub_details_cache.get(key)
    if cached and (now - cached.get("_ts", 0)) < _SUB_CACHE_TTL:
        return {"subscribers": cached.get("subscribers", 0), "online": cached.get("online", 0)}
    try:
        r = requests.get(
            f"{SCRAPECREATORS_BASE}/subreddit/details",
            headers={"x-api-key": SCRAPECREATORS_API_KEY},
            params={"subreddit": subreddit},
            timeout=10,
        )
        r.raise_for_status()
        data  = r.json()
        entry = {
            "subscribers": data.get("weekly_active_users") or 0,
            "online":      data.get("weekly_contributions") or 0,
            "rules":       (data.get("rules") or "").strip(),
            "description": (data.get("description") or "").strip(),
            "_ts":         now,
        }
        _sub_details_cache[key] = entry
        _save_sub_cache()
        return {"subscribers": entry["subscribers"], "online": entry["online"]}
    except Exception:
        return {"subscribers": 0, "online": 0}


def is_sub_allowed(sub: str) -> bool:
    if sub in BANNED_SUBS or sub.lower() in {s.lower() for s in BANNED_SUBS}:
        return False
    if sub in DRAWN_CONTENT_SUBS or sub.lower() in {s.lower() for s in DRAWN_CONTENT_SUBS}:
        return False
    mods = fetch_subreddit_mods(sub)
    if any(mod in BANNED_MODS for mod in mods):
        logger.info("Skipping r/%s — banned moderator found", sub)
        return False
    return True


def detect_video_policy(posts: list[dict]) -> str:
    video_count = sum(1 for p in posts if p.get("is_video") or "v.redd.it" in str(p.get("url", "")))
    if video_count >= 2:
        return "📹 Видео: можно постить напрямую"
    return "📹 Видео: используй Redgifs (прямые видео не работают в этом сабе)"


def avg_upvotes(posts: list[dict]) -> float:
    scores = [p.get("score") or p.get("ups") or 0 for p in posts]
    return sum(scores) / len(scores) if scores else 0


def extract_image_urls(posts: list[dict]) -> list[str]:
    urls = []
    for post in posts:
        url = post.get("url", "")
        if any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
            urls.append(url)
        elif post.get("preview"):
            try:
                img = post["preview"]["images"][0]["source"]["url"].replace("&amp;", "&")
                urls.append(img)
            except (KeyError, IndexError):
                pass
        if len(urls) >= 3:
            break
    return urls


def download_image_base64(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.ok and "image" in r.headers.get("content-type", ""):
            return base64.b64encode(r.content).decode()
    except Exception:
        pass
    return None


# ─── OpenRouter helpers ───────────────────────────────────────────────────────

def ask_openrouter(system_prompt: str, user_message: str, max_tokens: int = 1024) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://reddit-analyzer-bot",
        "X-Title": "Reddit Analyzer Bot",
    }
    payload = {
        "model": "anthropic/claude-sonnet-4-6",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ],
        "max_tokens": max_tokens,
    }
    r = requests.post(OPENROUTER_BASE, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    data  = r.json()
    usage = data.get("usage", {})
    _tok["in"]    += usage.get("prompt_tokens", 0)
    _tok["out"]   += usage.get("completion_tokens", 0)
    _tok["calls"] += 1
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise ValueError(f"Unexpected OpenRouter response: {data}") from e


def ask_openrouter_vision(images_b64: list[str], text_prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://reddit-analyzer-bot",
        "X-Title": "Reddit Analyzer Bot",
    }
    content: list[dict] = []
    for b64 in images_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    content.append({"type": "text", "text": text_prompt})
    payload = {
        "model": "google/gemini-2.0-flash-001",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 512,
    }
    r = requests.post(OPENROUTER_BASE, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    data  = r.json()
    usage = data.get("usage", {})
    _tok["in"]    += usage.get("prompt_tokens", 0)
    _tok["out"]   += usage.get("completion_tokens", 0)
    _tok["calls"] += 1
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise ValueError(f"Unexpected OpenRouter vision response: {data}") from e


# ─── Competitor helpers ───────────────────────────────────────────────────────

def fetch_competitor_posts(username: str, limit: int = 10) -> list[dict]:
    """Fetch recent posts by a competitor via RSS (Reddit JSON API returns 403)."""
    _NS    = "http://www.w3.org/2005/Atom"
    try:
        r = requests.get(
            f"https://www.reddit.com/user/{username}/submitted/.rss",
            headers=REDDIT_HEADERS,
            params={"sort": "new", "limit": limit},
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning("fetch_competitor_posts @%s HTTP %d", username, r.status_code)
            return []
        root  = ET.fromstring(r.content)
        posts = []
        for e in root.findall(f"{{{_NS}}}entry")[:limit]:
            cat   = e.find(f"{{{_NS}}}category")
            sub   = (cat.get("term") if cat is not None else None) or ""
            link  = e.find(f"{{{_NS}}}link")
            url   = (link.get("href") if link is not None else "") or ""
            title = e.findtext(f"{{{_NS}}}title") or ""
            pub   = e.findtext(f"{{{_NS}}}published") or ""
            try:
                ts = datetime.datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = 0
            posts.append({"subreddit": sub, "title": title, "url": url,
                          "created_utc": ts, "score": 0, "ups": 0})
        return posts
    except Exception as e:
        logger.warning("fetch_competitor_posts @%s failed: %s", username, e)
        return []


def fetch_user_posts_24h(username: str, limit: int = 25) -> list[dict]:
    """Fetch recent posts by a Reddit user via RSS, filtered to last 24 hours."""
    _NS     = "http://www.w3.org/2005/Atom"
    cutoff  = datetime.datetime.now(datetime.timezone.utc).timestamp() - 86400

    try:
        r = requests.get(
            f"https://www.reddit.com/user/{username}/submitted/.rss",
            headers=REDDIT_HEADERS,
            params={"sort": "new", "limit": limit},
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning("@%s RSS HTTP %d: %s", username, r.status_code, r.text[:200])
            return []

        root    = ET.fromstring(r.content)
        entries = root.findall(f"{{{_NS}}}entry")
        posts   = []
        for e in entries:
            pub = e.findtext(f"{{{_NS}}}published") or ""
            try:
                ts = datetime.datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if ts < cutoff:
                continue
            cat   = e.find(f"{{{_NS}}}category")
            sub   = (cat.get("term") if cat is not None else None) or ""
            if sub.startswith("u/"):       # RSS uses u/name; rest of code expects u_name
                sub = "u_" + sub[2:]
            link    = e.find(f"{{{_NS}}}link")
            url     = (link.get("href") if link is not None else "") or ""
            title   = e.findtext(f"{{{_NS}}}title") or ""
            rss_id  = e.findtext(f"{{{_NS}}}id") or ""          # e.g. "t3_1tiihmd"
            url_parts = url.split("/comments/")
            post_id = url_parts[1].split("/")[0] if len(url_parts) > 1 else rss_id.replace("t3_", "")
            posts.append({
                "subreddit":   sub,
                "title":       title,
                "url":         url,
                "post_id":     post_id,                          # e.g. "1tiihmd"
                "created_utc": ts,
                "score":       0,
                "ups":         0,
            })

        logger.info("@%s RSS: %d posts within 24h", username, len(posts))
        return posts

    except Exception as e:
        logger.warning("@%s RSS fetch failed: %s", username, e)
        return []


def db_save_competitor_insight(
    week_start: datetime.date,
    username: str,
    new_subreddits: list[str],
    top_subreddits: list[str],
    content_ideas: list[str],
    priority_subs: list[str],
    raw_analysis: str,
) -> None:
    try:
        get_sb().table("competitor_insights").insert({
            "week_start":        week_start.isoformat(),
            "competitor_username": username,
            "new_subreddits":    json.dumps(new_subreddits, ensure_ascii=False),
            "top_subreddits":    json.dumps(top_subreddits, ensure_ascii=False),
            "content_ideas":     json.dumps(content_ideas,  ensure_ascii=False),
            "priority_subs":     json.dumps(priority_subs,  ensure_ascii=False),
            "raw_analysis":      raw_analysis[:4000],
        }).execute()
        logger.info("Competitor insight saved for @%s", username)
    except Exception as e:
        logger.error("db_save_competitor_insight failed for @%s: %s", username, e)


def db_check_competitor_table() -> bool:
    try:
        get_sb().table("competitor_insights").select("id").limit(1).execute()
        return True
    except Exception:
        return False


# ─── Google Sheets ────────────────────────────────────────────────────────────

_SHEET_RANGE = urllib.parse.quote("'Reddit Stats — OF Analytics'!A:K")

def read_google_sheet(sheet_id: str) -> list[list[str]]:
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
        f"/values/{_SHEET_RANGE}?key={GOOGLE_API_KEY}"
    )
    masked_key = (GOOGLE_API_KEY[:10] + "…") if GOOGLE_API_KEY else "НЕ ЗАДАН"
    logger.info("Sheets request URL: %s", url.replace(GOOGLE_API_KEY or "", masked_key))
    r = requests.get(url, timeout=15)
    if not r.ok:
        logger.error("Sheets API error %s: %s", r.status_code, r.text[:300])
    r.raise_for_status()
    return r.json().get("values", [])


# ─── Formatting helpers ───────────────────────────────────────────────────────

def analyze_post_format(posts: list[dict]) -> str:
    videos = sum(1 for p in posts if
        p.get("is_video") or "video" in str(p.get("post_hint", "")).lower())
    images = sum(1 for p in posts if
        p.get("post_hint") == "image" or
        any(str(p.get("url", "")).lower().endswith(ext)
            for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")))
    total = len(posts)
    if total == 0:
        return "📷 Фото"
    if videos > images:
        return f"🎥 Видео ({videos}/{total} постов)"
    return f"📷 Фото ({max(images,1)}/{total} постов)"


def split_by_blocks(header: str, blocks: list[str], max_len: int = 3800) -> list[str]:
    messages: list[str] = []
    current = header
    for block in blocks:
        sep = "\n\n" if current else ""
        if len(current) + len(sep) + len(block) > max_len:
            if current:
                messages.append(current)
            current = block
        else:
            current = current + sep + block
    if current:
        messages.append(current)
    return messages or [header]


async def send_long_message(bot, chat_id: int, text: str) -> None:
    MAX = 4000
    if len(text) <= MAX:
        await bot.send_message(chat_id=chat_id, text=text)
        return

    chunks: list[str] = []
    current = ""

    for block in text.split("\n\n"):
        sep = "\n\n" if current else ""
        if len(current) + len(sep) + len(block) <= MAX:
            current += sep + block
        else:
            if current:
                chunks.append(current)
                current = ""
            for line in block.split("\n"):
                sep2 = "\n" if current else ""
                if len(current) + len(sep2) + len(line) <= MAX:
                    current += sep2 + line
                else:
                    if current:
                        chunks.append(current)
                        current = ""
                    for word in line.split(" "):
                        sep3 = " " if current else ""
                        if len(current) + len(sep3) + len(word) <= MAX:
                            current += sep3 + word
                        else:
                            if current:
                                chunks.append(current)
                            current = word

    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        await bot.send_message(chat_id=chat_id, text=chunk)
        if i < len(chunks) - 1:
            await asyncio.sleep(0.5)


# ─── Competitor analysis pipeline ────────────────────────────────────────────

async def run_competitor_analysis(known_subs: set[str]) -> str:
    now        = datetime.datetime.now(BANGKOK_TZ)
    week_start = now.date() - datetime.timedelta(days=now.weekday())
    known_lower = {s.lower() for s in known_subs}

    competitor_data: list[dict] = []
    for comp in COMPETITORS:
        username = comp["username"]
        notes    = comp["notes"]
        copy_ok  = comp.get("copy_style", True)
        logger.info("Fetching competitor posts: @%s", username)

        try:
            posts = await asyncio.to_thread(fetch_competitor_posts, username, 10)
        except Exception as e:
            logger.warning("@%s fetch failed: %s", username, e)
            posts = []

        used_subs: list[str] = list(dict.fromkeys(
            p.get("subreddit") or p.get("subreddit_name_prefixed", "").lstrip("r/")
            for p in posts
            if p.get("subreddit") or p.get("subreddit_name_prefixed")
        ))
        new_subs  = [s for s in used_subs if s.lower() not in known_lower]
        top_post  = max(posts, key=lambda p: p.get("score") or p.get("ups") or 0) if posts else None

        img_urls   = extract_image_urls(posts[:6])
        b64_images: list[str] = []
        for url in img_urls[:2]:
            b64 = await asyncio.to_thread(download_image_base64, url)
            if b64:
                b64_images.append(b64)

        vision_text = ""
        if b64_images:
            try:
                adapt_hint = (
                    "Внешность очень похожа на нас — разбери максимально детально: позы, ракурсы, свет, стиль кадрирования."
                    if copy_ok else
                    "Разбери стиль: позы, ракурсы, освещение, кадрирование, атмосфера. Что из этого можно воспроизвести?"
                )
                vision_text = await asyncio.to_thread(
                    ask_openrouter_vision,
                    b64_images,
                    f"Это посты блогера @{username} на Reddit для маркетинговой стратегии.\n{_compact_profile()}\n"
                    f"{adapt_hint}\n"
                    "За 3-4 предложения: какая поза, как поставлен свет, какой ракурс, какой общий стиль контента. "
                    "Конкретно что повторить в наших постах?",
                )
            except Exception as e:
                logger.warning("Vision failed for @%s: %s", username, e)
                vision_text = "Визуал недоступен."

        competitor_data.append({
            "username":   username,
            "notes":      notes,
            "copy_style": copy_ok,
            "posts":      posts,
            "used_subs":  used_subs,
            "new_subs":   new_subs,
            "top_post":   top_post,
            "vision":     vision_text,
        })

    if not competitor_data:
        return "📊 КОНКУРЕНТНЫЙ АНАЛИЗ\n\nДанные недоступны."

    comp_ctx_parts: list[str] = []
    for cd in competitor_data:
        top_posts_lines = "\n".join(
            f"  [{p.get('score') or p.get('ups', 0)} апв] r/{p.get('subreddit','?')}: "
            f"{p.get('title','')[:80]}"
            for p in sorted(cd["posts"], key=lambda p: p.get("score") or p.get("ups") or 0, reverse=True)[:5]
        ) or "  нет данных"
        copy_strategy = "копируем детально — внешность похожа" if cd["copy_style"] else "берём только стиль и сабреддиты"
        comp_ctx_parts.append(
            f"КОНКУРЕНТ: @{cd['username']}\n"
            f"Описание: {cd['notes']}\n"
            f"Стратегия: {copy_strategy}\n"
            f"Сабреддиты конкурента: {', '.join(cd['used_subs'][:12]) or 'нет данных'}\n"
            f"Новые сабы (нет в нашем пуле): {', '.join(cd['new_subs']) or 'нет новых'}\n"
            f"Топ постов недели:\n{top_posts_lines}\n"
            f"Визуальный анализ: {cd['vision'][:400] if cd['vision'] else 'нет'}"
        )

    sfw_acc  = _get_account("SFW")
    nsfw_acc = _get_account("NSFW")
    our_karma_ctx = (
        f"НАШИ АККАУНТЫ:\n"
        f"• SFW @{sfw_acc.get('username')}: {sfw_acc.get('post_karma',0):,} кармы\n"
        f"• NSFW @{nsfw_acc.get('username')}: {nsfw_acc.get('post_karma',0):,} кармы\n"
    )

    try:
        raw_analysis = await asyncio.to_thread(
            ask_openrouter,
            build_system_prompt(
                "Ты эксперт по конкурентному анализу OnlyFans/Reddit. "
                "Пиши на русском, структурированно и actionable.\n\n" + our_karma_ctx
            ),
            "ДАННЫЕ КОНКУРЕНТОВ:\n\n" + "\n\n---\n\n".join(comp_ctx_parts) + "\n\n"
            "Составь анализ СТРОГО в этом формате (не меняй порядок блоков):\n\n"
            "LEVEL3_START\n"
            "NEW_SUBS: sub1,sub2,sub3\n"
            "NICHES: ниша1; ниша2; ниша3\n"
            "IDEAS: идея1; идея2; идея3\n"
            "PRIORITY_SUBS: sub1,sub2,sub3\n"
            "LEVEL3_END\n\n"
            "LEVEL1_START\n"
            "## Прямые инсайты (копируем прямо сейчас)\n"
            "- Топ посты и апвоуты\n"
            "- Активные сабреддиты где они сейчас\n"
            "- Подписи/стиль подачи которые работают\n"
            "- Лучшее время постинга (если видно)\n"
            "LEVEL1_END\n\n"
            "LEVEL2_START\n"
            "## Стратегические выводы\n"
            "- Новые сабреддиты конкурентов которые нам стоит попробовать\n"
            "- Новые темы/ниши которые они осваивают\n"
            "- Где резкий рост апвоутов — горячая аудитория\n"
            "- Что адаптировать под нашу внешность (учитывай стратегию каждого конкурента)\n"
            f"- Сравни карму конкурентов с нашими аккаунтами: наш NSFW {nsfw_acc.get('post_karma',0):,} карм\n"
            "LEVEL2_END",
            max_tokens=2800,
        )
    except Exception as e:
        logger.error("Competitor Claude analysis failed: %s", e)
        raw_analysis = ""

    new_subs_db: list[str] = []
    niches_db:   list[str] = []
    ideas_db:    list[str] = []
    priority_db: list[str] = []

    in_l3 = False
    for line in raw_analysis.splitlines():
        s  = line.strip()
        if "LEVEL3_START" in s: in_l3 = True;  continue
        if "LEVEL3_END"   in s: in_l3 = False; continue
        if not in_l3: continue
        up = s.upper()
        if   up.startswith("NEW_SUBS:"):     new_subs_db = [x.strip() for x in s.split(":",1)[1].split(",") if x.strip()]
        elif up.startswith("NICHES:"):       niches_db   = [x.strip() for x in s.split(":",1)[1].split(";") if x.strip()]
        elif up.startswith("IDEAS:"):        ideas_db    = [x.strip() for x in s.split(":",1)[1].split(";") if x.strip()]
        elif up.startswith("PRIORITY_SUBS:"): priority_db = [x.strip() for x in s.split(":",1)[1].split(",") if x.strip()]

    for cd in competitor_data:
        db_save_competitor_insight(
            week_start=week_start, username=cd["username"],
            new_subreddits=cd["new_subs"], top_subreddits=cd["used_subs"],
            content_ideas=ideas_db, priority_subs=priority_db, raw_analysis=raw_analysis,
        )

    out: list[str] = ["📊 КОНКУРЕНТНЫЙ АНАЛИЗ\n"]
    for cd in competitor_data:
        tp        = cd["top_post"]
        top_title = (tp.get("title") or "—")[:60]          if tp else "—"
        top_score = (tp.get("score") or tp.get("ups") or 0) if tp else 0
        top_sub   = (tp.get("subreddit") or "?")            if tp else "?"
        copy_mark = "✅ Похожа на нас — копируем детально" if cd["copy_style"] else "⚠️ Копируем стиль, не анатомию"
        out.append(f"━━━ @{cd['username']} ━━━")
        out.append(f"🔥 Топ пост: {top_title} — {top_score} апв (r/{top_sub})")
        out.append(f"📍 Активные сабы: {', '.join(cd['used_subs'][:7]) or 'нет данных'}")
        if cd["new_subs"]:
            out.append(f"🆕 Новые для нас: {', '.join(cd['new_subs'][:6])}")
        out.append(f"👁️ Визуал: {cd['vision'][:220] if cd['vision'] else '—'}")
        out.append(f"{copy_mark}\n")

    l1_lines: list[str] = []
    l2_lines: list[str] = []
    in_l1 = in_l2 = False
    for line in raw_analysis.splitlines():
        s = line.strip()
        if "LEVEL1_START" in s: in_l1 = True;  continue
        if "LEVEL1_END"   in s: in_l1 = False; continue
        if "LEVEL2_START" in s: in_l2 = True;  continue
        if "LEVEL2_END"   in s: in_l2 = False; continue
        if in_l1 and s: l1_lines.append(s)
        if in_l2 and s: l2_lines.append(s)

    if l1_lines:
        out.append("📌 ПРЯМЫЕ ИНСАЙТЫ (копируем сейчас):")
        out.extend(l1_lines[:15])
        out.append("")
    if l2_lines:
        out.append("💡 СТРАТЕГИЧЕСКИЕ ВЫВОДЫ:")
        out.extend(l2_lines[:15])
        out.append("")

    all_new = list(dict.fromkeys(s for cd in competitor_data for s in cd["new_subs"]))
    if all_new:
        out.append("🎯 НОВЫЕ САБРЕДДИТЫ от конкурентов (добавить в пул):")
        out.append(", ".join(f"r/{s}" for s in all_new[:12]))
        out.append("")
    if niches_db:
        out.append("💡 НОВЫЕ НАПРАВЛЕНИЯ для нас:")
        out.extend(f"• {n}" for n in niches_db[:6])
        out.append("")

    out.append(
        f"🧠 СОХРАНЕНО В БАЗУ: "
        f"{len(new_subs_db)} новых сабов, {len(niches_db)} ниш, "
        f"{len(ideas_db)} идей, {len(priority_db)} приоритетных сабов"
    )
    return "\n".join(out)


# ─── Daily plan pipeline ──────────────────────────────────────────────────────

async def run_daily_plan(app) -> None:
    """Scheduled: every day 11:00 Asia/Bangkok. Sends readiness prompt — analysis runs on button press."""
    chat_ids = load_chat_ids()
    if not chat_ids:
        logger.warning("No registered chats — skipping daily plan")
        return

    now      = datetime.datetime.now(BANGKOK_TZ)
    date_str = now.strftime("%d.%m.%Y")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Готов к анализу", callback_data=CALLBACK_DAILY_READY),
    ]])

    for cid in chat_ids:
        try:
            await app.bot.send_message(
                chat_id=cid,
                text=f"📋 ПЛАН НА СЕГОДНЯ — {date_str}\n\nГотовы к анализу?",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error("Failed sending daily prompt to %s: %s", cid, e)


async def _run_daily_analysis(bot, chat_id: int) -> None:
    """Full analysis for one chat_id: fetch girls' posts, enrich scores, categorise, send result."""
    now      = datetime.datetime.now(BANGKOK_TZ)
    date_str = now.strftime("%d.%m.%Y")
    reset_tokens()

    # ── Fetch posts from all 3 girls ─────────────────────────────────────────
    girls_data: list[dict] = []
    all_subs_ordered: list[str] = []

    for username in DAILY_TRACKED_GIRLS:
        try:
            posts = await asyncio.to_thread(fetch_user_posts_24h, username, 25)
        except Exception as e:
            logger.warning("@%s fetch failed: %s", username, e)
            posts = []

        used_subs: list[str] = list(dict.fromkeys(
            s for s in (
                p.get("subreddit") or p.get("subreddit_name_prefixed", "").lstrip("r/")
                for p in posts
                if p.get("subreddit") or p.get("subreddit_name_prefixed")
            )
            if s
            and not s.startswith("u_")
            and not any(kw in s.lower() for kw in EXCLUDE_KEYWORDS)
        ))
        for s in used_subs:
            if s not in all_subs_ordered:
                all_subs_ordered.append(s)

        top_post = max(posts, key=lambda p: p.get("score") or p.get("ups") or 0) if posts else None
        girls_data.append({"username": username, "posts": posts, "used_subs": used_subs, "top_post": top_post})

    total_posts = sum(len(gd["posts"]) for gd in girls_data)

    if not all_subs_ordered:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📋 ПЛАН НА СЕГОДНЯ — {date_str}\n\nДевочки не постили последние 24 часа."
            )
        except Exception:
            pass
        return

    # ── Enrich post scores via ScrapeCreators top-of-day ────────────────────
    girl_by_name: dict[str, dict] = {gd["username"].lower(): gd for gd in girls_data}
    for sub in all_subs_ordered:
        try:
            sc_posts = await asyncio.to_thread(fetch_top_posts_24h, sub, 25)
            for sp in sc_posts:
                author = (sp.get("author") or "").lower()
                gd = girl_by_name.get(author)
                if gd is None:
                    continue
                sp_score = sp.get("score") or sp.get("ups") or 0
                if sp_score == 0:
                    continue
                sc_id    = sp.get("id") or sp.get("name", "").replace("t3_", "")
                sp_title = (sp.get("title") or "").strip().lower()
                for p in gd["posts"]:
                    if p.get("subreddit") != sub:
                        continue
                    if p.get("score", 0) > 0:
                        continue
                    id_match    = sc_id and p.get("post_id") and sc_id == p["post_id"]
                    title_match = sp_title and (p.get("title") or "").strip().lower() == sp_title
                    if id_match or title_match:
                        p["score"] = sp_score
                        p["ups"]   = sp_score
                        break
        except Exception as e:
            logger.warning("Score enrichment r/%s: %s", sub, e)

    # Recompute top_post with real scores
    for gd in girls_data:
        if gd["posts"]:
            gd["top_post"] = max(gd["posts"], key=lambda p: p.get("score") or p.get("ups") or 0)

    # ── Subreddit info (weekly active users) ─────────────────────────────────
    sub_info: dict[str, dict] = {}
    for sub in all_subs_ordered:
        sub_info[sub] = await asyncio.to_thread(fetch_subreddit_info, sub)

    # ── Detect flying posts ──────────────────────────────────────────────────
    flying_subs: dict[str, int] = {}
    for gd in girls_data:
        for p in gd["posts"]:
            score = p.get("score") or p.get("ups") or 0
            sub   = p.get("subreddit") or p.get("subreddit_name_prefixed", "").lstrip("r/")
            age_h = (datetime.datetime.utcnow().timestamp() - (p.get("created_utc") or 0)) / 3600
            if (score >= 200 or (age_h < 3 and score >= 50)) and sub:
                if sub not in flying_subs or score > flying_subs[sub]:
                    flying_subs[sub] = score

    # ── Per-subreddit best post score today ──────────────────────────────────
    sub_best: dict[str, int] = {}
    for sub in all_subs_ordered:
        best = 0
        for gd in girls_data:
            for p in gd["posts"]:
                if (p.get("subreddit") or "") == sub:
                    best = max(best, p.get("score") or p.get("ups") or 0)
        sub_best[sub] = best

    # ── Deterministic categorisation ─────────────────────────────────────────
    def _sort_key(sub: str) -> float:
        score  = sub_best.get(sub, 0)
        active = sub_info.get(sub, {}).get("subscribers", 0)
        return (100_000 if sub in flying_subs else 0) + score * 3 + active / 5_000

    hot_subs: list[str]     = []
    good_subs: list[str]    = []
    neutral_subs: list[str] = []

    for sub in all_subs_ordered:
        score  = sub_best.get(sub, 0)
        active = sub_info.get(sub, {}).get("subscribers", 0)
        if sub in flying_subs or score >= 70 or (score >= 40 and active >= 100_000):
            hot_subs.append(sub)
        elif score >= 25 or (score >= 10 and active >= 80_000):
            good_subs.append(sub)
        else:
            neutral_subs.append(sub)

    hot_subs.sort(key=_sort_key, reverse=True)
    good_subs.sort(key=_sort_key, reverse=True)
    neutral_subs.sort(key=_sort_key, reverse=True)

    flying_set = flying_subs

    # ── Build Telegram message ───────────────────────────────────────────────
    lines: list[str] = [f"📋 ПЛАН НА СЕГОДНЯ — {date_str}\n"]
    lines.append(f"Девочки сделали {total_posts} постов\n")

    def _fmt_score(n: int) -> str:
        return f"{n/1000:.1f}к".replace(".0к", "к") if n >= 1000 else str(n)

    def _sub_line_full(s: str) -> str:
        info  = sub_info.get(s, {})
        subs  = info.get("subscribers", 0)
        score = flying_set.get(s)
        flew  = f"  📈 залетело ({_fmt_score(score)} апв)" if score is not None else ""
        label = _fmt_score(subs) if subs else "?"
        return f"r/{s}  ({label} акт./нед.){flew}"

    def _sub_line_compact(s: str) -> str:
        info  = sub_info.get(s, {})
        subs  = info.get("subscribers", 0)
        label = _fmt_score(subs) if subs else "?"
        return f"r/{s} ({label})"

    if hot_subs:
        lines.append("🔥 ГОРЯЧИЕ РЕДДИТЫ:")
        lines.extend(_sub_line_full(s) for s in hot_subs)
        lines.append("")

    if good_subs:
        lines.append("✅ ХОРОШИЕ РЕДДИТЫ:")
        lines.extend(_sub_line_full(s) for s in good_subs)
        lines.append("")

    if neutral_subs:
        lines.append("⚪ НЕЙТРАЛЬНЫЕ РЕДДИТЫ:")
        lines.append("  " + ",  ".join(_sub_line_compact(s) for s in neutral_subs))
        lines.append("")

    message_text = "\n".join(lines)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔥 Горячее",     callback_data=CALLBACK_DAILY_HOT),
        InlineKeyboardButton("✅ Хорошее",     callback_data=CALLBACK_DAILY_GOOD),
        InlineKeyboardButton("⚪ Нейтральное", callback_data=CALLBACK_DAILY_NEUTRAL),
    ]])

    try:
        await bot.send_message(chat_id=chat_id, text=message_text, reply_markup=keyboard)
        if len(_daily_cache) > 50:
            del _daily_cache[next(iter(_daily_cache))]
        _daily_cache[chat_id] = {
            "hot":        hot_subs,
            "good":       good_subs,
            "neutral":    neutral_subs,
            "flying":     dict(flying_subs),
            "sub_info":   sub_info,
            "girls_data": [{"username": gd["username"], "used_subs": gd["used_subs"]} for gd in girls_data],
            "date_str":   date_str,
        }
        _save_cache(_daily_cache)
    except Exception as e:
        logger.error("Failed sending daily plan to %s: %s", chat_id, e)

    logger.info("Daily analysis sent to %s. %s", chat_id, token_summary())


async def _analyze_subs_for_pdf(subs_raw: list[dict]) -> list[dict]:
    """
    Calls Claude in batches of 5 to analyze subreddits.
    Returns list of dicts with keys: name, rules_ru, verification, nudity, content_type.
    Subreddits that require pussy/anus are excluded.
    """
    results: list[dict] = []

    for i in range(0, len(subs_raw), 5):
        batch = subs_raw[i:i + 5]
        ctx_parts = []
        for sd in batch:
            top_titles = " | ".join(p.get("title", "")[:70] for p in sd["posts"][:5])
            ctx_parts.append(
                f"SUB: r/{sd['name']}\n"
                f"RULES:\n{sd['rules'][:2000]}\n"
                f"TOP POSTS: {top_titles or 'нет данных'}"
            )

        prompt = (
            "Проанализируй каждый сабреддит строго по формату. Только данный формат, без лишнего текста.\n\n"
            "Для каждого саба выведи блок:\n"
            "SUBSTART\n"
            "NAME: название_саба\n"
            "EXCLUDE: да или нет  (да — если: 1) для апвоутов ОБЯЗАТЕЛЬНО нужно показывать киску или анус; ИЛИ 2) саб направлен конкретно на рыжих/ginger девушек)\n"
            "RULES_RU: [все правила на русском, каждое с новой строки]\n"
            "VERIFICATION: нужна / не нужна / нужна — подробности\n"
            "NUDITY: бикини / топлесс / полностью голая / зависит от поста\n"
            "CONTENT_TYPE: [подробно: что постят, какой стиль, формат, что хорошо заходит]\n"
            "SUBEND\n\n"
            "Данные:\n\n" + "\n\n===\n\n".join(ctx_parts)
        )

        try:
            raw = await asyncio.to_thread(
                ask_openrouter,
                "Ты эксперт по Reddit-маркетингу для OnlyFans/Fansly. Пиши на русском.",
                prompt,
                max_tokens=3500,
            )
        except Exception as e:
            logger.error("PDF analysis batch %d failed: %s", i // 5, e)
            continue

        current: dict = {}
        in_sub         = False
        rules_active   = False

        for line in raw.splitlines():
            s  = line.strip()
            up = s.upper()

            if "SUBSTART" in up:
                in_sub = True; current = {}; rules_active = False; continue
            if "SUBEND" in up:
                if current and current.get("exclude", "нет").lower().strip() != "да":
                    results.append(current)
                in_sub = False; current = {}; rules_active = False; continue
            if not in_sub:
                continue

            if up.startswith("NAME:"):
                current["name"]  = s.split(":", 1)[1].strip(); rules_active = False
            elif up.startswith("EXCLUDE:"):
                current["exclude"] = s.split(":", 1)[1].strip(); rules_active = False
            elif up.startswith("RULES_RU:"):
                current["rules_ru"] = s.split(":", 1)[1].strip(); rules_active = True
            elif up.startswith("VERIFICATION:"):
                current["verification"] = s.split(":", 1)[1].strip(); rules_active = False
            elif up.startswith("NUDITY:"):
                current["nudity"] = s.split(":", 1)[1].strip(); rules_active = False
            elif up.startswith("CONTENT_TYPE:"):
                current["content_type"] = s.split(":", 1)[1].strip(); rules_active = False
            elif rules_active and s:
                current["rules_ru"] = current.get("rules_ru", "") + "\n" + s

    return results


def _wrap_long_words(text: str, max_len: int = 40) -> str:
    """Insert spaces into words longer than max_len so fpdf2 can wrap them."""
    result = []
    for word in text.split():
        while len(word) > max_len:
            result.append(word[:max_len])
            word = word[max_len:]
        result.append(word)
    return " ".join(result)


def _build_pdf(subs_analyzed: list[dict], category: str, date_str: str, token_info: dict | None = None) -> str:
    """Generate PDF and return temp file path."""
    import tempfile
    from fpdf import FPDF

    # A5 portrait — narrower than A4, fits phone screens much better
    pdf = FPDF(orientation="P", unit="mm", format="A5")
    pdf.set_margins(12, 12, 12)
    pdf.set_auto_page_break(auto=True, margin=12)

    _base = os.path.dirname(os.path.abspath(__file__))
    _candidates_reg = [
        os.path.join(_base, "fonts", "DejaVuSans.ttf"),
        os.path.join(_base, "fonts", "arial.ttf"),
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    _candidates_bold = [
        os.path.join(_base, "fonts", "DejaVuSans-Bold.ttf"),
        os.path.join(_base, "fonts", "arialbd.ttf"),
        r"C:\Windows\Fonts\arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    font_reg  = next((p for p in _candidates_reg  if os.path.exists(p)), None)
    font_bold = next((p for p in _candidates_bold if os.path.exists(p)), None)
    if font_reg is None:
        raise FileNotFoundError(
            "Не найден TTF-шрифт для PDF. Убедитесь что папка fonts/ есть рядом с bot.py"
        )
    pdf.add_font("f", "",  font_reg)
    pdf.add_font("f", "B", font_bold or font_reg)

    lh = 7  # line height

    def section(label: str, text: str) -> None:
        clean = _wrap_long_words((text or "—").strip())
        pdf.set_x(pdf.l_margin)
        pdf.set_font("f", "B", 12)
        pdf.multi_cell(pdf.epw, lh, label, align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(pdf.l_margin)
        pdf.set_font("f", "", 11)
        pdf.multi_cell(pdf.epw, lh, clean, align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

    # ── Title page ────────────────────────────────────────────────────────
    pdf.add_page()
    pdf.set_x(pdf.l_margin)
    pdf.set_font("f", "B", 18)
    pdf.multi_cell(pdf.epw, 11, f"Реддиты: {category}", align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("f", "", 12)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(pdf.epw, 8, f"Дата: {date_str}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(pdf.epw, 8, f"Сабреддитов: {len(subs_analyzed)}", new_x="LMARGIN", new_y="NEXT")

    # ── One sub per page ──────────────────────────────────────────────────
    for sub in subs_analyzed:
        pdf.add_page()

        pdf.set_x(pdf.l_margin)
        pdf.set_font("f", "B", 15)
        pdf.multi_cell(pdf.epw, 10, f"r/{sub.get('name', '?')}", align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

        section("Верификация",    sub.get("verification", "—"))
        section("Уровень наготы", sub.get("nudity",        "—"))
        section("Тип контента",   sub.get("content_type",  "—"))
        section("Правила (рус.)", sub.get("rules_ru",      "—"))

    # ── Token / cost page ─────────────────────────────────────────────────
    if token_info:
        pdf.add_page()
        pdf.set_font("f", "B", 18)
        pdf.multi_cell(pdf.epw, 12, "Расход токенов (этот PDF)")
        pdf.ln(5)
        t_in  = token_info.get("in",   0)
        t_out = token_info.get("out",  0)
        cost  = token_info.get("cost", 0.0)
        rows = [
            ("Модель",           "Claude Sonnet 4.6 (OpenRouter)"),
            ("Входящие токены",  f"{t_in:,}"),
            ("Исходящие токены", f"{t_out:,}"),
            ("Всего токенов",    f"{t_in + t_out:,}"),
            ("Стоимость",        f"${cost:.4f}"),
        ]
        for label, value in rows:
            section(label, value)

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    pdf.output(tmp.name)
    tmp.close()
    return tmp.name


async def callback_daily_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Hot / Good / Neutral button: fetch sub rules, ask Claude, send PDF."""
    query   = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()

    cache = _daily_cache.get(chat_id)
    if not cache:
        await context.bot.send_message(chat_id=chat_id, text="Данные устарели. Запусти /todayplan заново.")
        return

    cat_key  = query.data
    cat_map  = {
        CALLBACK_DAILY_HOT:     ("hot",     "Горячие"),
        CALLBACK_DAILY_GOOD:    ("good",    "Хорошие"),
        CALLBACK_DAILY_NEUTRAL: ("neutral", "Нейтральные"),
    }
    cache_key, cat_name = cat_map[cat_key]
    subs = cache.get(cache_key, [])

    if not subs:
        await context.bot.send_message(chat_id=chat_id, text=f"В категории «{cat_name}» нет сабреддитов.")
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Анализирую {len(subs)} сабреддитов «{cat_name}»... (~{max(1, len(subs) // 5 * 30)} сек)"
    )

    # ── Fetch rules + top posts for each sub ─────────────────────────────
    subs_raw: list[dict] = []
    for sub in subs:
        rules = await asyncio.to_thread(fetch_subreddit_rules, sub)
        try:
            posts = await asyncio.to_thread(fetch_top_posts_week, sub, 10)
        except Exception:
            posts = []
        subs_raw.append({"name": sub, "rules": rules, "posts": posts})

    # ── Claude analysis (batches of 5) ───────────────────────────────────
    tok_before = (_tok["in"], _tok["out"])
    analyzed = await _analyze_subs_for_pdf(subs_raw)
    tok_in  = _tok["in"]  - tok_before[0]
    tok_out = _tok["out"] - tok_before[1]
    cost    = tok_in * 3 / 1_000_000 + tok_out * 15 / 1_000_000

    excluded_count = len(subs) - len(analyzed)
    if not analyzed:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Все сабреддиты исключены (требуют контент который мы не снимаем)."
        )
        return

    # ── Build and send PDF ───────────────────────────────────────────────
    date_str  = cache.get("date_str", "")
    token_info = {
        "in":   tok_in,
        "out":  tok_out,
        "cost": cost,
    }
    try:
        pdf_path = await asyncio.to_thread(_build_pdf, analyzed, cat_name, date_str, token_info)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Ошибка генерации PDF: {e}")
        return

    fname   = f"plan_{cat_name.lower()}_{date_str.replace('.', '_')}.pdf"
    caption = f"📄 {cat_name} — {len(analyzed)} сабреддитов"
    if excluded_count:
        caption += f" (исключено {excluded_count}: требуют киску/анус)"

    try:
        with open(pdf_path, "rb") as f:
            await context.bot.send_document(chat_id=chat_id, document=f, filename=fname, caption=caption)
    finally:
        try:
            os.unlink(pdf_path)
        except Exception:
            pass


# ─── Callback: "Готов к анализу" button ──────────────────────────────────────

async def callback_daily_ready(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()
    await context.bot.send_message(chat_id=chat_id, text="🔍 Запускаю анализ, подожди минуту...")
    await _run_daily_analysis(context.bot, chat_id)


# ─── Telegram command handlers ────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    save_chat_id(update.effective_chat.id)
    text = (
        "Привет! Я бот для анализа Reddit-контента.\n\n"
        "Команды:\n"
        "/ping — проверить все подключения\n"
        "/test — топ-5 постов из r/cosplay\n"
        "/digest — дайджест топ-3 постов по всем категориям\n"
        "/findsubs — найти новые сабреддиты (10k+ подписчиков)\n"
        "/todayplan — дневной план прямо сейчас\n"
        "/competitors — конкурентный анализ прямо сейчас\n"
        "/ask [вопрос] — вопрос по Reddit-данным\n\n"
        "Авто: дневной план каждый день в 11:00 Bangkok."
    )
    await update.message.reply_text(text)


async def cmd_todayplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual trigger: run full analysis right now."""
    save_chat_id(update.effective_chat.id)
    await update.message.reply_text("🔍 Запускаю анализ, подожди минуту...")
    await _run_daily_analysis(context.bot, update.effective_chat.id)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Загружаю топ-5 постов из r/cosplay...")
    try:
        posts = await asyncio.to_thread(fetch_reddit_posts, "cosplay", 5)
        if not posts:
            await update.message.reply_text("Посты не найдены. Проверьте API-ключ.")
            return
        lines = ["Топ-5 постов r/cosplay:\n"]
        for i, post in enumerate(posts, 1):
            title = post.get("title") or post.get("name") or "No title"
            score = post.get("score") or post.get("ups") or 0
            url   = post.get("url") or post.get("permalink") or ""
            lines.append(f"{i}. {title}\n   👍 {score}  {url}")
        await update.message.reply_text("\n\n".join(lines))
    except requests.HTTPError as e:
        await update.message.reply_text(
            f"Ошибка ScrapeCreators: {e.response.status_code}\n{e.response.text[:300]}"
        )
    except Exception as e:
        logger.exception("Error in /test")
        await update.message.reply_text(f"Ошибка: {e}")


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Собираю дайджест по всем категориям...")
    for category, subs in CATEGORIES.items():
        lines     = [f"📂 {category}"]
        has_posts = False
        for sub in subs:
            try:
                posts = await asyncio.to_thread(fetch_reddit_posts, sub, 3)
                if not posts:
                    continue
                lines.append(f"\nr/{sub}:")
                for i, post in enumerate(posts, 1):
                    title = post.get("title") or post.get("name") or "No title"
                    score = post.get("score") or post.get("ups") or 0
                    lines.append(f"  {i}. {title} (👍{score})")
                has_posts = True
            except Exception as e:
                logger.warning("r/%s failed: %s", sub, e)
        if has_posts:
            await update.message.reply_text("\n".join(lines))
    await update.message.reply_text("Дайджест готов.")


def search_subreddits_by_keyword(keyword: str, limit: int = 25) -> list[dict]:
    try:
        r = requests.get(
            "https://www.reddit.com/subreddits/search.json",
            headers=REDDIT_HEADERS,
            params={"q": keyword, "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        return [c["data"] for c in r.json().get("data", {}).get("children", [])]
    except Exception as e:
        logger.warning("search_subreddits_by_keyword %r failed: %s", keyword, e)
        return []


def filter_by_subscribers(subs: list[dict], min_subs: int = 10_000) -> list[dict]:
    return [s for s in subs if (s.get("subscribers") or 0) >= min_subs]


async def cmd_findsubs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Ищу сабреддиты по {len(FINDSUBS_KEYWORDS)} ключевым словам...")
    seen: dict[str, dict] = {}
    for keyword in FINDSUBS_KEYWORDS:
        try:
            raw = await asyncio.to_thread(search_subreddits_by_keyword, keyword)
            for sub in filter_by_subscribers(raw):
                name = sub["display_name"].lower()
                if name not in seen:
                    seen[name] = sub
        except Exception as e:
            logger.warning("Keyword '%s' failed: %s", keyword, e)

    if not seen:
        await update.message.reply_text("Ничего не найдено.")
        return

    await update.message.reply_text(f"Найдено {len(seen)} сабреддитов. Отправляю на анализ Claude...")

    subs_list = sorted(seen.values(), key=lambda s: s.get("subscribers", 0), reverse=True)
    subs_text = "\n".join(
        f"- r/{s['display_name']} ({s.get('subscribers', 0):,}) — "
        f"{(s.get('public_description') or s.get('title') or '')[:80]}"
        for s in subs_list
    )

    try:
        answer = await asyncio.to_thread(
            ask_openrouter,
            build_system_prompt("Ты Reddit-стратег для создателя контента для взрослых. Отвечай на русском."),
            f"Список сабреддитов:\n{subs_text}\n\n"
            "Выбери топ-10 самых релевантных для продвижения контента ЭТОГО конкретного создателя. "
            "Учитывай типы контента, эстетику и аудиторию из профиля. "
            "Формат: r/название — X подписчиков — почему подходит именно нам.",
        )
        await update.message.reply_text(f"Топ-10 сабреддитов:\n\n{answer}")
    except Exception as e:
        logger.exception("OpenRouter error in /findsubs")
        fallback = "\n".join(f"r/{s['display_name']} — {s.get('subscribers', 0):,}" for s in subs_list[:10])
        await update.message.reply_text(f"Claude недоступен. Топ-10 по подписчикам:\n\n{fallback}")


async def cmd_competitors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    save_chat_id(update.effective_chat.id)
    await update.message.reply_text(
        f"🔍 Анализирую {len(COMPETITORS)} конкурентов...\nЭто займёт ~1 минуту."
    )
    try:
        known  = set(ALL_SUBS) | set(await asyncio.to_thread(db_get_active_sub_names))
        report = await run_competitor_analysis(known)
        await send_long_message(context.bot, update.effective_chat.id, report)
    except Exception as e:
        logger.exception("Error in /competitors")
        await update.message.reply_text(f"Ошибка: {e}")


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    save_chat_id(update.effective_chat.id)
    await update.message.reply_text("🔍 Проверка систем...")

    results: list[str] = []
    failed:  list[str] = []

    try:
        t0 = datetime.datetime.now()
        r  = requests.get(
            f"{SCRAPECREATORS_BASE}/subreddit",
            headers={"x-api-key": SCRAPECREATORS_API_KEY},
            params={"subreddit": "cosplay", "type": "hot", "limit": 1},
            timeout=15,
        )
        r.raise_for_status()
        ms = int((datetime.datetime.now() - t0).total_seconds() * 1000)
        results.append(f"🌐 ScrapeCreators: ✅ {ms}ms")
    except Exception as e:
        results.append(f"🌐 ScrapeCreators: ❌ {e}")
        failed.append("ScrapeCreators")

    try:
        t0 = datetime.datetime.now()
        r  = requests.post(
            OPENROUTER_BASE,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://reddit-analyzer-bot",
                "X-Title": "Reddit Analyzer Bot",
            },
            json={"model": "anthropic/claude-sonnet-4-6", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5},
            timeout=30,
        )
        r.raise_for_status()
        ms = int((datetime.datetime.now() - t0).total_seconds() * 1000)
        results.append(f"🤖 Claude Sonnet: ✅ {ms}ms")
    except Exception as e:
        results.append(f"🤖 Claude Sonnet: ❌ {e}")
        failed.append("Claude Sonnet")

    try:
        t0 = datetime.datetime.now()
        get_sb().table("subreddits").select("name").limit(1).execute()
        ms = int((datetime.datetime.now() - t0).total_seconds() * 1000)
        results.append(f"🗄️ Supabase: ✅ {ms}ms")
    except Exception as e:
        results.append(f"🗄️ Supabase: ❌ {e}")
        failed.append("Supabase")

    try:
        t0 = datetime.datetime.now()
        read_google_sheet(SHEET_ID)
        ms = int((datetime.datetime.now() - t0).total_seconds() * 1000)
        results.append(f"📊 Google Sheets: ✅ {ms}ms")
    except Exception as e:
        results.append(f"📊 Google Sheets: ❌ {str(e)[:100]}")
        failed.append("Google Sheets")

    try:
        bot_info = await context.bot.get_me()
        chat_id  = update.effective_chat.id
        results.append(f"🤖 Telegram: ✅ @{bot_info.username} | chat_id: {chat_id}")
    except Exception as e:
        results.append(f"🤖 Telegram: ❌ {e}")
        failed.append("Telegram")

    footer = "Все системы работают 🟢" if not failed else f"⚠️ Проблемы обнаружены: {', '.join(failed)}"
    await update.message.reply_text("🔍 Проверка систем...\n\n" + "\n".join(results) + f"\n\n{footer}")


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Использование: /ask [вопрос]\n"
            "Пример: /ask Какие косплей-персонажи сейчас в тренде?"
        )
        return
    question = " ".join(context.args)
    await update.message.reply_text("Анализирую Reddit...")
    try:
        posts      = await asyncio.to_thread(fetch_reddit_posts, "cosplay", 10)
        posts_text = "\n".join(f"- {p.get('title', '')} (score: {p.get('score', 0)})" for p in posts)
        answer     = await asyncio.to_thread(
            ask_openrouter,
            build_system_prompt("Ты Reddit-аналитик для создателя контента для взрослых. Отвечай на русском."),
            f"Данные r/cosplay:\n{posts_text}\n\nВопрос: {question}",
        )
        await update.message.reply_text(answer)
    except Exception as e:
        logger.exception("Error in /ask")
        await update.message.reply_text(f"Ошибка: {e}")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    gkey_preview = (GOOGLE_API_KEY[:10] + "…") if GOOGLE_API_KEY else "НЕ ЗАДАН"
    logger.info("GOOGLE_API_KEY (первые 10 симв.): %s", gkey_preview)
    logger.info("SHEET_ID: %s", SHEET_ID or "НЕ ЗАДАН")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("ping",        cmd_ping))
    app.add_handler(CommandHandler("test",        cmd_test))
    app.add_handler(CommandHandler("digest",      cmd_digest))
    app.add_handler(CommandHandler("findsubs",    cmd_findsubs))
    app.add_handler(CommandHandler("todayplan",   cmd_todayplan))
    app.add_handler(CommandHandler("competitors", cmd_competitors))
    app.add_handler(CommandHandler("ask",         cmd_ask))
    app.add_handler(CallbackQueryHandler(
        callback_daily_ready,
        pattern=f"^{CALLBACK_DAILY_READY}$"
    ))
    app.add_handler(CallbackQueryHandler(
        callback_daily_category,
        pattern=f"^({CALLBACK_DAILY_HOT}|{CALLBACK_DAILY_GOOD}|{CALLBACK_DAILY_NEUTRAL})$"
    ))

    if db_check_ready():
        seeded = db_seed_from_file()
        logger.info("Supabase OK. Seeded/updated %d subreddits from subreddits.py.", seeded)
    else:
        logger.warning(
            "Supabase table 'subreddits' not found. "
            "Run this SQL in the Supabase dashboard:\n\n"
            "CREATE TABLE public.subreddits (\n"
            "  id BIGSERIAL PRIMARY KEY,\n"
            "  name TEXT UNIQUE NOT NULL,\n"
            "  category TEXT,\n"
            "  subscribers BIGINT DEFAULT 0,\n"
            "  added_date DATE DEFAULT CURRENT_DATE,\n"
            "  active BOOLEAN DEFAULT TRUE,\n"
            "  created_at TIMESTAMPTZ DEFAULT NOW()\n"
            ");\n"
            "ALTER TABLE public.subreddits ENABLE ROW LEVEL SECURITY;\n"
            "CREATE POLICY allow_all ON public.subreddits FOR ALL TO anon USING (true) WITH CHECK (true);\n"
        )

    if not db_check_competitor_table():
        logger.warning(
            "Supabase table 'competitor_insights' not found. "
            "Run this SQL in the Supabase dashboard:\n\n"
            "CREATE TABLE public.competitor_insights (\n"
            "  id BIGSERIAL PRIMARY KEY,\n"
            "  week_start DATE NOT NULL,\n"
            "  competitor_username TEXT NOT NULL,\n"
            "  new_subreddits JSONB DEFAULT '[]',\n"
            "  top_subreddits JSONB DEFAULT '[]',\n"
            "  content_ideas JSONB DEFAULT '[]',\n"
            "  priority_subs JSONB DEFAULT '[]',\n"
            "  raw_analysis TEXT,\n"
            "  created_at TIMESTAMPTZ DEFAULT NOW()\n"
            ");\n"
            "ALTER TABLE public.competitor_insights ENABLE ROW LEVEL SECURITY;\n"
            "CREATE POLICY allow_all ON public.competitor_insights FOR ALL TO anon USING (true) WITH CHECK (true);\n"
        )

    scheduler = AsyncIOScheduler(timezone=BANGKOK_TZ)
    scheduler.add_job(
        run_daily_plan,
        trigger=CronTrigger(hour=11, minute=0, timezone=BANGKOK_TZ),
        args=[app],
        id="daily_plan",
        replace_existing=True,
    )

    async def on_startup(application) -> None:
        scheduler.start()
        logger.info("Scheduler started. Daily plan at 11:00 Asia/Bangkok.")

    app.post_init = on_startup
    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
