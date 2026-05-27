import asyncio
import os
import sys
import json
import base64
import logging
import urllib.parse
import datetime
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

try:
    from ddgs import DDGS as _DDGS
    _HAS_DDGS = True
except ImportError:
    _HAS_DDGS = False

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from supabase import create_client, Client as SupabaseClient

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from subreddits import CATEGORIES, ALL_SUBS
from profile import CREATOR_PROFILE, COMPETITORS, OUR_ACCOUNTS

# Fix Windows console encoding for Cyrillic output
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
OPENROUTER_BASE = "https://openrouter.ai/api/v1/chat/completions"
REDDIT_HEADERS = {"User-Agent": "SubFinderBot/1.0"}
FINDSUBS_KEYWORDS = ["cosplay", "anime", "fetish", "gamer girl", "latex", "alternative", "goth", "NSFW"]

BANNED_SUBS = frozenset({"ResidentEvil", "DunderMifflin"})
BANNED_MODS = frozenset({"louise mania", "mad dickson", "pessimist", "rick spanish"})
DRAWN_CONTENT_SUBS = frozenset({
    "AnimeArt", "hentai", "animeart", "GoneWildAnime", "AnimeGirlsNSFW",
    "anime", "AnimeGirls", "HentaiAi", "rule34", "rule34ai", "hentai_gif",
    "ecchi", "doujinshi", "Moescape", "AmateurRoomPorn",
})
UPVOTES_SKIP = 50    # avg below this → exclude sub
UPVOTES_MID  = 100   # avg 50-99 → include with warning label

CALLBACK_WEEKLY    = "weekly_analyze"
CALLBACK_SUBGROUP  = "wk_sg"        # wk_sg_0 … wk_sg_4

# ── In-memory cache: {chat_id: weekly_plan_dict} ──────────────────────────────
_weekly_cache: dict[int, dict] = {}

# Reverse map: subreddit_name_lower -> category name
_SUB_CATEGORY: dict[str, str] = {
    sub.lower(): cat for cat, subs in CATEGORIES.items() for sub in subs
}

BANGKOK_TZ = pytz.timezone("Asia/Bangkok")
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
    avoid = "; ".join(p["avoid"])
    return (
        f"CREATOR: Blonde OnlyFans/Fansly model, wigs (multi-color), alt/egirl/goth/cosplay aesthetics. "
        f"Content: {content}. "
        f"Audience: men 18-35, anime/gaming/alt/fetish. Goal: $7-8k/mo Reddit traffic. "
        f"AVOID on Reddit: {avoid}."
    )


def build_system_prompt(role: str) -> str:
    return f"{role}\n\n{_compact_profile()}"


# ─── Account helpers ───────────────────────────────────────────────────────────

def _get_account(acc_type: str) -> dict:
    """Return SFW or NSFW account dict from OUR_ACCOUNTS."""
    return next((a for a in OUR_ACCOUNTS if a["type"] == acc_type), {})


def _accounts_block() -> str:
    """Short text block describing our accounts for Claude prompts."""
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
    """One-liner for inline references in prompts."""
    sfw  = _get_account("SFW")
    nsfw = _get_account("NSFW")
    return (
        f"SFW @{sfw.get('username')} ({sfw.get('post_karma',0):,} karma) | "
        f"NSFW @{nsfw.get('username')} ({nsfw.get('post_karma',0):,} karma)"
    )


# ─── Token usage tracking ──────────────────────────────────────────────────────

_tok: dict[str, int] = {"in": 0, "out": 0, "calls": 0}

def reset_tokens() -> None:
    _tok["in"] = 0
    _tok["out"] = 0
    _tok["calls"] = 0

def token_summary() -> str:
    total = _tok["in"] + _tok["out"]
    # Claude Sonnet 4.6: $3/M input, $15/M output (via OpenRouter)
    cost = _tok["in"] * 3 / 1_000_000 + _tok["out"] * 15 / 1_000_000
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
    """Insert or update subreddits by name. Returns count saved."""
    if not rows:
        return 0
    try:
        result = get_sb().table("subreddits").upsert(rows, on_conflict="name").execute()
        return len(result.data) if result.data else 0
    except Exception as e:
        logger.error("db_upsert_subreddits failed: %s", e)
        return 0


def db_get_active_sub_names() -> list[str]:
    """Return names of all active subreddits from Supabase."""
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
    """Seed Supabase with subreddits.py list if they are not already present."""
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
    """Return True if the subreddits table exists and is accessible."""
    try:
        get_sb().table("subreddits").select("name").limit(1).execute()
        return True
    except Exception:
        return False


# ─── Chat ID persistence ───────────────────────────────────────────────────────

def load_chat_ids() -> set[int]:
    if CHAT_IDS_FILE.exists():
        return set(json.loads(CHAT_IDS_FILE.read_text(encoding="utf-8")))
    return set()

def save_chat_id(chat_id: int) -> None:
    ids = load_chat_ids()
    ids.add(chat_id)
    CHAT_IDS_FILE.write_text(json.dumps(list(ids)), encoding="utf-8")


# ─── Reddit / ScrapeCreators helpers ──────────────────────────────────────────

def fetch_reddit_posts(subreddit: str, limit: int = 5) -> list[dict]:
    url = f"{SCRAPECREATORS_BASE}/subreddit"
    headers = {"x-api-key": SCRAPECREATORS_API_KEY}
    params = {"subreddit": subreddit, "type": "hot", "limit": limit}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    posts = data.get("posts") or data.get("data") or []
    return posts[:limit]


def fetch_top_posts_24h(subreddit: str, limit: int = 5) -> list[dict]:
    url = f"{SCRAPECREATORS_BASE}/subreddit"
    headers = {"x-api-key": SCRAPECREATORS_API_KEY}
    params = {"subreddit": subreddit, "type": "top", "t": "day", "limit": limit}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    posts = data.get("posts") or data.get("data") or []
    return posts[:limit]


def fetch_top_posts_week(subreddit: str, limit: int = 10) -> list[dict]:
    url = f"{SCRAPECREATORS_BASE}/subreddit"
    headers = {"x-api-key": SCRAPECREATORS_API_KEY}
    params = {"subreddit": subreddit, "type": "top", "t": "week", "limit": limit}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    posts = data.get("posts") or data.get("data") or []
    return posts[:limit]


