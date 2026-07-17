"""YouTube MCP Server.

Tools for reading a channel's latest videos and fetching video transcripts.
No API key required — channel listing uses YouTube's public RSS feed and
transcripts use youtube-transcript-api directly.
"""

import os
import re
import sys
import json
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

    resp = requests.get(page_url, headers=HTTP_HEADERS, timeout=20)
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
    resp = requests.get(RSS_FEED.format(cid=cid), headers=HTTP_HEADERS, timeout=20)
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


def _snippet_text(seg) -> str:
    # 1.x yields snippet objects (seg.text); older/raw dicts use seg["text"].
    text = getattr(seg, "text", None)
    if text is None and isinstance(seg, dict):
        text = seg.get("text", "")
    return (text or "").replace("\n", " ")


def _to_text(entries) -> str:
    return " ".join(_snippet_text(s) for s in entries).strip()


def fetch_transcript(video_id: str, languages: List[str]) -> str:
    """Fetch a transcript (youtube-transcript-api 1.x), falling back to any language."""
    api = YouTubeTranscriptApi()

    # Preferred languages first.
    try:
        return _to_text(api.fetch(video_id, languages=languages))
    except Exception as first_err:  # NoTranscriptFound / language mismatch, etc.
        logger.info("Preferred-language fetch failed for %s: %s", video_id, first_err)

    # Fall back to whatever transcript exists (manual or auto-generated).
    try:
        listing = api.list(video_id)
    except Exception as e:
        raise RuntimeError(f"No transcript available for {video_id}: {e}")

    for tr in listing:
        try:
            return _to_text(tr.fetch())
        except Exception:
            continue
    raise RuntimeError(f"No fetchable transcript for {video_id}")


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
                },
                "required": ["channel"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
    logger.info("Tool call: %s %s", name, arguments)
    languages = arguments.get("languages") or DEFAULT_LANGUAGES

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
            text = await asyncio.to_thread(fetch_transcript, vid, languages)
            return [TextContent(type="text", text=text or "No transcript found for this video.")]

        if name == "get-channel-transcripts":
            max_results = int(arguments.get("max_results", 5))
            videos = await asyncio.to_thread(fetch_channel_videos, arguments["channel"], max_results)
            out = []
            for v in videos:
                try:
                    text = await asyncio.to_thread(fetch_transcript, v["video_id"], languages)
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
