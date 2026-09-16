"""End-to-end MCP checks: spawn the server over stdio and talk to it.

Only offline paths are exercised — no test here reaches YouTube.
"""

import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _params():
    env = {**os.environ, "YT_CACHE_TTL": "0", "YT_PROXY": "", "YT_LOG_LEVEL": "CRITICAL"}
    return StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], env=env)


async def test_tools_are_listed_and_annotated():
    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}

    assert set(tools) == {
        "get-channel-videos", "get-transcript",
        "get-channel-transcripts", "check-youtube-access",
    }
    for tool in tools.values():
        assert tool.annotations is not None, f"{tool.name} has no annotations"
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.openWorldHint is True
    # The cap has to be advertised, not just applied.
    assert tools["get-transcript"].inputSchema["properties"]["max_chars"]["default"] > 0


async def test_bad_video_url_is_a_flagged_error_not_a_silent_success():
    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get-transcript", {"video_url": "https://example.com/some-article-about-things"}
            )

    assert result.isError is True
    text = result.content[0].text
    assert "not a YouTube video URL" in text


async def test_missing_required_argument_is_a_flagged_error():
    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get-channel-videos", {})

    assert result.isError is True
    assert "channel" in result.content[0].text


async def test_unknown_tool_is_a_flagged_error():
    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get-subtitles", {"video_url": "x"})

    assert result.isError is True
    assert "Unknown tool" in result.content[0].text


async def test_null_arguments_do_not_break_the_call():
    """A model sending null for every optional must not produce a validation error."""
    async with stdio_client(_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get-transcript", {
                "video_url": "not a video",
                "languages": None,
                "max_chars": None,
                "offset": None,
                "include_timestamps": None,
                "strip_ads": None,
            })

    # It still fails — but on the video id, having accepted the nulls.
    assert result.isError is True
    assert "not a YouTube video URL" in result.content[0].text
    assert "validation error" not in result.content[0].text.lower()