def fetch_subreddit_rules(subreddit: str) -> str:
    try:
        r = requests.get(
            f"https://www.reddit.com/r/{subreddit}/about/rules.json",
            headers=REDDIT_HEADERS,
            timeout=10,
        )
        rules = r.json().get("rules", [])
        if not rules:
            return "No rules found"
        parts = []
        for i, rule in enumerate(rules, 1):
            name = rule.get("short_name", "").strip()
            desc = (rule.get("description") or "").strip()[:300]
            line = f"Rule #{i}: {name}"
            if desc:
                line += f" — {desc}"
            parts.append(line)
        return "\n".join(parts)
    except Exception:
        return "Rules unavailable"


def fetch_subreddit_mods(subreddit: str) -> list[str]:
    try:
        r = requests.get(
            f"https://www.reddit.com/r/{subreddit}/about/moderators.json",
            headers=REDDIT_HEADERS,
            timeout=10,
        )
        data = r.json().get("data", {}).get("children", [])
        return [m.get("name", "").lower() for m in data]
    except Exception:
        return []


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


# ─── OpenRouter helpers ────────────────────────────────────────────────────────

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
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_tokens,
    }
    r = requests.post(OPENROUTER_BASE, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    usage = data.get("usage", {})
    _tok["in"] += usage.get("prompt_tokens", 0)
    _tok["out"] += usage.get("completion_tokens", 0)
    _tok["calls"] += 1
    return data["choices"][0]["message"]["content"]


def ask_openrouter_vision(images_b64: list[str], text_prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://reddit-analyzer-bot",
        "X-Title": "Reddit Analyzer Bot",
    }
    content: list[dict] = []
    for b64 in images_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })
    content.append({"type": "text", "text": text_prompt})
    payload = {
        "model": "anthropic/claude-sonnet-4-6",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 512,
    }
    r = requests.post(OPENROUTER_BASE, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    usage = data.get("usage", {})
    _tok["in"] += usage.get("prompt_tokens", 0)
    _tok["out"] += usage.get("completion_tokens", 0)
    _tok["calls"] += 1
    return data["choices"][0]["message"]["content"]


# ─── Step 0: web-search tool helpers ──────────────────────────────────────────

_WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information about Reddit trends, "
            "popular subreddits, and content creator niches on OnlyFans/Fansly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"],
        },
    },
}


def execute_web_search(query: str, max_results: int = 6) -> str:
    if not _HAS_DDGS:
        return "Web search unavailable (ddgs not installed)."
    try:
        with _DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "No results found."
        return "\n\n".join(
            f"Title: {r.get('title', '')}\nURL: {r.get('href', '')}\n{r.get('body', '')[:400]}"
            for r in results
        )
    except Exception as e:
        return f"Search error: {e}"


def step0_search_trends() -> tuple[list[str], str]:
    """
    Agentic tool-use loop: Claude calls web_search multiple times,
    then returns a list of trending keywords and a summary.
    Returns (keywords_list, summary_text).
    """
    api_headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://reddit-analyzer-bot",
        "X-Title": "Reddit Analyzer Bot",
    }

    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"You are a Reddit traffic strategist for an adult content creator.\n\n"
                f"{_profile_block()}\n\n"
                "Use the web_search tool to research what niches and subreddit topics "
                "are most popular on Reddit RIGHT NOW (2025) for OnlyFans and Fansly "
                "creators with THIS specific profile. Search at least 3 different queries. "
                "Think beyond the obvious — find unexpected subreddit connections that fit "
                "the content types, aesthetics, and audience described above. After searching, return:\n"
                "1. A comma-separated list of 15-25 keywords/topics to search for subreddits\n"
                "2. A short summary (3-5 sentences) of what is trending\n"
                "Format your final answer as:\n"
                "KEYWORDS: keyword1, keyword2, ...\n"
                "SUMMARY: your summary here"
            ),
        }
    ]

    # Agentic loop — max 6 tool calls
    last_content = ""
    for _ in range(6):
        payload = {
            "model": "anthropic/claude-sonnet-4-6",
            "messages": messages,
            "tools": [_WEB_SEARCH_TOOL],
            "max_tokens": 1500,
        }
        r = requests.post(OPENROUTER_BASE, headers=api_headers, json=payload, timeout=60)
        r.raise_for_status()
        response = r.json()
        choice = response["choices"][0]
        assistant_msg = choice["message"]
        messages.append(assistant_msg)

        if choice.get("finish_reason") != "tool_calls":
            last_content = assistant_msg.get("content") or ""
            break

        # Execute every tool call Claude requested
        for tc in assistant_msg.get("tool_calls", []):
            if tc["function"]["name"] == "web_search":
                query = json.loads(tc["function"]["arguments"]).get("query", "")
                logger.info("Step 0 web_search: %s", query)
                result = execute_web_search(query)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

    # Parse KEYWORDS / SUMMARY — strip markdown bold (**) before matching
    keywords: list[str] = []
    summary = ""
    for line in last_content.splitlines():
        clean = line.replace("**", "").strip()
        upper = clean.upper()
        if upper.startswith("KEYWORDS:"):
            raw = clean.split(":", 1)[1]
            keywords = [k.strip(" *•-") for k in raw.split(",") if k.strip(" *•-")]
        elif upper.startswith("SUMMARY:"):
            summary = clean.split(":", 1)[1].strip()

    if not keywords:
        # Fallback: any comma-separated line with 8+ items
        for line in last_content.splitlines():
            parts = [p.strip(" *•-") for p in line.split(",") if p.strip(" *•-")]
            if len(parts) >= 8:
                keywords = parts
                break

    return keywords[:25], summary


def step0_find_subs_from_keywords(keywords: list[str]) -> list[str]:
    """
    Search Reddit for subreddits matching each keyword,
    filter by 10k+ subscribers, save new ones to Supabase,
    return unique subreddit names.
    """
    seen: dict[str, int] = {}  # display_name -> subscribers
    for kw in keywords:
        try:
            r = requests.get(
                "https://www.reddit.com/subreddits/search.json",
                headers=REDDIT_HEADERS,
                params={"q": kw, "limit": 15},
                timeout=10,
            )
            for child in r.json().get("data", {}).get("children", []):
                d = child["data"]
                subs_count = d.get("subscribers") or 0
                name = d.get("display_name", "")
                if subs_count >= 10_000 and name and name not in seen:
                    if name in BANNED_SUBS or name.lower() in {s.lower() for s in DRAWN_CONTENT_SUBS}:
                        continue
                    seen[name] = subs_count
        except Exception as e:
            logger.warning("Subreddit search for '%s' failed: %s", kw, e)

    if not seen:
        return []

    # Save to Supabase (upsert by name — updates subscriber count)
    today = datetime.date.today().isoformat()
    rows = [
        {
            "name": name,
            "category": "discovered",
            "subscribers": count,
            "added_date": today,
            "active": True,
        }
        for name, count in seen.items()
    ]
    saved = db_upsert_subreddits(rows)
    logger.info("Step 0 saved %d subreddits to Supabase", saved)

    # Return sorted by subscribers descending, max 40
    return sorted(seen.keys(), key=lambda n: seen[n], reverse=True)[:40]


