"""YouTube MCP Server.

Tools for reading a channel's latest videos and fetching video transcripts.
No API key required — channel listing uses YouTube's public RSS feed and
transcripts come from youtube-transcript-api, with a yt-dlp fallback.

Running on a VPS? YouTube blocks datacenter IPs hard. Everything in the
"Anti-block configuration" section below exists for that; `check-youtube-access`
(or `python server.py --check`) reports what this host can actually reach.
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import random
import hashlib
import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse, parse_qs

import requests
from defusedxml import ElementTree as ET
from mcp.server import Server
from mcp.types import Tool, TextContent, ToolAnnotations
from mcp.server.stdio import stdio_server

from youtube_transcript_api import YouTubeTranscriptApi

# ---------------------------------------------------------------------------
# Logging (stderr only — stdout is reserved for the MCP protocol)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("YT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("youtube-mcp")

DEFAULT_LANGUAGES = [
    "en", "es", "fr", "pt", "it", "de", "id", "zh", "zh-Hans", "zh-Hant",
    "ko", "ja", "ar", "hi", "bn", "ru", "tr", "nl", "pl", "sw", "yo",
]

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

RSS_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
# YouTube's RSS feed never returns more than this many entries, whatever we ask.
RSS_MAX_ENTRIES = 15
YT_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

# A short, permanently-available video used only by the diagnostics tool.
PROBE_VIDEO_ID = "jNQXAC9IVRw"


# ---------------------------------------------------------------------------
# Environment parsing
# ---------------------------------------------------------------------------
def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer, using %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number, using %s", name, raw, default)
        return default


def _env_list(name: str, default: Sequence[str] = ()) -> List[str]:
    raw = _env_str(name)
    if not raw:
        return list(default)
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]


# ---------------------------------------------------------------------------
# Anti-block configuration — all optional, all via env. See README.
# ---------------------------------------------------------------------------
# One or more proxy URLs (comma-separated). The pool is rotated on every block,
# so a list of cheap residential proxies degrades gracefully instead of dying.
_PROXY_URLS = _env_list("YT_PROXY")
if not _PROXY_URLS:
    _inherited = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    _PROXY_URLS = [_inherited] if _inherited else []

# Webshare's rotating residential pool — what youtube-transcript-api itself
# recommends, and the least-effort fix for a flagged datacenter IP.
_WEBSHARE_USER = _env_str("YT_WEBSHARE_USERNAME")
_WEBSHARE_PASS = _env_str("YT_WEBSHARE_PASSWORD")

# Netscape cookies.txt — an authenticated session is blocked far less often.
_COOKIES = _env_str("YT_COOKIES") or None

# yt-dlp player clients, tried in order. YouTube's bot check is aimed squarely
# at the `web` client; the TV/embedded/mobile clients are policed far more
# loosely and often still answer from an IP that `web` is blocked on. This is
# the most effective lever that costs nothing.
_PLAYER_CLIENTS = _env_list("YT_PLAYER_CLIENTS", ("tv", "android_vr", "web_safari", "mweb", "web"))

# A PO token, if you run a provider for one (see README).
_PO_TOKEN = _env_str("YT_PO_TOKEN")

_RETRIES = max(1, _env_int("YT_RETRIES", 3))
_TIMEOUT = max(5, _env_int("YT_TIMEOUT", 30))

# Minimum seconds between outbound YouTube requests. Staying under the radar
# beats getting blocked and retrying; worth raising on a shared VPS.
_MIN_INTERVAL = max(0.0, _env_float("YT_MIN_INTERVAL", 0.0))

# On-disk transcript cache. Transcripts are immutable, so a hit costs YouTube
# nothing and works while the host is blocked. YT_CACHE_TTL=0 disables it.
_CACHE_DIR = Path(_env_str("YT_CACHE_DIR") or (Path.home() / ".cache" / "youtube-mcp"))
_CACHE_TTL = _env_int("YT_CACHE_TTL", 30 * 24 * 3600)

# Default output cap, in characters. 0 means unlimited — a three-hour video is
# 150k+ characters, which blows up any agent's context window, so the default
# is a cap rather than everything.
_DEFAULT_MAX_CHARS = max(0, _env_int("YT_MAX_CHARS", 8000))

# Group untimestamped transcripts into paragraphs of roughly this many seconds.
_PARAGRAPH_SECONDS = 30.0

# How long yt-dlp video metadata stays in memory. Long enough that strip_ads
# reuses the extraction the transcript fallback already paid for.
_INFO_TTL = 300.0
_CHANNEL_TTL = 3600.0


# ---------------------------------------------------------------------------
# Redaction — nothing with credentials in it reaches a log or the model
# ---------------------------------------------------------------------------
# Matches the "user:pass" in proxy URLs like http://user:pass@host:port.
_CREDS_RE = re.compile(r"(?<=//)[^/\s@]+(?=@)")
_MAX_ERR_CHARS = 400


def redact(text: Any) -> str:
    """Strip embedded credentials from anything headed for a log or the model.

    Proxy URLs carry a password and turn up verbatim in requests/yt-dlp error
    strings, which would otherwise be handed straight to the model.
    """
    return _CREDS_RE.sub("***:***", str(text))


def safe_err(err: Any) -> str:
    """One-line, redacted, length-capped rendering of an exception."""
    if err is None:
        return "none"
    label = f"{type(err).__name__}: {err}" if isinstance(err, BaseException) else str(err)
    msg = " ".join(redact(label).split())
    return msg[: _MAX_ERR_CHARS - 1] + "…" if len(msg) > _MAX_ERR_CHARS else msg


class TranscriptUnavailable(RuntimeError):
    """Every transcript path failed. The message is model-facing and redacted."""


# ---------------------------------------------------------------------------
# Throttling and proxy rotation
# ---------------------------------------------------------------------------
_net_lock = threading.Lock()
_last_request = 0.0
_proxy_index = 0


def _throttle() -> None:
    """Hold a floor of YT_MIN_INTERVAL seconds between outbound requests."""
    global _last_request
    if _MIN_INTERVAL <= 0:
        return
    with _net_lock:
        wait = _last_request + _MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def current_proxy() -> Optional[str]:
    return _PROXY_URLS[_proxy_index % len(_PROXY_URLS)] if _PROXY_URLS else None


def rotate_proxy() -> None:
    """Advance to the next proxy in the pool after a block."""
    global _proxy_index
    if len(_PROXY_URLS) > 1:
        _proxy_index += 1
        logger.info("Rotating proxy (%d of %d)", _proxy_index % len(_PROXY_URLS) + 1, len(_PROXY_URLS))


def _requests_proxies() -> Optional[Dict[str, str]]:
    url = current_proxy()
    return {"http": url, "https": url} if url else None


def _proxy_config():
    """Proxy config for youtube-transcript-api: Webshare pool if set, else YT_PROXY."""
    if _WEBSHARE_USER and _WEBSHARE_PASS:
        try:
            from youtube_transcript_api.proxies import WebshareProxyConfig
            return WebshareProxyConfig(proxy_username=_WEBSHARE_USER, proxy_password=_WEBSHARE_PASS)
        except Exception as e:
            logger.warning("Webshare proxy config unavailable: %s", safe_err(e))
    url = current_proxy()
    if not url:
        return None
    try:
        from youtube_transcript_api.proxies import GenericProxyConfig
        return GenericProxyConfig(http_url=url, https_url=url)
    except Exception as e:
        logger.warning("Proxy config unavailable: %s", safe_err(e))
        return None


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
_cookie_jar = None
_cookie_jar_loaded = False


def _load_cookie_jar():
    """Load YT_COOKIES once and reuse it.

    youtube-transcript-api no longer loads cookies itself (disabled upstream),
    so without this the primary path goes out unauthenticated even when
    YT_COOKIES is set. Parsing goes through yt-dlp's Netscape parser rather than
    the stdlib one: http.cookiejar.MozillaCookieJar silently drops lines
    prefixed with "#HttpOnly_", which is exactly where YouTube's auth cookies live.
    """
    global _cookie_jar, _cookie_jar_loaded
    if _cookie_jar_loaded:
        return _cookie_jar
    _cookie_jar_loaded = True
    if not _COOKIES:
        return None
    try:
        from yt_dlp.cookies import YoutubeDLCookieJar
        jar = YoutubeDLCookieJar(_COOKIES)
        jar.load(ignore_discard=True, ignore_expires=True)
        _cookie_jar = jar
        logger.info("Loaded %d cookies from YT_COOKIES", len(jar))
    except Exception as e:
        logger.warning("Could not load cookies from YT_COOKIES: %s", safe_err(e))
    return _cookie_jar


def _http_client() -> requests.Session:
    """Session with browser headers and the YT_COOKIES jar attached, if any."""
    session = requests.Session()
    session.headers.update(HTTP_HEADERS)
    jar = _load_cookie_jar()
    if jar is not None:
        session.cookies = jar
    return session


# ---------------------------------------------------------------------------
# Argument coercion
#
# Small function-calling models routinely send `null` for an omitted optional,
# a string where an array is declared, and "true" where a boolean is. Coercing
# instead of raising turns a whole class of wasted agent turns into no-ops.
# ---------------------------------------------------------------------------
def as_int(args: dict, key: str, default: int, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    value = args.get(key)
    if value is None or value == "":
        value = default
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def as_bool(args: dict, key: str, default: bool = False) -> bool:
    value = args.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def as_text(args: dict, key: str) -> str:
    """Required string argument, with an error the model can act on."""
    value = args.get(key)
    if value is None or not str(value).strip():
        raise ValueError(f"Missing required argument {key!r}.")
    return str(value).strip()


def as_languages(args: dict) -> List[str]:
    value = args.get("languages")
    if isinstance(value, str):
        value = [p.strip() for p in value.split(",")]
    if not isinstance(value, (list, tuple)):
        return list(DEFAULT_LANGUAGES)
    codes = [str(v).strip() for v in value if str(v).strip()]
    return codes or list(DEFAULT_LANGUAGES)


# ---------------------------------------------------------------------------
# Identifier parsing
# ---------------------------------------------------------------------------
_VIDEO_ID_RE = re.compile(r"[0-9A-Za-z_-]{11}")
_CHANNEL_ID_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")
_YOUTUBE_HOSTS = ("youtube.com", "youtube-nocookie.com", "youtu.be")


def _is_youtube_host(host: str) -> bool:
    host = host.lower()
    return any(host == h or host.endswith("." + h) for h in _YOUTUBE_HOSTS)


def extract_video_id(url_or_id: str) -> Optional[str]:
    """Return the 11-char video id from a YouTube URL or a bare id.

    Returns None rather than guessing. The loose "any 11 chars" fallback only
    applies to YouTube URLs — applied to arbitrary text it happily turned
    https://example.com/some-article into the video id "some-articl", and a
    playlist URL into a nonexistent video.
    """
    s = (url_or_id or "").strip()
    if not s:
        return None
    if _VIDEO_ID_RE.fullmatch(s):
        return s

    try:
        parsed = urlparse(s if "//" in s else "https://" + s)
    except ValueError:
        return None
    host = parsed.hostname or ""
    if not _is_youtube_host(host):
        return None

    if host.lower().endswith("youtu.be"):
        vid = parsed.path.lstrip("/").split("/")[0]
        return vid if _VIDEO_ID_RE.fullmatch(vid) else None

    query = parse_qs(parsed.query)
    for key in ("v", "video_id"):
        value = (query.get(key) or [""])[0]
        if _VIDEO_ID_RE.fullmatch(value):
            return value

    m = re.search(r"/(?:embed|shorts|live|v|e)/([0-9A-Za-z_-]{11})(?:[/?#]|$)", parsed.path)
    return m.group(1) if m else None


_channel_cache: Dict[str, tuple] = {}


def _channel_page_url(s: str) -> str:
    if s.startswith("http"):
        return s
    if s.startswith("@"):
        return f"https://www.youtube.com/{s}"
    return f"https://www.youtube.com/@{s}"


def _channel_id_via_ytdlp(page_url: str) -> Optional[str]:
    """Last resort when the HTML scrape is blocked or the page shape changed."""
    import yt_dlp

    opts = _yt_dlp_opts()
    opts.update({"extract_flat": True, "playlist_items": "1"})
    _throttle()
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(page_url, download=False)
    for key in ("channel_id", "uploader_id", "id"):
        value = str(info.get(key) or "")
        if _CHANNEL_ID_RE.fullmatch(value):
            return value
    return None


def resolve_channel_id(channel: str) -> str:
    """Resolve any channel reference (id, URL, @handle) to a UC... channel id."""
    s = (channel or "").strip()
    if not s:
        raise ValueError("Missing channel.")
    if _CHANNEL_ID_RE.fullmatch(s):
        return s
    m = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", s)
    if m:
        return m.group(1)

    now = time.monotonic()
    hit = _channel_cache.get(s)
    if hit and now - hit[0] < _CHANNEL_TTL:
        return hit[1]

    page_url = _channel_page_url(s)
    html = ""
    try:
        _throttle()
        resp = _http_client().get(page_url, timeout=_TIMEOUT, proxies=_requests_proxies())
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.info("Channel page fetch failed (%s): %s", page_url, safe_err(e))

    for pattern in (
        r'"channelId":"(UC[0-9A-Za-z_-]{22})"',
        r'<meta itemprop="(?:channelId|identifier)" content="(UC[0-9A-Za-z_-]{22})"',
        r'/channel/(UC[0-9A-Za-z_-]{22})',
    ):
        m = re.search(pattern, html)
        if m:
            _channel_cache[s] = (now, m.group(1))
            return m.group(1)

    try:
        cid = _channel_id_via_ytdlp(page_url)
        if cid:
            _channel_cache[s] = (now, cid)
            return cid
    except Exception as e:
        logger.info("yt-dlp channel resolution failed (%s): %s", page_url, safe_err(e))

    raise ValueError(
        f"Could not resolve a channel id from {channel!r}. Pass the channel URL, an "
        "@handle, or a UC... id. If the handle is correct, this host is probably "
        "blocked — run check-youtube-access."
    )


def fetch_channel_videos(channel: str, max_results: int = 10) -> List[dict]:
    """Return recent videos for a channel via its public RSS feed."""
    cid = resolve_channel_id(channel)
    _throttle()
    resp = _http_client().get(
        RSS_FEED.format(cid=cid), timeout=_TIMEOUT, proxies=_requests_proxies()
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    channel_title = root.findtext("atom:title", default="", namespaces=YT_NS)
    videos = []
    for entry in root.findall("atom:entry", YT_NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=YT_NS)
        videos.append({
            "video_id": vid,
            "title": entry.findtext("atom:title", default="", namespaces=YT_NS),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "published": entry.findtext("atom:published", default="", namespaces=YT_NS),
            "channel": channel_title,
        })
        if len(videos) >= max_results:
            break
    return videos


# ---------------------------------------------------------------------------
# Transcript formatting
# ---------------------------------------------------------------------------
def _snippet_field(seg, name, default=None):
    # 1.x yields snippet objects (seg.text); raw dicts use seg["text"].
    val = getattr(seg, name, None)
    if val is None and isinstance(seg, dict):
        val = seg.get(name, default)
    return default if val is None else val


def _to_dicts(snippets) -> List[dict]:
    """Normalise snippet objects or dicts into plain dicts (cacheable as JSON)."""
    out = []
    for seg in snippets:
        try:
            start = float(_snippet_field(seg, "start", 0.0))
        except (TypeError, ValueError):
            start = 0.0
        try:
            duration = float(_snippet_field(seg, "duration", 0.0))
        except (TypeError, ValueError):
            duration = 0.0
        out.append({"text": str(_snippet_field(seg, "text", "")), "start": start, "duration": duration})
    return out


def _fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def format_transcript(snippets, include_timestamps: bool = False,
                      max_chars: Optional[int] = None, offset: int = 0) -> str:
    """Render snippets to text, windowed to [offset, offset+max_chars).

    Without timestamps the text is grouped into ~30s paragraphs: it reads better
    than one unbroken line and gives truncation somewhere clean to land. When
    the text is cut, the footer tells the caller the exact offset to resume
    from, so an agent can page through instead of losing the tail.
    """
    if max_chars is None:
        max_chars = _DEFAULT_MAX_CHARS

    lines: List[str] = []
    paragraph: List[str] = []
    paragraph_start: Optional[float] = None

    for seg in snippets:
        text = str(_snippet_field(seg, "text", "")).replace("\n", " ").strip()
        if not text:
            continue
        try:
            start = float(_snippet_field(seg, "start", 0.0))
        except (TypeError, ValueError):
            start = 0.0
        if include_timestamps:
            lines.append(f"[{_fmt_ts(start)}] {text}")
            continue
        if paragraph_start is None:
            paragraph_start = start
        paragraph.append(text)
        if start - paragraph_start >= _PARAGRAPH_SECONDS:
            lines.append(" ".join(paragraph))
            paragraph, paragraph_start = [], None
    if paragraph:
        lines.append(" ".join(paragraph))

    full = ("\n" if include_timestamps else "\n\n").join(lines).strip()
    total = len(full)

    offset = max(0, min(int(offset or 0), total))
    out = full[offset:]

    if max_chars and len(out) > max_chars:
        chunk = out[:max_chars]
        brk = chunk.rfind("\n")
        if brk > max_chars * 0.6:  # only snap to a line break if we keep most of the chunk
            chunk = chunk[:brk]
        end = offset + len(chunk)
        return chunk.rstrip() + (
            f"\n\n[truncated: characters {offset}-{end} of {total}. "
            f"Call again with offset={end} for the next part, or max_chars=0 for the whole thing.]"
        )
    if offset:
        return out.rstrip() + f"\n\n[end of transcript: characters {offset}-{total} of {total}.]"
    return out


# ---------------------------------------------------------------------------
# Block detection
# ---------------------------------------------------------------------------
_BLOCK_MARKERS = (
    "ipblocked", "requestblocked", "too many requests", "http error 429",
    "http error 403", "sign in to confirm", "not a bot", "rate limit",
    "youtubedatauniversalprohibited", "blocked",
)


def _is_block(err: Any) -> bool:
    """Whether an error looks like YouTube refusing this IP rather than a bad video."""
    if err is None:
        return False
    blob = f"{type(err).__name__} {err}".lower() if isinstance(err, BaseException) else str(err).lower()
    return any(marker in blob for marker in _BLOCK_MARKERS)


# ---------------------------------------------------------------------------
# On-disk transcript cache
#
# Transcripts never change, so a hit costs YouTube nothing — and keeps serving
# results while the host is blocked, which on a VPS is most of the value.
# ---------------------------------------------------------------------------
def _cache_path(video_id: str, languages: Sequence[str]) -> Path:
    digest = hashlib.sha256(f"{video_id}|{','.join(languages)}".encode()).hexdigest()[:32]
    return _CACHE_DIR / f"{digest}.json"


def cache_load(video_id: str, languages: Sequence[str]) -> Optional[List[dict]]:
    if _CACHE_TTL <= 0:
        return None
    path = _cache_path(video_id, languages)
    try:
        if not path.is_file() or time.time() - path.stat().st_mtime > _CACHE_TTL:
            return None
        payload = json.loads(path.read_text("utf-8"))
        snippets = payload.get("snippets")
        return snippets if isinstance(snippets, list) and snippets else None
    except Exception as e:
        logger.debug("Cache read failed for %s: %s", video_id, safe_err(e))
        return None


def cache_store(video_id: str, languages: Sequence[str], snippets: List[dict]) -> None:
    if _CACHE_TTL <= 0 or not snippets:
        return
    path = _cache_path(video_id, languages)
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"video_id": video_id, "languages": list(languages),
                   "fetched_at": time.time(), "snippets": snippets}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        tmp.replace(path)  # atomic: a torn write never becomes a poisoned cache entry
    except Exception as e:
        logger.debug("Cache write failed for %s: %s", video_id, safe_err(e))


# ---------------------------------------------------------------------------
# Transcript sources
# ---------------------------------------------------------------------------
def _yta_snippets(video_id: str, languages: List[str]):
    """Primary path: youtube-transcript-api, preferred language then any."""
    _throttle()
    api = YouTubeTranscriptApi(proxy_config=_proxy_config(), http_client=_http_client())
    try:
        return list(api.fetch(video_id, languages=languages))
    except Exception as first_err:
        if _is_block(first_err):
            raise
        logger.info("Preferred-language fetch failed for %s: %s", video_id, safe_err(first_err))
    _throttle()
    for tr in api.list(video_id):
        try:
            return list(tr.fetch())
        except Exception:
            continue
    raise RuntimeError(f"No fetchable transcript track for {video_id}")


def _parse_json3(data: dict) -> List[dict]:
    snippets = []
    for ev in data.get("events", []):
        text = "".join(seg.get("utf8", "") for seg in ev.get("segs", []) if seg.get("utf8"))
        if text.strip():
            snippets.append({"text": text, "start": ev.get("tStartMs", 0) / 1000.0,
                             "duration": ev.get("dDurationMs", 0) / 1000.0})
    return snippets


def _pick_caption_lang(tracks: dict, languages: List[str]) -> str:
    """Choose a track: preferred code, then its base (pt-BR→pt), then any."""
    for code in languages:
        if code in tracks:
            return code
    for code in languages:
        base = code.split("-")[0]
        match = next((k for k in tracks if k.split("-")[0] == base), None)
        if match:
            return match
    return next(iter(tracks))


def _yt_dlp_opts(client: Optional[str] = None) -> dict:
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": _TIMEOUT,  # without this a hung call blocks the agent forever
        "retries": 1,
        "extractor_retries": 1,
    }
    proxy = current_proxy()
    if proxy:
        opts["proxy"] = proxy
    if _COOKIES:
        opts["cookiefile"] = _COOKIES
    extractor_args = {}
    if client:
        extractor_args["player_client"] = [client]
    if _PO_TOKEN:
        extractor_args["po_token"] = [_PO_TOKEN]
    if extractor_args:
        opts["extractor_args"] = {"youtube": extractor_args}
    return opts


_info_cache: Dict[str, tuple] = {}
_info_lock = threading.Lock()


def _ytdlp_info(video_id: str, client: Optional[str] = None) -> dict:
    """Fetch video metadata through yt-dlp, memoised per (video, client)."""
    import yt_dlp

    key = f"{video_id}|{client or ''}"
    now = time.monotonic()
    with _info_lock:
        hit = _info_cache.get(key)
        if hit and now - hit[0] < _INFO_TTL:
            return hit[1]

    _throttle()
    with yt_dlp.YoutubeDL(_yt_dlp_opts(client)) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)

    with _info_lock:
        _info_cache[key] = (now, info)
        if len(_info_cache) > 64:
            for stale in list(_info_cache)[:32]:
                _info_cache.pop(stale, None)
    return info


def _cached_info_any(video_id: str) -> Optional[dict]:
    """Any fresh cached metadata for this video, whichever client produced it.

    Chapters do not vary by player client, so strip_ads can reuse whatever the
    transcript fallback already downloaded instead of paying for a second
    extraction — which used to double this video's rate-limit exposure.
    """
    now = time.monotonic()
    with _info_lock:
        for key, (ts, info) in _info_cache.items():
            if key.startswith(f"{video_id}|") and now - ts < _INFO_TTL:
                return info
    return None


def _ytdlp_snippets(video_id: str, languages: List[str]) -> List[dict]:
    """Fallback path: pull one json3 caption track through yt-dlp's session.

    Each configured player client is tried in turn. Only one ``timedtext`` hit
    per attempt, for the best-matching language, to minimise rate-limit exposure.
    """
    import yt_dlp

    errors = []
    for client in (_PLAYER_CLIENTS or [None]):
        try:
            info = _ytdlp_info(video_id, client)
            # Manual subtitles win over auto-generated ones for the same language.
            tracks = {**(info.get("automatic_captions") or {}), **(info.get("subtitles") or {})}
            if not tracks:
                raise RuntimeError("no caption tracks returned")
            lang = _pick_caption_lang(tracks, languages)
            fmt = next((f for f in tracks[lang] if f.get("ext") == "json3"), None)
            if fmt is None:
                raise RuntimeError(f"no json3 caption format for {lang}")
            _throttle()
            with yt_dlp.YoutubeDL(_yt_dlp_opts(client)) as ydl:
                raw = ydl.urlopen(fmt["url"]).read().decode("utf-8", "replace")
            snippets = _parse_json3(json.loads(raw))
            if not snippets:
                raise RuntimeError("caption track was empty")
            logger.info("yt-dlp fallback succeeded for %s via player_client=%s", video_id, client)
            return snippets
        except Exception as e:
            errors.append(f"{client or 'default'}={safe_err(e)}")
            if _is_block(e):
                rotate_proxy()
    raise RuntimeError("; ".join(errors) or "no player client succeeded")


def _unavailable_message(video_id: str, yta_err: Any, ydl_err: Any) -> str:
    """Model-facing explanation — says whether retrying could possibly help."""
    lines = [
        f"No transcript available for {video_id}.",
        f"  youtube-transcript-api: {safe_err(yta_err)}",
        f"  yt-dlp ({', '.join(_PLAYER_CLIENTS) or 'default'}): {safe_err(ydl_err)}",
    ]
    if _is_block(yta_err) or _is_block(ydl_err):
        lines.append(
            "Cause: YouTube is blocking this host's IP address. This is an infrastructure "
            "problem, not a bad video id, and repeating this call will not help. Fixes, "
            "best first: set YT_PROXY to a residential proxy, set YT_WEBSHARE_USERNAME and "
            "YT_WEBSHARE_PASSWORD, add YT_COOKIES, or reorder YT_PLAYER_CLIENTS. "
            "Run the check-youtube-access tool to see what this host can reach."
        )
    else:
        lines.append("Cause: most likely subtitles are disabled for this video. Try another video.")
    return "\n".join(lines)


def get_snippets(video_id: str, languages: List[str]) -> List[dict]:
    """Cache, then youtube-transcript-api with retry-on-block, then yt-dlp."""
    cached = cache_load(video_id, languages)
    if cached is not None:
        logger.info("Cache hit for %s", video_id)
        return cached

    yta_err = None
    for attempt in range(1, _RETRIES + 1):
        try:
            snippets = _to_dicts(_yta_snippets(video_id, languages))
            cache_store(video_id, languages, snippets)
            return snippets
        except Exception as e:
            yta_err = e
            if _is_block(e) and attempt < _RETRIES:
                rotate_proxy()
                # Jitter so parallel callers do not retry in lockstep.
                wait = 2 ** attempt + random.uniform(0, 1)
                logger.warning("Blocked on %s (attempt %d/%d), retrying in %.1fs",
                               video_id, attempt, _RETRIES, wait)
                time.sleep(wait)
                continue
            break

    logger.info("Primary path exhausted for %s (%s); trying yt-dlp",
                video_id, safe_err(yta_err))
    try:
        snippets = _ytdlp_snippets(video_id, languages)
    except Exception as ydl_err:
        raise TranscriptUnavailable(_unavailable_message(video_id, yta_err, ydl_err)) from None
    cache_store(video_id, languages, snippets)
    return snippets


# ---------------------------------------------------------------------------
# Ad stripping
# ---------------------------------------------------------------------------
AD_CHAPTER_RE = re.compile(r"\b(sponsor\w*|advert\w*|promo\w*|ads?)\b", re.IGNORECASE)


def _fetch_ad_chapters(video_id: str) -> List[dict]:
    """Chapters whose title looks like a sponsor/ad segment (best-effort)."""
    info = _cached_info_any(video_id)
    if info is None:
        info = _ytdlp_info(video_id, _PLAYER_CLIENTS[0] if _PLAYER_CLIENTS else None)
    chapters = info.get("chapters") or []
    return [c for c in chapters if AD_CHAPTER_RE.search(c.get("title") or "")]


def _strip_ad_snippets(snippets, ad_chapters: List[dict]):
    if not ad_chapters:
        return snippets
    return [
        seg for seg in snippets
        if not any(
            c["start_time"] <= float(_snippet_field(seg, "start", 0.0)) < c["end_time"]
            for c in ad_chapters
        )
    ]


def fetch_transcript(video_id: str, languages: List[str],
                     include_timestamps: bool = False, max_chars: Optional[int] = None,
                     strip_ads: bool = False, offset: int = 0) -> str:
    snippets = get_snippets(video_id, languages)
    if strip_ads:
        try:
            snippets = _strip_ad_snippets(snippets, _fetch_ad_chapters(video_id))
        except Exception as e:
            logger.info("Could not fetch chapters for %s to strip ads: %s", video_id, safe_err(e))
    return format_transcript(snippets, include_timestamps, max_chars, offset)


# ---------------------------------------------------------------------------
# Diagnostics — the first thing to run on a VPS
# ---------------------------------------------------------------------------
def diagnose(probe_video_id: str = PROBE_VIDEO_ID) -> dict:
    """Probe what this host can actually reach, and say what to change."""
    report = {
        "config": {
            "proxies_configured": len(_PROXY_URLS),
            "webshare_configured": bool(_WEBSHARE_USER and _WEBSHARE_PASS),
            "cookies_configured": bool(_COOKIES),
            "cookies_readable": bool(_COOKIES) and Path(_COOKIES).is_file(),
            "player_clients": list(_PLAYER_CLIENTS),
            "po_token_configured": bool(_PO_TOKEN),
            "cache_dir": str(_CACHE_DIR),
            "cache_ttl_seconds": _CACHE_TTL,
            "min_interval_seconds": _MIN_INTERVAL,
            "retries": _RETRIES,
            "timeout_seconds": _TIMEOUT,
            "default_max_chars": _DEFAULT_MAX_CHARS,
        },
        "checks": [],
    }

    def record(name, ok, detail, blocked=False):
        report["checks"].append({"check": name, "ok": ok, "blocked": blocked, "detail": detail})
        return ok

    try:
        _throttle()
        resp = _http_client().get("https://www.youtube.com/", timeout=_TIMEOUT,
                                  proxies=_requests_proxies())
        record("https_reachable", resp.status_code < 400, f"HTTP {resp.status_code}",
               blocked=resp.status_code in (403, 429))
    except Exception as e:
        record("https_reachable", False, safe_err(e), blocked=_is_block(e))

    transcript_ok = False
    try:
        snippets = _yta_snippets(probe_video_id, DEFAULT_LANGUAGES)
        transcript_ok = record("youtube_transcript_api", True, f"{len(snippets)} snippets")
    except Exception as e:
        record("youtube_transcript_api", False, safe_err(e), blocked=_is_block(e))

    working_clients = []
    for client in (_PLAYER_CLIENTS or [None]):
        try:
            info = _ytdlp_info(probe_video_id, client)
            tracks = {**(info.get("automatic_captions") or {}), **(info.get("subtitles") or {})}
            ok = bool(tracks)
            if ok:
                working_clients.append(client)
            record(f"yt_dlp:{client or 'default'}", ok,
                   f"{len(tracks)} caption languages" if ok else "no caption tracks")
        except Exception as e:
            record(f"yt_dlp:{client or 'default'}", False, safe_err(e), blocked=_is_block(e))

    any_ok = transcript_ok or bool(working_clients)
    blocked = any(c["blocked"] for c in report["checks"])
    report["working_player_clients"] = working_clients
    report["verdict"] = "ok" if any_ok else ("blocked" if blocked else "failing")

    if any_ok and working_clients and working_clients[0] != (_PLAYER_CLIENTS or [None])[0]:
        report["recommendation"] = (
            "Working, but the first player client in YT_PLAYER_CLIENTS is not the one "
            f"that answers. Put {working_clients[0]!r} first to avoid wasted attempts: "
            f"YT_PLAYER_CLIENTS={','.join(working_clients)}"
        )
    elif any_ok:
        report["recommendation"] = "This host can reach YouTube. No changes needed."
    elif blocked:
        report["recommendation"] = (
            "This host's IP is blocked by YouTube — normal for a VPS, and no amount of "
            "retrying fixes it. In order of effectiveness: (1) set YT_PROXY to a "
            "residential/ISP proxy, or a SOCKS5 tunnel to a home connection; "
            "(2) set YT_WEBSHARE_USERNAME and YT_WEBSHARE_PASSWORD for a rotating "
            "residential pool; (3) add YT_COOKIES from a logged-in browser (helps, but "
            "rarely clears a flagged datacenter IP on its own); (4) try other clients in "
            "YT_PLAYER_CLIENTS. Raising YT_MIN_INTERVAL reduces how often you get blocked "
            "in the first place."
        )
    else:
        report["recommendation"] = (
            "No path reached YouTube and nothing looks like an IP block — check outbound "
            "network access, DNS, and the proxy URL if one is configured."
        )
    return report


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
server = Server(name="youtube-mcp-server")

# Every tool here only reads public data from a third-party service. Saying so
# lets a client auto-approve the calls instead of prompting on each one.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=True)

_LANGUAGES_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Preferred language codes, best first (e.g. ['pt', 'en'])",
}


@server.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="get-channel-videos",
            description=(
                "List a channel's most recent videos. Accepts a channel URL, @handle, or "
                f"channel id (UC...). Returns at most {RSS_MAX_ENTRIES} videos — that is "
                "YouTube's own feed limit."
            ),
            annotations=READ_ONLY,
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel URL, @handle, or UC... id"},
                    "max_results": {
                        "type": "integer", "description": "How many recent videos (1-15)",
                        "default": 10, "minimum": 1, "maximum": RSS_MAX_ENTRIES,
                    },
                },
                "required": ["channel"],
            },
        ),
        Tool(
            name="get-transcript",
            description=(
                "Get the transcript of a single YouTube video (URL or 11-character video id). "
                f"Output is capped at {_DEFAULT_MAX_CHARS} characters by default; if the result "
                "says it was truncated, call again with the offset it gives you to read on."
            ),
            annotations=READ_ONLY,
            inputSchema={
                "type": "object",
                "properties": {
                    "video_url": {"type": "string", "description": "Video URL or 11-char video id"},
                    "languages": _LANGUAGES_SCHEMA,
                    "include_timestamps": {
                        "type": "boolean", "description": "Prefix each line with [H:MM:SS]",
                        "default": False,
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Cap output length; 0 = no cap (can be very large)",
                        "default": _DEFAULT_MAX_CHARS, "minimum": 0,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Start at this character — use the offset a truncated result reports",
                        "default": 0, "minimum": 0,
                    },
                    "strip_ads": {
                        "type": "boolean",
                        "description": "Drop lines inside sponsor/ad chapter markers (best-effort, needs chapters)",
                        "default": False,
                    },
                },
                "required": ["video_url"],
            },
        ),
        Tool(
            name="get-channel-transcripts",
            description=(
                "Get transcripts for a channel's latest videos in one call. Your go-to tool for "
                "'transcribe the last N videos of this channel'. Each transcript is capped "
                "separately — keep max_results small to avoid an enormous result."
            ),
            annotations=READ_ONLY,
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel URL, @handle, or UC... id"},
                    "max_results": {
                        "type": "integer", "description": "How many recent videos to transcribe (1-15)",
                        "default": 5, "minimum": 1, "maximum": RSS_MAX_ENTRIES,
                    },
                    "languages": _LANGUAGES_SCHEMA,
                    "include_timestamps": {
                        "type": "boolean", "description": "Prefix each line with [H:MM:SS]",
                        "default": False,
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Cap each transcript; 0 = no cap (can be enormous across several videos)",
                        "default": max(1, _DEFAULT_MAX_CHARS // 2), "minimum": 0,
                    },
                    "strip_ads": {
                        "type": "boolean",
                        "description": "Drop lines inside sponsor/ad chapter markers (best-effort, needs chapters)",
                        "default": False,
                    },
                },
                "required": ["channel"],
            },
        ),
        Tool(
            name="check-youtube-access",
            description=(
                "Diagnose whether this host can reach YouTube, and report what to change if not. "
                "Run this when transcripts fail repeatedly — it distinguishes an IP block "
                "(common on a VPS, not fixable by retrying) from a video that simply has no subtitles."
            ),
            annotations=READ_ONLY,
            inputSchema={
                "type": "object",
                "properties": {
                    "video_url": {
                        "type": "string",
                        "description": "Optional video to probe with; defaults to a known-good one",
                    },
                },
            },
        ),
    ]


# Newer SDKs validate arguments against inputSchema before the handler runs,
# which would reject the sloppy-but-recoverable arguments (a null for an omitted
# optional, a string for an array) that the coercion helpers above exist to
# absorb. Turn that off where it is supported and do the checking ourselves.
try:
    _call_tool = server.call_tool(validate_input=False)
except TypeError:  # SDK predates the flag; its own validation applies
    _call_tool = server.call_tool()


@_call_tool
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
    arguments = arguments or {}
    logger.info("Tool call: %s %s", name, redact(json.dumps(arguments, ensure_ascii=False, default=str))[:500])

    try:
        languages = as_languages(arguments)
        include_timestamps = as_bool(arguments, "include_timestamps", False)
        strip_ads = as_bool(arguments, "strip_ads", False)

        if name == "get-channel-videos":
            videos = await asyncio.to_thread(
                fetch_channel_videos,
                as_text(arguments, "channel"),
                as_int(arguments, "max_results", 10, lo=1, hi=RSS_MAX_ENTRIES),
            )
            return [TextContent(type="text", text=json.dumps(videos, ensure_ascii=False))]

        if name == "get-transcript":
            raw = as_text(arguments, "video_url")
            video_id = extract_video_id(raw)
            if not video_id:
                raise ValueError(
                    f"{raw!r} is not a YouTube video URL or an 11-character video id. "
                    "Playlist and channel URLs are not videos — use get-channel-videos for a channel."
                )
            text = await asyncio.to_thread(
                fetch_transcript, video_id, languages, include_timestamps,
                as_int(arguments, "max_chars", _DEFAULT_MAX_CHARS, lo=0),
                strip_ads,
                as_int(arguments, "offset", 0, lo=0),
            )
            return [TextContent(type="text", text=text or "This video has no transcript text.")]

        if name == "get-channel-transcripts":
            max_chars = as_int(arguments, "max_chars", max(1, _DEFAULT_MAX_CHARS // 2), lo=0)
            videos = await asyncio.to_thread(
                fetch_channel_videos,
                as_text(arguments, "channel"),
                as_int(arguments, "max_results", 5, lo=1, hi=RSS_MAX_ENTRIES),
            )
            results = []
            for video in videos:
                try:
                    text = await asyncio.to_thread(
                        fetch_transcript, video["video_id"], languages,
                        include_timestamps, max_chars, strip_ads, 0,
                    )
                except Exception as e:
                    # One unavailable video must not fail the whole batch.
                    results.append({**video, "transcript": None, "error": safe_err(e)})
                    continue
                results.append({**video, "transcript": text})
            return [TextContent(type="text", text=json.dumps(results, ensure_ascii=False))]

        if name == "check-youtube-access":
            probe = extract_video_id(str(arguments.get("video_url") or "")) or PROBE_VIDEO_ID
            report = await asyncio.to_thread(diagnose, probe)
            return [TextContent(type="text", text=json.dumps(report, ensure_ascii=False, indent=2))]

        raise ValueError(
            f"Unknown tool {name!r}. Available: get-channel-videos, get-transcript, "
            "get-channel-transcripts, check-youtube-access."
        )

    except (TranscriptUnavailable, ValueError) as e:
        # Message is already written for the model; raising marks isError on the result.
        logger.info("Tool %s: %s", name, safe_err(e))
        raise RuntimeError(redact(str(e))) from None
    except Exception as e:
        logger.error("Tool %s failed: %s", name, safe_err(e))
        logger.debug("Traceback for %s", name, exc_info=True)
        raise RuntimeError(f"{name} failed. {safe_err(e)}") from None


async def main():
    logger.info(
        "Starting YouTube MCP Server (proxies=%d, webshare=%s, cookies=%s, clients=%s, cache=%s)",
        len(_PROXY_URLS), bool(_WEBSHARE_USER and _WEBSHARE_PASS), bool(_COOKIES),
        ",".join(_PLAYER_CLIENTS) or "default", "on" if _CACHE_TTL > 0 else "off",
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    if "--check" in sys.argv:
        # Diagnostics go to stdout only in this mode — the MCP server is not running.
        report = diagnose()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        sys.exit(0 if report["verdict"] == "ok" else 1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Server shutting down...", file=sys.stderr)
