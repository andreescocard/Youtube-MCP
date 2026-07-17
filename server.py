"""YouTube MCP Server.

Tools for reading a channel's latest videos and fetching video transcripts.
No API key required — channel listing uses YouTube's public RSS feed and
transcripts use youtube-transcript-api directly.
"""

import os
import re
import sys
import json
import time
import asyncio
import logging
from typing import List, Optional
from urllib.parse import urlparse, parse_qs

import requests
from defusedxml import ElementTree as ET
from mcp.server import Server
from mcp.types import Tool, TextContent
from mcp.server.stdio import stdio_server

from youtube_transcript_api import YouTubeTranscriptApi

# ---------------------------------------------------------------------------
# Logging (stderr only — stdout is reserved for the MCP protocol)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
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

# Optional proxy — set YT_PROXY (or HTTPS_PROXY / HTTP_PROXY). Needed on cloud
# hosts, whose datacenter IPs YouTube frequently blocks.
_PROXY_URL = os.getenv("YT_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
# Optional Netscape cookie file — authenticated requests are blocked far less.
_COOKIES = os.getenv("YT_COOKIES")
# Transient IpBlocked errors recover — retry a few times with backoff.
_RETRIES = int(os.getenv("YT_RETRIES", "3"))


def _requests_proxies():
    return {"http": _PROXY_URL, "https": _PROXY_URL} if _PROXY_URL else None


def _proxy_config():
    if not _PROXY_URL:
        return None
    try:
        from youtube_transcript_api.proxies import GenericProxyConfig
        return GenericProxyConfig(http_url=_PROXY_URL, https_url=_PROXY_URL)
    except Exception as e:
        logger.warning("Proxy config unavailable: %s", e)
        return None


RSS_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
YT_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def extract_video_id(url_or_id: str) -> Optional[str]:
    """Return the 11-char video id from a URL or a bare id."""
    s = url_or_id.strip()
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", s):
        return s
    try:
        parsed = urlparse(s)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        vid = parsed.path.lstrip("/").split("/")[0]
        return vid if re.fullmatch(r"[0-9A-Za-z_-]{11}", vid) else None
    if "youtube" in host:
        if parsed.path.startswith(("/watch",)):
            q = parse_qs(parsed.query).get("v", [None])[0]
            if q and re.fullmatch(r"[0-9A-Za-z_-]{11}", q):
                return q
        m = re.search(r"/(?:embed|shorts|live|v)/([0-9A-Za-z_-]{11})", parsed.path)
        if m:
            return m.group(1)
    m = re.search(r"([0-9A-Za-z_-]{11})", s)
    return m.group(1) if m else None


def resolve_channel_id(channel: str) -> str:
    """Resolve any channel reference (id, URL, @handle) to a UC... channel id."""
    s = channel.strip()

    # Already a channel id.
    if re.fullmatch(r"UC[0-9A-Za-z_-]{22}", s):
        return s

    m = re.search(r"/channel/(UC[0-9A-Za-z_-]{22})", s)
    if m:
        return m.group(1)

    # Build a canonical URL to scrape the channelId from.
    if s.startswith("http"):
        page_url = s
    elif s.startswith("@"):
        page_url = f"https://www.youtube.com/{s}"
    else:
        page_url = f"https://www.youtube.com/@{s}"

    resp = requests.get(page_url, headers=HTTP_HEADERS, timeout=20, proxies=_requests_proxies())
    resp.raise_for_status()
    html = resp.text
    for pattern in (
        r'"channelId":"(UC[0-9A-Za-z_-]{22})"',
        r'<meta itemprop="(?:channelId|identifier)" content="(UC[0-9A-Za-z_-]{22})"',
        r'/channel/(UC[0-9A-Za-z_-]{22})',
    ):
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    raise ValueError(f"Could not resolve channel id from: {channel}")


def fetch_channel_videos(channel: str, max_results: int = 10) -> List[dict]:
    """Return recent videos for a channel via its public RSS feed."""
    cid = resolve_channel_id(channel)
    resp = requests.get(RSS_FEED.format(cid=cid), headers=HTTP_HEADERS, timeout=20, proxies=_requests_proxies())
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


def _snippet_field(seg, name, default=None):
    # 1.x yields snippet objects (seg.text); raw dicts use seg["text"].
    val = getattr(seg, name, None)
    if val is None and isinstance(seg, dict):
        val = seg.get(name, default)
    return default if val is None else val


def _fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def format_transcript(snippets, include_timestamps: bool, max_chars: int) -> str:
    """Render snippets to text, optionally timestamped, capped at max_chars (0 = no cap)."""
    parts = []
    for seg in snippets:
        text = str(_snippet_field(seg, "text", "")).replace("\n", " ").strip()
        if not text:
            continue
        if include_timestamps:
            parts.append(f"[{_fmt_ts(float(_snippet_field(seg, 'start', 0.0)))}] {text}")
        else:
            parts.append(text)
    out = ("\n" if include_timestamps else " ").join(parts).strip()
    if max_chars and len(out) > max_chars:
        out = out[:max_chars].rstrip() + f"\n\n[...truncated at {max_chars} chars; full length {len(out)}]"
    return out


def _is_block(err: Exception) -> bool:
    name = type(err).__name__.lower()
    return "block" in name or "block" in str(err).lower() or "too many requests" in str(err).lower()


def _yta_snippets(video_id: str, languages: List[str]):
    """Primary path: youtube-transcript-api, preferred language then any."""
    api = YouTubeTranscriptApi(proxy_config=_proxy_config())
    try:
        return list(api.fetch(video_id, languages=languages))
    except Exception as first_err:
        if _is_block(first_err):
            raise
        logger.info("Preferred-language fetch failed for %s: %s", video_id, first_err)
    listing = api.list(video_id)
    for tr in listing:
        try:
            return list(tr.fetch())
        except Exception:
            continue
    raise RuntimeError(f"No fetchable transcript for {video_id}")


def _parse_json3(data: dict):
    snippets = []
    for ev in data.get("events", []):
        text = "".join(seg.get("utf8", "") for seg in ev.get("segs", []) if seg.get("utf8"))
        if text.strip():
            snippets.append({"text": text, "start": ev.get("tStartMs", 0) / 1000.0,
                             "duration": ev.get("dDurationMs", 0) / 1000.0})
    return snippets


def _pick_caption_lang(tracks: dict, languages: List[str]) -> str:
    """Choose a track: preferred code, then its base (pt-BR→pt), then any."""
    for l in languages:
        if l in tracks:
            return l
    for l in languages:
        base = l.split("-")[0]
        match = next((k for k in tracks if k.split("-")[0] == base), None)
        if match:
            return match
    return next(iter(tracks))


def _ytdlp_snippets(video_id: str, languages: List[str]):
    """Fallback path: single metadata call, then fetch one json3 track via yt-dlp's session.

    Uses yt-dlp networking (correct headers/consent) and hits ``timedtext`` only
    once for the best-matching *original* language — minimises rate-limit exposure.
    """
    import yt_dlp

    opts = {"skip_download": True, "quiet": True, "no_warnings": True}
    if _PROXY_URL:
        opts["proxy"] = _PROXY_URL
    if _COOKIES:
        opts["cookiefile"] = _COOKIES

    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        # Prefer manual subtitles over auto-generated for the same language.
        tracks = {**(info.get("automatic_captions") or {}), **(info.get("subtitles") or {})}
        if not tracks:
            raise RuntimeError(f"yt-dlp found no captions for {video_id}")

        lang = _pick_caption_lang(tracks, languages)
        fmt = next((f for f in tracks[lang] if f.get("ext") == "json3"), None)
        if fmt is None:
            raise RuntimeError(f"No json3 caption format for {video_id} ({lang})")

        raw = ydl.urlopen(fmt["url"]).read().decode("utf-8", "replace")

    data = json.loads(raw)
    snippets = _parse_json3(data)
    if not snippets:
        raise RuntimeError(f"yt-dlp returned empty captions for {video_id}")
    return snippets


def get_snippets(video_id: str, languages: List[str]):
    """Fetch snippets with retry-on-block, then yt-dlp fallback."""
    last = None
    for attempt in range(1, _RETRIES + 1):
        try:
            return _yta_snippets(video_id, languages)
        except Exception as e:
            last = e
            if _is_block(e) and attempt < _RETRIES:
                wait = 2 ** attempt
                logger.warning("Blocked on %s (attempt %d), retrying in %ds", video_id, attempt, wait)
                time.sleep(wait)
                continue
            break

    logger.info("yta path exhausted for %s (%s); trying yt-dlp fallback", video_id, last)
    try:
        return _ytdlp_snippets(video_id, languages)
    except Exception as e:
        raise RuntimeError(f"No transcript for {video_id}: yta={last}; yt-dlp={e}")


def fetch_transcript(video_id: str, languages: List[str],
                     include_timestamps: bool = False, max_chars: int = 0) -> str:
    return format_transcript(get_snippets(video_id, languages), include_timestamps, max_chars)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
server = Server(name="youtube-mcp-server")


@server.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="get-channel-videos",
            description="List a channel's most recent videos. Accepts a channel URL, @handle, or channel id (UC...).",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel URL, @handle, or UC... id"},
                    "max_results": {"type": "integer", "description": "How many recent videos", "default": 10},
                },
                "required": ["channel"],
            },
        ),
        Tool(
            name="get-transcript",
            description="Get the transcript of a single YouTube video (URL or video id).",
            inputSchema={
                "type": "object",
                "properties": {
                    "video_url": {"type": "string", "description": "Video URL or 11-char video id"},
                    "languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Preferred language codes, best first",
                    },
                    "include_timestamps": {"type": "boolean", "description": "Prefix each line with [H:MM:SS]", "default": False},
                    "max_chars": {"type": "integer", "description": "Cap output length; 0 = no cap", "default": 0},
                },
                "required": ["video_url"],
            },
        ),
        Tool(
            name="get-channel-transcripts",
            description="Get transcripts for a channel's latest videos in one call. Your go-to tool for 'transcribe the last N videos of this channel'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Channel URL, @handle, or UC... id"},
                    "max_results": {"type": "integer", "description": "How many recent videos to transcribe", "default": 5},
                    "languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Preferred language codes, best first",
                    },
                    "include_timestamps": {"type": "boolean", "description": "Prefix each line with [H:MM:SS]", "default": False},
                    "max_chars": {"type": "integer", "description": "Cap each transcript length; 0 = no cap", "default": 0},
                },
                "required": ["channel"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
    logger.info("Tool call: %s %s", name, arguments)
    languages = arguments.get("languages") or DEFAULT_LANGUAGES
    ts = bool(arguments.get("include_timestamps", False))
    max_chars = int(arguments.get("max_chars", 0))

    try:
        if name == "get-channel-videos":
            videos = await asyncio.to_thread(
                fetch_channel_videos, arguments["channel"], int(arguments.get("max_results", 10))
            )
            return [TextContent(type="text", text=json.dumps(videos, ensure_ascii=False, indent=2))]

        if name == "get-transcript":
            vid = extract_video_id(arguments["video_url"])
            if not vid:
                return [TextContent(type="text", text=f"Could not parse a video id from: {arguments['video_url']}")]
            text = await asyncio.to_thread(fetch_transcript, vid, languages, ts, max_chars)
            return [TextContent(type="text", text=text or "No transcript found for this video.")]

        if name == "get-channel-transcripts":
            max_results = int(arguments.get("max_results", 5))
            videos = await asyncio.to_thread(fetch_channel_videos, arguments["channel"], max_results)
            out = []
            for v in videos:
                try:
                    text = await asyncio.to_thread(fetch_transcript, v["video_id"], languages, ts, max_chars)
                except Exception as e:
                    text = None
                    v["error"] = str(e)
                out.append({**v, "transcript": text})
            return [TextContent(type="text", text=json.dumps(out, ensure_ascii=False, indent=2))]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        logger.error("Error in %s: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=f"Error: {e}")]


async def main():
    logger.info("Starting YouTube MCP Server...")
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Server shutting down...", file=sys.stderr)