# ─── Competitor helpers ───────────────────────────────────────────────────────

def fetch_competitor_posts(username: str, limit: int = 10) -> list[dict]:
    """Fetch top posts of the week for a Reddit user via Reddit's native JSON API."""
    r = requests.get(
        f"https://www.reddit.com/user/{username}/submitted.json",
        headers=REDDIT_HEADERS,
        params={"sort": "top", "t": "week", "limit": limit},
        timeout=15,
    )
    r.raise_for_status()
    children = r.json().get("data", {}).get("children", [])
    return [c["data"] for c in children[:limit]]


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
            "week_start": week_start.isoformat(),
            "competitor_username": username,
            "new_subreddits": json.dumps(new_subreddits, ensure_ascii=False),
            "top_subreddits": json.dumps(top_subreddits, ensure_ascii=False),
            "content_ideas": json.dumps(content_ideas, ensure_ascii=False),
            "priority_subs": json.dumps(priority_subs, ensure_ascii=False),
            "raw_analysis": raw_analysis[:4000],
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
    """Read all rows from the analytics sheet via Google Sheets API v4."""
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


def format_sheet_for_claude(rows: list[list[str]], max_rows: int = 60) -> str:
    """Format sheet rows as a readable table string for Claude."""
    if not rows:
        return "Таблица пуста."
    header = rows[0]
    data = rows[1:]
    # Take last max_rows data rows (most recent)
    recent = data[-max_rows:] if len(data) > max_rows else data
    lines = [", ".join(header)]
    lines += [", ".join(row) for row in recent if any(cell.strip() for cell in row)]
    return "\n".join(lines)


# ─── Supabase weekly_insights ──────────────────────────────────────────────────

def db_save_weekly_insight(
    week_start: datetime.date,
    week_end: datetime.date,
    raw_rows: list[list[str]],
    analysis: str,
) -> None:
    try:
        get_sb().table("weekly_insights").insert({
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "raw_data": json.dumps(raw_rows, ensure_ascii=False),
            "analysis": analysis,
        }).execute()
        logger.info("Weekly insight saved: %s — %s", week_start, week_end)
    except Exception as e:
        logger.error("db_save_weekly_insight failed: %s", e)


def db_get_previous_insights(limit: int = 4) -> str:
    """Return last N weekly analyses as context for trend comparison."""
    try:
        result = (
            get_sb().table("weekly_insights")
            .select("week_start,week_end,analysis")
            .order("week_start", desc=True)
            .limit(limit)
            .execute()
        )
        rows = result.data or []
        if not rows:
            return ""
        parts = []
        for r in reversed(rows):
            parts.append(
                f"Неделя {r['week_start']}–{r['week_end']}:\n{r['analysis'][:300]}..."
            )
        return "\n\n".join(parts)
    except Exception as e:
        logger.error("db_get_previous_insights failed: %s", e)
        return ""


# ─── Weekly analytics handlers ────────────────────────────────────────────────

