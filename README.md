# YouTube MCP Server

A Model Context Protocol (MCP) server for reading a YouTube channel's latest videos and fetching their transcripts. **No API key required** — channel listing uses YouTube's public RSS feed and transcripts use `youtube-transcript-api`, with a `yt-dlp` fallback.

Built to keep working on a VPS, where YouTube blocks datacenter IPs aggressively. See [Running on a VPS](#running-on-a-vps).

## Support Us

If you find this project helpful and would like to support future projects, consider buying us a coffee! Your support helps us continue building innovative AI solutions.

<a href="https://www.buymeacoffee.com/blazzmocompany"><img src="https://img.buymeacoffee.com/button-api/?text=Buy me a coffee&emoji=&slug=blazzmocompany&button_colour=40DCA5&font_colour=ffffff&font_family=Cookie&outline_colour=000000&coffee_colour=FFDD00"></a>

Your contributions go a long way in fueling our passion for creating intelligent and user-friendly applications.

## Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Running the Server](#running-the-server)
- [Available Tools](#available-tools)
- [Running on a VPS](#running-on-a-vps)
  - [Diagnose first](#diagnose-first)
  - [The layers, in order of effort](#the-layers-in-order-of-effort)
  - [Recommended VPS configuration](#recommended-vps-configuration)
- [Configuration reference](#configuration-reference)
- [Using with MCP Clients](#using-with-mcp-clients)
- [Development](#development)
- [Debugging](#debugging)
- [Contributing](#contributing)
- [License](#license)

## Features

- List a channel's most recent videos from a URL, `@handle`, or channel id — no API key
- Retrieve a single video's transcript, with paging for long ones
- Transcribe a channel's last N videos in one call
- Built-in diagnostics for when YouTube blocks the host
- Layered anti-block handling: on-disk cache, request throttling, player-client
  rotation, cookies, and a rotating proxy pool

## Prerequisites

- Python 3.10+
- uv package manager

## Installation

1. Clone this repository

2. Create and activate a virtual environment using uv:
```bash
uv venv
# On Windows:
.venv\Scripts\activate
# On Unix/MacOS:
source .venv/bin/activate
```

3. Install dependencies using uv:
```bash
uv sync
```

## Running the Server

### 1. Direct Method

```bash
uv run python server.py
```

### 2. Configure for Claude.app

Without a package manager (this works for Windows):
```json
"mcpServers": {
  "youtube": {
    "command": "C:\\Path\\To\\Your\\Project\\.venv\\Scripts\\python.exe",
    "args": ["C:\\Path\\To\\Your\\Project\\server.py"]
  }
}
```

Using the uv package manager:

```json
"mcpServers": {
  "youtube": {
    "command": "uv",
    "args": ["--directory", "C:\\Path\\To\\Your\\Project", "run", "server.py"],
    "env": {
      "YT_PROXY": "http://user:pass@host:port"
    }
  }
}
```

## Available Tools

1. `get-channel-videos`: List a channel's most recent videos
   - `channel`: Channel URL, `@handle`, or channel id (`UC...`)
   - `max_results`: How many recent videos, 1-15 (default: 10). YouTube's feed
     never returns more than 15, whatever you ask for.

2. `get-transcript`: Get the transcript of a single video
   - `video_url`: Video URL or 11-char video id
   - `languages`: Optional preferred language codes, best first
   - `include_timestamps`: Prefix each line with `[H:MM:SS]` (default: false)
   - `max_chars`: Cap output length (default: `YT_MAX_CHARS`, 8000; `0` = no cap)
   - `offset`: Start at this character — a truncated result tells you the exact
     offset to resume from, so an agent can page through a long video instead of
     losing the tail
   - `strip_ads`: Drop lines inside sponsor/ad chapter markers (default: false,
     best-effort — needs the video to have chapters)

3. `get-channel-transcripts`: Transcribe a channel's latest videos in one call
   - `channel`, `max_results` (1-15, default 5), `languages`,
     `include_timestamps`, `strip_ads` — as above
   - `max_chars`: Cap **each** transcript (default: half of `YT_MAX_CHARS`).
     Keep `max_results` small; several uncapped transcripts add up fast.

4. `check-youtube-access`: Diagnose whether this host can reach YouTube
   - `video_url`: Optional video to probe with; defaults to a known-good one
   - Reports which paths work, and what to change if none do. Run this when
     transcripts keep failing — it separates an IP block (not fixable by
     retrying) from a video that simply has no subtitles.

All four tools are annotated `readOnlyHint` / `openWorldHint`, so MCP clients can
auto-approve them rather than prompting on every call.

### Notes for agent use

- **Output is capped by default.** A three-hour video is 150k+ characters, which
  will blow up most context windows. When a result says it was truncated, call
  `get-transcript` again with the `offset` it reports.
- **Failures are real failures.** Tool errors come back with `isError` set and a
  message that says whether retrying could possibly help, rather than a
  successful-looking string starting with "Error:".
- **Sloppy arguments are tolerated.** `null` for an omitted optional, `"true"`
  for a boolean, `"pt,en"` for the `languages` array — all coerced rather than
  rejected, which is a common failure mode for smaller function-calling models.

## Running on a VPS

YouTube rate-limits and blocks per IP, and datacenter ranges get hit hardest.
A VPS will usually be blocked on the default `web` client from day one. There is
no single switch that fixes this; what works is stacking cheap layers so you hit
YouTube as rarely as possible and from the least-suspicious angle available.

### Diagnose first

Before changing anything, find out what this host can actually reach:

```bash
uv run python server.py --check
```

Or, from inside the agent, call the `check-youtube-access` tool. Either way you
get a per-path report and a concrete recommendation. Crucially it distinguishes:

- **`"verdict": "blocked"`** — YouTube is refusing this IP. Retrying will not
  help; you need one of the layers below.
- **`"verdict": "failing"`** — nothing reached YouTube at all. Check outbound
  network, DNS, and your proxy URL.
- **`"verdict": "ok"`** — the host works. If the report names
  `working_player_clients`, put the first one at the front of
  `YT_PLAYER_CLIENTS` so no attempts are wasted.

### The layers, in order of effort

**1. On-disk cache (on by default, free).** Transcripts never change, so a cache
hit costs YouTube nothing — and keeps serving results *while the host is
blocked*. On a VPS this is most of the practical value. It lives in
`YT_CACHE_DIR` (default `~/.cache/youtube-mcp`) for `YT_CACHE_TTL` seconds
(default 30 days). Point it at a persistent volume, not a container's scratch
disk.

**2. Throttling (free).** `YT_MIN_INTERVAL` sets a floor, in seconds, between
outbound YouTube requests. Not getting blocked beats recovering from a block;
`YT_MIN_INTERVAL=2` costs you almost nothing and meaningfully lowers how often
you trip the limiter.

**3. Player-client rotation (free, often enough on its own).** YouTube's bot
check is aimed squarely at the `web` client. The TV, embedded and mobile clients
are policed far more loosely and frequently still answer from an IP that `web`
is blocked on. The server tries them in order:

```
YT_PLAYER_CLIENTS=tv,android_vr,web_safari,mweb,web
```

Which clients work shifts over time as YouTube tightens things, which is exactly
why this is a list and why `--check` reports the ones that answered. Reorder it
when the report tells you to.

**4. Cookies (free, partial).** Export a logged-in session to a Netscape-format
`cookies.txt` (any browser extension does this) and set `YT_COOKIES`. An
authenticated session is blocked noticeably less. Be aware this is a real
account credential — use a throwaway account, and note that YouTube's bot check
is largely IP-reputation based, so cookies alone rarely clear a flagged
datacenter IP.

**5. A residential exit (the reliable fix).** Everything above reduces how often
you get blocked; only this changes the IP YouTube sees.

- **Free, if you have a machine at home.** Open a SOCKS5 tunnel from the VPS to
  it and send YouTube traffic through your home ISP connection:

  ```bash
  ssh -f -N -D 1080 you@your-home-box
  export YT_PROXY=socks5h://127.0.0.1:1080
  ```

  (SOCKS support is installed with the dependencies. Use `socks5h://` so DNS
  resolves on the home side too. Pair it with an autossh unit so the tunnel
  survives reboots.)

- **Paid, zero setup.** A rotating residential pool. Webshare is what
  `youtube-transcript-api` integrates with directly:

  ```bash
  export YT_WEBSHARE_USERNAME=...
  export YT_WEBSHARE_PASSWORD=...
  ```

- **Any other proxy.** `YT_PROXY` accepts a comma-separated list, and the server
  rotates to the next entry every time it hits a block — so a handful of cheap
  proxies degrades gracefully instead of dying on the first ban:

  ```bash
  export YT_PROXY=http://u:p@proxy-a:8080,http://u:p@proxy-b:8080
  ```

Proxy credentials are redacted from every log line and every message returned to
the model, so a `YT_PROXY` password never leaks into an agent's context.

**6. PO tokens (last resort).** If YouTube demands a proof-of-origin token for
your setup, run a provider such as
[`bgutil-ytdlp-pot-provider`](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
and pass the token through with `YT_PO_TOKEN`. Only worth it once the layers
above have been exhausted.

### Recommended VPS configuration

```bash
# Persist the cache across restarts — this is what keeps you serving while blocked
export YT_CACHE_DIR=/var/lib/youtube-mcp/cache
export YT_CACHE_TTL=2592000        # 30 days

# Stay under the radar rather than recovering from blocks
export YT_MIN_INTERVAL=2
export YT_RETRIES=3

# Free first, then the residential exit
export YT_PLAYER_CLIENTS=tv,android_vr,web_safari,mweb,web
export YT_PROXY=socks5h://127.0.0.1:1080

# Keep responses inside the agent's context window
export YT_MAX_CHARS=8000
```

Then confirm with `uv run python server.py --check` before pointing the agent at it.

## Configuration reference

All configuration is via environment variables. All are optional.

| Variable | Default | What it does |
| --- | --- | --- |
| `YT_PROXY` | unset | Proxy URL, or a comma-separated pool rotated on block. Falls back to `HTTPS_PROXY`/`HTTP_PROXY`. `http://`, `https://` and `socks5h://` all work. |
| `YT_WEBSHARE_USERNAME` | unset | Webshare rotating residential pool username. |
| `YT_WEBSHARE_PASSWORD` | unset | Webshare password. Takes precedence over `YT_PROXY` for transcripts. |
| `YT_COOKIES` | unset | Path to a Netscape-format `cookies.txt`. |
| `YT_PLAYER_CLIENTS` | `tv,android_vr,web_safari,mweb,web` | yt-dlp player clients to try, in order. |
| `YT_PO_TOKEN` | unset | Proof-of-origin token passed to yt-dlp. |
| `YT_CACHE_DIR` | `~/.cache/youtube-mcp` | On-disk transcript cache location. |
| `YT_CACHE_TTL` | `2592000` (30d) | Cache lifetime in seconds. `0` disables the cache. |
| `YT_MIN_INTERVAL` | `0` | Minimum seconds between outbound YouTube requests. |
| `YT_RETRIES` | `3` | Attempts on the primary path before falling back to yt-dlp. |
| `YT_TIMEOUT` | `30` | Socket/HTTP timeout in seconds. |
| `YT_MAX_CHARS` | `8000` | Default output cap. `0` means unlimited. |
| `YT_LOG_LEVEL` | `INFO` | Python log level, written to stderr. |

## Using with MCP Clients

This server works with any MCP-compatible client. The tools are discovered
automatically.

### Example Usage

1. Start the server using one of the methods above
2. Open your MCP client
3. Verify that the YouTube tools are available
4. Try:
   - "List the latest videos from https://www.youtube.com/@veritasium"
   - "Get the transcript of this video: [video_url]"
   - "Transcribe the last 5 videos from @veritasium"
   - "Check whether this machine can reach YouTube"

## Development

```bash
uv sync
uv run pytest -q
```

The test suite is offline by design — nothing in it reaches YouTube — so it runs
anywhere, including on a blocked host.

## Debugging

1. Run `uv run python server.py --check` first. Most "it doesn't work" reports on
   a VPS are an IP block, and that command says so explicitly.
2. Raise the log level: `YT_LOG_LEVEL=DEBUG`. Logs go to stderr; stdout is
   reserved for the MCP protocol.
3. Check that dependencies are installed (`uv sync`).
4. Some videos have subtitles disabled — that is reported per video, not a
   server error.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](./LICENSE) file for details.