async def send_weekly_prompt(app) -> None:
    """Scheduled: every Sunday 20:00 Bangkok."""
    chat_ids = load_chat_ids()
    if not chat_ids:
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Готово, анализируй", callback_data=CALLBACK_WEEKLY)
    ]])
    for cid in chat_ids:
        try:
            await app.bot.send_message(
                chat_id=cid,
                text=(
                    "📊 Время проверять стату за неделю!\n"
                    "Заполни таблицу данными за эту неделю и нажми кнопку."
                ),
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error("weekly_prompt failed for %s: %s", cid, e)


async def callback_analyze_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'Готово, анализируй' button press."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📊 Читаю Google Sheets...")

    chat_id = query.message.chat_id

    # Read sheet (run in thread to avoid blocking event loop)
    try:
        rows = await asyncio.to_thread(read_google_sheet, SHEET_ID)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Ошибка чтения таблицы:\n{e}")
        return

    if len(rows) < 2:
        await context.bot.send_message(chat_id=chat_id, text="Таблица пуста — добавь данные и попробуй снова.")
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Прочитано {len(rows)-1} строк. Анализирую с помощью Claude..."
    )

    # Dates
    now = datetime.datetime.now(BANGKOK_TZ)
    week_end = now.date()
    week_start = week_end - datetime.timedelta(days=6)

    # Previous weeks context
    prev_context = db_get_previous_insights(limit=3)
    prev_block = f"\n\nДАННЫЕ ПРЕДЫДУЩИХ НЕДЕЛЬ (для сравнения трендов):\n{prev_context}" if prev_context else ""

    table_text = format_sheet_for_claude(rows)

    analysis = await asyncio.to_thread(
        ask_openrouter,
        build_system_prompt(
            "Ты аналитик еженедельной статистики Reddit-продвижения для создателя контента. "
            "Отвечай на русском, конкретно и actionable."
        ),
        f"Данные за неделю {week_start.strftime('%d.%m')}–{week_end.strftime('%d.%m.%Y')}:\n\n"
        f"{table_text}"
        f"{prev_block}\n\n"
        "Проанализируй по структуре:\n"
        "1. ТОП КОНТЕНТ — какой тип дал больше апвоутов\n"
        "2. ТОП САБРЕДДИТЫ — что сработало лучше всего\n"
        "3. ОБРАЗ / ПАРИК — какой стиль заходит лучше\n"
        "4. ВРЕМЯ ПОСТИНГА — когда лучший отклик\n"
        "5. ПОДПИСЧИКИ OF/FANSLY — сколько пришло и откуда\n"
        "6. ТРЕНД — растём / падаем / стагнация, почему\n"
        "7. РЕКОМЕНДАЦИИ — 3 конкретных действия на следующую неделю",
        1400,
    )

    # Send report
    header = (
        f"📊 Аналитика {week_start.strftime('%d.%m')}–{week_end.strftime('%d.%m.%Y')}\n\n"
    )
    full = header + analysis
    await send_long_message(context.bot, chat_id, full)

    # Save to Supabase
    db_save_weekly_insight(week_start, week_end, rows, analysis)
    await context.bot.send_message(chat_id=chat_id, text="Выводы сохранены в Supabase.")


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual trigger: send the weekly stats prompt with button."""
    save_chat_id(update.effective_chat.id)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Готово, анализируй", callback_data=CALLBACK_WEEKLY)
    ]])
    await update.message.reply_text(
        "📊 Время проверять стату за неделю!\n"
        "Заполни таблицу данными за эту неделю и нажми кнопку.",
        reply_markup=keyboard,
    )


# ─── Formatting helpers ───────────────────────────────────────────────────────

def analyze_post_format(posts: list[dict]) -> str:
    """Return '📷 Фото (N/M)' or '🎥 Видео (N/M)' based on post metadata."""
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
    """Pack complete text blocks into messages ≤ max_len chars, never splitting a block."""
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
    """Split text into ≤4000-char messages and send them sequentially.
    Content is NEVER truncated — continues in the next message.
    Split priority: double newline → single newline → space (word boundary)."""
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
            # Block itself may exceed MAX — split by single newline
            for line in block.split("\n"):
                sep2 = "\n" if current else ""
                if len(current) + len(sep2) + len(line) <= MAX:
                    current += sep2 + line
                else:
                    if current:
                        chunks.append(current)
                        current = ""
                    # Line itself may exceed MAX — split by words
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
    """
    Fetch top weekly posts for each competitor, run vision + 3-level Claude analysis.
    Saves insights to Supabase. Returns formatted report string.
    """
    now = datetime.datetime.now(BANGKOK_TZ)
    week_start = now.date() - datetime.timedelta(days=now.weekday())
    known_lower = {s.lower() for s in known_subs}

    # ── Fetch data for every competitor ──────────────────────────────────────
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

        # Extract subreddits used by this competitor
        used_subs: list[str] = list(dict.fromkeys(
            p.get("subreddit") or p.get("subreddit_name_prefixed", "").lstrip("r/")
            for p in posts
            if p.get("subreddit") or p.get("subreddit_name_prefixed")
        ))
        new_subs = [s for s in used_subs if s.lower() not in known_lower]

        # Best post this week
        top_post = max(posts, key=lambda p: p.get("score") or p.get("ups") or 0) if posts else None

        # Vision: download top-2 images
        img_urls = extract_image_urls(posts[:6])
        b64_images: list[str] = []
        for url in img_urls[:2]:
            b64 = await asyncio.to_thread(download_image_base64, url)
            if b64:
                b64_images.append(b64)

        vision_text = ""
        if b64_images:
            try:
                adapt_hint = (
                    "Она очень похожа на нас внешне — анализируй максимально детально."
                    if copy_ok else
                    "У неё грудь больше — акцент на декольте НАМ не подходит, но стиль и ракурсы разбери."
                )
                vision_text = await asyncio.to_thread(
                    ask_openrouter_vision,
                    b64_images,
                    f"Это посты конкурента @{username} на Reddit.\n{_compact_profile()}\n"
                    f"Описание конкурента: {notes}. {adapt_hint}\n"
                    "За 3-4 предложения: поза, свет, ракурс, стиль контента. "
                    "Что именно можно воспроизвести с нашей внешностью?",
                )
            except Exception as e:
                logger.warning("Vision failed for @%s: %s", username, e)
                vision_text = "Визуал недоступен."

        competitor_data.append({
            "username": username,
            "notes": notes,
            "copy_style": copy_ok,
            "posts": posts,
            "used_subs": used_subs,
            "new_subs": new_subs,
            "top_post": top_post,
            "vision": vision_text,
        })

    if not competitor_data:
        return "📊 КОНКУРЕНТНЫЙ АНАЛИЗ\n\nДанные недоступны."

    # ── Build Claude prompt ───────────────────────────────────────────────────
    comp_ctx_parts: list[str] = []
    for cd in competitor_data:
        top_posts_lines = "\n".join(
            f"  [{p.get('score') or p.get('ups', 0)} апв] r/{p.get('subreddit','?')}: "
            f"{p.get('title','')[:80]}"
            for p in sorted(cd["posts"],
                            key=lambda p: p.get("score") or p.get("ups") or 0,
                            reverse=True)[:5]
        ) or "  нет данных"

        comp_ctx_parts.append(
            f"КОНКУРЕНТ: @{cd['username']}\n"
            f"Описание: {cd['notes']}\n"
            f"copy_style={cd['copy_style']} (True = копируем детально, False = берём только стиль/сабы)\n"
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
                "Пиши на русском, структурированно и actionable.\n\n"
                + our_karma_ctx
            ),
            f"ДАННЫЕ КОНКУРЕНТОВ:\n\n" + "\n\n---\n\n".join(comp_ctx_parts) + "\n\n"
            "Составь трёхуровневый анализ СТРОГО в этом формате:\n\n"
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
            "- Что адаптировать под нашу внешность (учитывай copy_style каждого конкурента)\n"
            "- Сравни карму конкурентов с нашими аккаунтами: "
            f"наш NSFW {nsfw_acc.get('post_karma',0):,} карм — открывает ли это сабы которые они используют?\n"
            "LEVEL2_END\n\n"
            "LEVEL3_START\n"
            "NEW_SUBS: sub1,sub2,sub3\n"
            "NICHES: ниша1; ниша2; ниша3\n"
            "IDEAS: идея1; идея2; идея3\n"
            "PRIORITY_SUBS: сабы где конкуренты набирают 500+ апвоутов\n"
            "LEVEL3_END",
            max_tokens=1800,
        )
    except Exception as e:
        logger.error("Competitor Claude analysis failed: %s", e)
        raw_analysis = ""

    # ── Parse Level 3 ─────────────────────────────────────────────────────────
    new_subs_db: list[str] = []
    niches_db:   list[str] = []
    ideas_db:    list[str] = []
    priority_db: list[str] = []

    in_l3 = False
    for line in raw_analysis.splitlines():
        s = line.strip()
        if "LEVEL3_START" in s:
            in_l3 = True; continue
        if "LEVEL3_END"  in s:
            in_l3 = False; continue
        if not in_l3:
            continue
        up = s.upper()
        if up.startswith("NEW_SUBS:"):
            new_subs_db   = [x.strip() for x in s.split(":",1)[1].split(",") if x.strip()]
        elif up.startswith("NICHES:"):
            niches_db     = [x.strip() for x in s.split(":",1)[1].split(";") if x.strip()]
        elif up.startswith("IDEAS:"):
            ideas_db      = [x.strip() for x in s.split(":",1)[1].split(";") if x.strip()]
        elif up.startswith("PRIORITY_SUBS:"):
            priority_db   = [x.strip() for x in s.split(":",1)[1].split(",") if x.strip()]

    # ── Save to Supabase ──────────────────────────────────────────────────────
    for cd in competitor_data:
        db_save_competitor_insight(
            week_start     = week_start,
            username       = cd["username"],
            new_subreddits = cd["new_subs"],
            top_subreddits = cd["used_subs"],
            content_ideas  = ideas_db,
            priority_subs  = priority_db,
            raw_analysis   = raw_analysis,
        )

    # ── Format Telegram message ───────────────────────────────────────────────
    out: list[str] = ["📊 КОНКУРЕНТНЫЙ АНАЛИЗ\n"]

    for cd in competitor_data:
        tp = cd["top_post"]
        top_title = (tp.get("title") or "—")[:60]   if tp else "—"
        top_score = (tp.get("score") or tp.get("ups") or 0) if tp else 0
        top_sub   = (tp.get("subreddit") or "?")     if tp else "?"
        copy_mark = "✅ Похожа на нас — копируем детально" if cd["copy_style"] else "⚠️ Копируем стиль, не анатомию"

        out.append(f"━━━ @{cd['username']} ━━━")
        out.append(f"🔥 Топ пост: {top_title} — {top_score} апв (r/{top_sub})")
        out.append(f"📍 Активные сабы: {', '.join(cd['used_subs'][:7]) or 'нет данных'}")
        if cd["new_subs"]:
            out.append(f"🆕 Новые для нас: {', '.join(cd['new_subs'][:6])}")
        out.append(f"👁️ Визуал: {cd['vision'][:220] if cd['vision'] else '—'}")
        out.append(f"{copy_mark}\n")

    # Extract Level 1 and Level 2 blocks from Claude output
    l1_lines: list[str] = []
    l2_lines: list[str] = []
    in_l1 = in_l2 = False
    for line in raw_analysis.splitlines():
        s = line.strip()
        if "LEVEL1_START" in s: in_l1 = True;  continue
        if "LEVEL1_END"  in s: in_l1 = False; continue
        if "LEVEL2_START" in s: in_l2 = True;  continue
        if "LEVEL2_END"  in s: in_l2 = False; continue
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

    # All new subs discovered across competitors
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


# ─── Daily plan pipeline ───────────────────────────────────────────────────────

async def run_weekly_plan(app) -> None:
    chat_ids = load_chat_ids()
    if not chat_ids:
        logger.warning("No registered chats — skipping weekly plan")
        return

    now = datetime.datetime.now(BANGKOK_TZ)
    week_str = now.strftime("неделя %d.%m.%Y")
    date_str  = now.strftime("%d.%m.%Y")
    reset_tokens()

    async def broadcast(text: str) -> None:
        for cid in chat_ids:
            try:
                await app.bot.send_message(chat_id=cid, text=text)
            except Exception as e:
                logger.error("Broadcast failed for %s: %s", cid, e)

    await broadcast(f"Доброе утро! Генерирую еженедельный план на {week_str}...")

    # ── Step 0: web trend search ──────────────────────────────────────────────
    await broadcast("Шаг 0: ищу актуальные тренды...")
    trend_keywords: list[str] = []
    trend_summary = ""
    discovered_subs: list[str] = []
    try:
        trend_keywords, trend_summary = step0_search_trends()
        discovered_subs = step0_find_subs_from_keywords(trend_keywords)
        await broadcast(
            f"Шаг 0 готов. Трендов: {len(trend_keywords)}, "
            f"новых сабов: {len(discovered_subs)}"
        )
    except Exception as e:
        logger.error("Step 0 failed: %s", e)
        await broadcast("Шаг 0: веб-поиск не удался, продолжаю.")

    # ── Step 1: select top-15 subs ────────────────────────────────────────────
    db_subs = db_get_active_sub_names()
    base_pool = db_subs if db_subs else ALL_SUBS
    combined_pool = list(dict.fromkeys(base_pool + discovered_subs))
    subs_joined  = ", ".join(combined_pool)
    trend_context = (
        f"Тренды недели: {trend_summary}\nКлючевые темы: {', '.join(trend_keywords[:12])}\n\n"
        if trend_keywords else ""
    )
    try:
        raw = ask_openrouter(
            build_system_prompt("Ты Reddit-стратег для создателя контента для взрослых."),
            f"{trend_context}Из списка выбери топ-15 сабреддитов для OnlyFans/Fansly продвижения "
            f"на эту неделю. Список: {subs_joined}. "
            "Верни только названия через запятую, без r/, без объяснений.",
            max_tokens=256,
        )
        selected = [s.strip().lstrip("r/") for s in raw.split(",")][:20]
        valid = {s.lower() for s in combined_pool}
        selected = [s for s in selected if s.lower() in valid] or combined_pool[:20]
    except Exception as e:
        logger.error("Step 1 failed: %s", e)
        selected = combined_pool[:20]

    # Filter: banned subs, drawn content, banned mods
    filtered_selected: list[str] = []
    for s in selected:
        if not is_sub_allowed(s):
            logger.info("Filtered out r/%s from plan", s)
            continue
        filtered_selected.append(s)
        if len(filtered_selected) == 15:
            break
    selected = filtered_selected or selected[:15]

    await broadcast("Шаг 1 готов: " + ", ".join(f"r/{s}" for s in selected))

    # ── Step 2: fetch top-10 posts (week) + rules + format ───────────────────
    subs_data: dict[str, dict] = {}
    low_traffic: list[str] = []
    for sub in selected:
        try:
            posts = fetch_top_posts_week(sub, limit=10)
            rules = fetch_subreddit_rules(sub)
        except Exception as e:
            logger.warning("Step 2 r/%s: %s", sub, e)
            posts, rules = [], "unavailable"
        sub_avg = avg_upvotes(posts)
        if posts and sub_avg < UPVOTES_SKIP:
            low_traffic.append(f"{sub}(avg {sub_avg:.0f})")
            logger.info("r/%s avg upvotes %.0f < %d — skipping", sub, sub_avg, UPVOTES_SKIP)
            continue
        if sub_avg < UPVOTES_MID:
            activity_label = "⚠️ Средняя активность"
        else:
            activity_label = "✅ Высокая активность"
        fmt = analyze_post_format(posts)
        video_policy = detect_video_policy(posts)
        subs_data[sub] = {
            "posts": posts, "rules": rules, "format": fmt,
            "video_policy": video_policy,
            "activity": activity_label, "avg_upvotes": round(sub_avg),
        }
    if low_traffic:
        await broadcast(f"Исключены (avg < {UPVOTES_SKIP} апвоутов/неделю): " + ", ".join(low_traffic))
    await broadcast("Шаг 2 готов. Посты и правила загружены.")

    # ── Step 3: single vision call ────────────────────────────────────────────
    all_imgs: list[tuple[int, str, str]] = []
    for sub, d in subs_data.items():
        for post in d.get("posts", []):
            urls = extract_image_urls([post])
            if urls:
                all_imgs.append((post.get("score") or 0, sub, urls[0]))
    all_imgs.sort(key=lambda x: x[0], reverse=True)
    vision_analysis = ""
    top3 = all_imgs[:3]
    if top3:
        b64s = [b for _, _, u in top3 if (b := download_image_base64(u))]
        if b64s:
            src = " | ".join(f"r/{s}(👍{sc})" for sc, s, _ in top3[:len(b64s)])
            try:
                vision_analysis = ask_openrouter_vision(
                    b64s,
                    f"{_compact_profile()}\nФото из: {src}\n"
                    "3-4 предложения: поза, стиль, что воспроизвести."
                )
            except Exception as e:
                logger.warning("Vision failed: %s", e)
    await broadcast("Шаг 3 готов. Визуальный анализ завершён.")

    # ── Step 4: generate overview + subgroups ─────────────────────────────────
    ctx_lines = []
    for sub in selected:
        if sub not in subs_data:
            continue
        d = subs_data[sub]
        titles = " | ".join(p.get("title","")[:35] for p in d["posts"][:2])
        ctx_lines.append(
            f"r/{sub}[{d['format']}][{d['activity']} avg:{d['avg_upvotes']}]: "
            f"{titles} | rules:{d['rules'][:60]}"
        )

    vision_ctx = f"\nВизуал: {vision_analysis[:180]}" if vision_analysis else ""

    try:
        raw_groups = ask_openrouter(
            build_system_prompt("Ты Reddit-стратег, отвечай на русском.\n\n" + _accounts_block()),
            f"Неделя {date_str}. Данные:\n" + "\n".join(ctx_lines) + vision_ctx + "\n\n"
            "Верни ТОЛЬКО в таком формате (ничего лишнего):\n"
            "OVERVIEW: [3-5 предложений — фокус недели, что модно, на что бить. "
            f"Упомяни распределение по аккаунтам: SFW @{_get_account('SFW').get('username')} / "
            f"NSFW @{_get_account('NSFW').get('username')}]\n"
            "GROUP: [эмодзи Название] | sub1,sub2,sub3\n"
            "GROUP: ...\n"
            "Сделай 4-5 групп. Все 15 сабов распредели по группам.",
            max_tokens=450,
        )
    except Exception as e:
        logger.error("Step 4 groups failed: %s", e)
        raw_groups = f"OVERVIEW: Еженедельный план.\nGROUP: 🔥 Все сабы | {','.join(selected)}"

    # Parse overview + groups
    overview = ""
    subgroups: list[dict] = []
    for line in raw_groups.splitlines():
        line = line.strip()
        if line.upper().startswith("OVERVIEW:"):
            overview = line.split(":", 1)[1].strip()
        elif line.upper().startswith("GROUP:"):
            rest = line.split(":", 1)[1].strip()
            if "|" in rest:
                name_part, subs_part = rest.split("|", 1)
                group_subs = [s.strip().lstrip("r/") for s in subs_part.split(",") if s.strip()]
                if group_subs:
                    subgroups.append({"name": name_part.strip(), "subs": group_subs})

    if not subgroups:
        subgroups = [{"name": "🔥 Все сабы", "subs": selected}]

    # ── Step 5: send first message with inline buttons ─────────────────────────
    week_label = now.strftime("%d.%m")
    header_text = f"📅 ПЛАН НА НЕДЕЛЮ — {week_label}\n\n{overview}\n\nВыбери категорию:"

    buttons = [
        [InlineKeyboardButton(sg["name"], callback_data=f"{CALLBACK_SUBGROUP}_{i}")]
        for i, sg in enumerate(subgroups)
    ]
    keyboard = InlineKeyboardMarkup(buttons)

    for cid in chat_ids:
        try:
            await app.bot.send_message(chat_id=cid, text=header_text, reply_markup=keyboard)
            # Store plan in cache for this chat (evict old entries if cache grows too large)
            if len(_weekly_cache) > 50:
                oldest = next(iter(_weekly_cache))
                del _weekly_cache[oldest]
            _weekly_cache[cid] = {
                "subgroups": subgroups,
                "subs_data": subs_data,
                "vision": vision_analysis,
                "date_str": date_str,
                "trend_context": trend_context,
            }
        except Exception as e:
            logger.error("Failed sending weekly plan to %s: %s", cid, e)

    await broadcast(f"План готов. {token_summary()}")

    # ── Step 6: competitor analysis ───────────────────────────────────────────
    await broadcast("🔍 Анализирую конкурентов...")
    known_sub_names: set[str] = set(combined_pool)
    try:
        competitor_report = await run_competitor_analysis(known_sub_names)
    except Exception as e:
        logger.error("Competitor analysis failed: %s", e)
        competitor_report = "📊 Конкурентный анализ: не удалось выполнить."

    for cid in chat_ids:
        try:
            await send_long_message(app.bot, cid, competitor_report)
        except Exception as e:
            logger.error("Failed sending competitor report to %s: %s", cid, e)

    await broadcast(f"Всё готово. Итого: {token_summary()}")


# ─── Subgroup detail callback ──────────────────────────────────────────────────

async def callback_weekly_subgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id

    # Parse group index from callback_data like "wk_sg_2"
    try:
        group_idx = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        await query.answer("Ошибка данных кнопки")
        return

    await query.answer()  # answer only once, after successful parsing

    cache = _weekly_cache.get(chat_id)
    if not cache:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Данные плана устарели. Запусти /plan заново."
        )
        return

    subgroups  = cache["subgroups"]
    subs_data  = cache["subs_data"]
    date_str   = cache["date_str"]
    trend_ctx  = cache.get("trend_context", "")

    if group_idx >= len(subgroups):
        await context.bot.send_message(chat_id=chat_id, text="Группа не найдена.")
        return

    group      = subgroups[group_idx]
    group_name = group["name"]
    group_subs = [s for s in group["subs"] if s in subs_data]

    if not group_subs:
        await context.bot.send_message(chat_id=chat_id, text=f"{group_name}: нет данных по сабам.")
        return

    await context.bot.send_message(chat_id=chat_id, text=f"Генерирую план для {group_name}...")

    # Build context for this subgroup
    sub_ctx = []
    for sub in group_subs:
        d = subs_data[sub]
        titles = " | ".join(p.get("title", "")[:60] for p in d["posts"][:3])
        video_pol = d.get("video_policy", "📹 Видео: уточни")
        activity = d.get("activity", "")
        avg_up = d.get("avg_upvotes", 0)
        sub_ctx.append(
            f"r/{sub}\n"
            f"  Активность: {activity} (avg {avg_up} апвоутов/неделю)\n"
            f"  Формат: {d['format']} | {video_pol}\n"
            f"  Топ-посты: {titles}\n"
            f"  ПОЛНЫЕ ПРАВИЛА:\n{d['rules']}"
        )

    sfw_acc  = _get_account("SFW")
    nsfw_acc = _get_account("NSFW")

    system_with_rules = build_system_prompt(
        f"Ты Reddit-стратег, составляешь детальный план для группы '{group_name}'. "
        "Отвечай на русском.\n\n"
        f"{_accounts_block()}\n\n"
        "ACCOUNT SELECTION RULE:\n"
        f"Для каждого саба выбирай ОДИН аккаунт:\n"
        f"• SFW сабы (косплей, альт, лайфстайл, без наготы) → @{sfw_acc.get('username')} ({sfw_acc.get('post_karma',0):,} karma)\n"
        f"• NSFW сабы (explicit, фетиш, ахегао, грудь, взрослый контент) → @{nsfw_acc.get('username')} ({nsfw_acc.get('post_karma',0):,} karma)\n"
        f"Если саб требует высокую карму — у NSFW аккаунта {nsfw_acc.get('post_karma',0):,} кармы, это открывает доступ к строгим сабам.\n\n"
        "STRICT RULES ENFORCEMENT:\n"
        "Перед планом каждого саба ты ОБЯЗАН:\n"
        "1. Прочитать ВСЕ правила саба\n"
        "2. Проверить каждый пункт плана против КАЖДОГО правила\n"
        "3. Если что-то нарушает правила — НЕ предлагать это\n"
        "4. Для каждого правила указать: ✅ Правило #N — ок ИЛИ ❌ Правило #N — [что нарушает, что убрали]\n"
        "5. Предложить альтернативу для всего убранного\n\n"
        "CONTENT TYPE RULE:\n"
        "Мы постим ТОЛЬКО реальные фото/видео реальных людей.\n"
        "Никакого рисованного/аниме арт контента в плане.\n\n"
        "UPVOTE RULE:\n"
        "Упоминай только контент-стратегии которые реально набирают 100+ апвоутов в этом сабе."
    )

    try:
        detail = await asyncio.to_thread(
            ask_openrouter,
            system_with_rules,
            f"{trend_ctx}Неделя {date_str}. Группа: {group_name}\n\n"
            "Данные по сабам:\n\n" + "\n\n".join(sub_ctx) + "\n\n"
            f"Для каждого из {len(group_subs)} сабов напиши блок СТРОГО в формате:\n\n"
            "━━━ r/название ━━━\n\n"
            "⚠️ Проверка правил r/название:\n"
            "✅ Правило #1 — ок\n"
            "✅ Правило #2 — ок\n"
            "❌ Правило #N — [нарушение], убрали [X] из плана\n"
            "[все правила]\n\n"
            f"👤 Аккаунт: [@username] ([karma] кармы) — [одна строка почему этот аккаунт]\n\n"
            "📌 Что постить: [конкретно, соответствует правилам]\n"
            "🎬 Как снять: [поза, свет, ракурс]\n"
            "✍️ Подпись: [готовый текст]\n"
            "[video_policy из данных]\n"
            "⏰ Время: [лучшее время UTC+7]\n\n"
            "⚠️ CRITICAL REQUIREMENTS:\n"
            "- Верификация: [нужна / не нужна]\n"
            "- Уровень наготы: [конкретно что разрешено]\n"
            "- Тип контента: [что именно требуется]\n\n"
            "❌ HARD BANS:\n"
            "- [список что категорически нельзя]\n\n"
            "✅ WHAT WORKS:\n"
            "- [конкретные примеры контента который точно пройдёт]\n\n"
            "Каждый блок отделяй строкой ━━━. Пиши чётко и конкретно.",
            max_tokens=2500,
        )
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Ошибка генерации: {e}")
        return

    # Split by complete sub blocks — never break inside a block
    header = f"{group_name} — план на неделю {date_str}\n"

    # Split by the "━━━" marker, keeping each sub block intact
    sub_blocks: list[str] = []
    for block in detail.split("\n"):
        if block.startswith("━━━"):
            sub_blocks.append(block)
        elif sub_blocks:
            sub_blocks[-1] += "\n" + block

    # Merge short trailing lines into last block
    final_blocks = [b.strip() for b in sub_blocks if b.strip()]
    if not final_blocks:
        # Fallback: send as-is
        final_blocks = [detail]

    messages = split_by_blocks(header, final_blocks)
    for msg in messages:
        await send_long_message(context.bot, chat_id, msg)


# ─── Telegram command handlers ─────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    save_chat_id(update.effective_chat.id)
    text = (
        "Привет! Я бот для анализа Reddit-контента.\n\n"
        "Команды:\n"
        "/ping — проверить все подключения\n"
        "/test — топ-5 постов из r/cosplay\n"
        "/digest — дайджест топ-3 постов по всем категориям\n"
        "/findsubs — найти новые сабреддиты (10k+ подписчиков)\n"
        "/plan — еженедельный план прямо сейчас\n"
        "/competitors — конкурентный анализ прямо сейчас\n"
        "/weekly — аналитика за неделю (из Google Sheets)\n"
        "/ask [вопрос] — вопрос по Reddit-данным\n\n"
        "Авто: план + конкурентный анализ по понедельникам в 09:00, "
        "аналитика по воскресеньям в 20:00 Bangkok."
    )
    await update.message.reply_text(text)


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
            url = post.get("url") or post.get("permalink") or ""
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
        lines = [f"📂 {category}"]
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
    r = requests.get(
        "https://www.reddit.com/subreddits/search.json",
        headers=REDDIT_HEADERS,
        params={"q": keyword, "limit": limit},
        timeout=10,
    )
    r.raise_for_status()
    return [c["data"] for c in r.json().get("data", {}).get("children", [])]


def filter_by_subscribers(subs: list[dict], min_subs: int = 10_000) -> list[dict]:
    return [s for s in subs if (s.get("subscribers") or 0) >= min_subs]


async def cmd_findsubs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Ищу сабреддиты по {len(FINDSUBS_KEYWORDS)} ключевым словам..."
    )
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

    await update.message.reply_text(
        f"Найдено {len(seen)} сабреддитов. Отправляю на анализ Claude..."
    )

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
        fallback = "\n".join(
            f"r/{s['display_name']} — {s.get('subscribers', 0):,}"
            for s in subs_list[:10]
        )
        await update.message.reply_text(f"Claude недоступен. Топ-10 по подписчикам:\n\n{fallback}")


async def cmd_competitors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual trigger: run competitor analysis right now."""
    save_chat_id(update.effective_chat.id)
    await update.message.reply_text(
        f"🔍 Анализирую {len(COMPETITORS)} конкурентов...\n"
        "Это займёт ~1 минуту."
    )
    try:
        known = set(ALL_SUBS) | set(await asyncio.to_thread(db_get_active_sub_names))
        report = await run_competitor_analysis(known)
        await send_long_message(context.bot, update.effective_chat.id, report)
    except Exception as e:
        logger.exception("Error in /competitors")
        await update.message.reply_text(f"Ошибка: {e}")


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    save_chat_id(update.effective_chat.id)
    await update.message.reply_text("Запускаю еженедельный план...")
    await run_weekly_plan(context.application)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    save_chat_id(update.effective_chat.id)
    await update.message.reply_text("🔍 Проверка систем...")

    results: list[str] = []
    failed: list[str] = []

    # 1. ScrapeCreators API
    try:
        t0 = datetime.datetime.now()
        r = requests.get(
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

    # 2. OpenRouter (Claude Sonnet)
    try:
        t0 = datetime.datetime.now()
        r = requests.post(
            OPENROUTER_BASE,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://reddit-analyzer-bot",
                "X-Title": "Reddit Analyzer Bot",
            },
            json={
                "model": "anthropic/claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
            },
            timeout=30,
        )
        r.raise_for_status()
        ms = int((datetime.datetime.now() - t0).total_seconds() * 1000)
        results.append(f"🤖 Claude Sonnet: ✅ {ms}ms")
    except Exception as e:
        results.append(f"🤖 Claude Sonnet: ❌ {e}")
        failed.append("Claude Sonnet")

    # 3. Supabase
    try:
        t0 = datetime.datetime.now()
        get_sb().table("subreddits").select("name").limit(1).execute()
        ms = int((datetime.datetime.now() - t0).total_seconds() * 1000)
        results.append(f"🗄️ Supabase: ✅ {ms}ms")
    except Exception as e:
        results.append(f"🗄️ Supabase: ❌ {e}")
        failed.append("Supabase")

    # 4. Google Sheets
    try:
        t0 = datetime.datetime.now()
        read_google_sheet(SHEET_ID)
        ms = int((datetime.datetime.now() - t0).total_seconds() * 1000)
        results.append(f"📊 Google Sheets: ✅ {ms}ms")
    except Exception as e:
        results.append(f"📊 Google Sheets: ❌ {str(e)[:100]}")
        failed.append("Google Sheets")

    # 5. Telegram Bot
    try:
        bot_info = await context.bot.get_me()
        chat_id = update.effective_chat.id
        results.append(f"🤖 Telegram: ✅ @{bot_info.username} | chat_id: {chat_id}")
    except Exception as e:
        results.append(f"🤖 Telegram: ❌ {e}")
        failed.append("Telegram")

    if not failed:
        footer = "Все системы работают 🟢"
    else:
        footer = f"⚠️ Проблемы обнаружены: {', '.join(failed)}"

    await update.message.reply_text(
        "🔍 Проверка систем...\n\n" + "\n".join(results) + f"\n\n{footer}"
    )


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
        posts = await asyncio.to_thread(fetch_reddit_posts, "cosplay", 10)
        posts_text = "\n".join(
            f"- {p.get('title', '')} (score: {p.get('score', 0)})" for p in posts
        )
        answer = await asyncio.to_thread(
            ask_openrouter,
            build_system_prompt("Ты Reddit-аналитик для создателя контента для взрослых. Отвечай на русском."),
            f"Данные r/cosplay:\n{posts_text}\n\nВопрос: {question}",
        )
        await update.message.reply_text(answer)
    except Exception as e:
        logger.exception("Error in /ask")
        await update.message.reply_text(f"Ошибка: {e}")


# ─── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Diagnostic: verify env vars are loaded correctly
    gkey_preview = (GOOGLE_API_KEY[:10] + "…") if GOOGLE_API_KEY else "НЕ ЗАДАН"
    logger.info("GOOGLE_API_KEY (первые 10 симв.): %s", gkey_preview)
    logger.info("SHEET_ID: %s", SHEET_ID or "НЕ ЗАДАН")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("ping",        cmd_ping))
    app.add_handler(CommandHandler("test",        cmd_test))
    app.add_handler(CommandHandler("digest",      cmd_digest))
    app.add_handler(CommandHandler("findsubs",    cmd_findsubs))
    app.add_handler(CommandHandler("plan",        cmd_plan))
    app.add_handler(CommandHandler("weekly",      cmd_weekly))
    app.add_handler(CommandHandler("ask",         cmd_ask))
    app.add_handler(CommandHandler("competitors", cmd_competitors))
    app.add_handler(CallbackQueryHandler(callback_analyze_weekly,  pattern=f"^{CALLBACK_WEEKLY}$"))
    app.add_handler(CallbackQueryHandler(callback_weekly_subgroup, pattern=f"^{CALLBACK_SUBGROUP}_\\d+$"))

    # ── Supabase init ──────────────────────────────────────────────────────────
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
        run_weekly_plan,
        trigger=CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=BANGKOK_TZ),
        args=[app],
        id="weekly_plan",
        replace_existing=True,
    )
    scheduler.add_job(
        send_weekly_prompt,
        trigger=CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=BANGKOK_TZ),
        args=[app],
        id="weekly_prompt",
        replace_existing=True,
    )

    async def on_startup(application) -> None:
        scheduler.start()
        logger.info("Scheduler started. Daily plan at 09:00 Asia/Bangkok.")

    app.post_init = on_startup
    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
