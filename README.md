# YouTube MCP Server

A Model Context Protocol (MCP) server for reading a YouTube channel's latest videos and fetching their transcripts. **No API key required** — channel listing uses YouTube's public RSS feed and transcripts use `youtube-transcript-api` directly.

## Support Us

If you find this project helpful and would like to support future projects, consider buying us a coffee! Your support helps us continue building innovative AI solutions.

<a href="https://www.buymeacoffee.com/blazzmocompany"><img src="https://img.buymeacoffee.com/button-api/?text=Buy me a coffee&emoji=&slug=blazzmocompany&button_colour=40DCA5&font_colour=ffffff&font_family=Cookie&outline_colour=000000&coffee_colour=FFDD00"></a>

Your contributions go a long way in fueling our passion for creating intelligent and user-friendly applications.

## Table of Contents

- [YouTube MCP Server](#youtube-mcp-server)
- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Running the Server](#running-the-server)
  - [1. Direct Method](#1-direct-method)
  - [2. Configure for Claude.app](#2-configure-for-claudeapp)
- [Available Tools](#available-tools)
- [Using with MCP Clients](#using-with-mcp-clients)
  - [Example Usage](#example-usage)
- [Debugging](#debugging)
- [Contributing](#contributing)
- [License](#license)

## Features

- List a channel's most recent videos from a URL, `@handle`, or channel id — no API key
- Retrieve a single video's transcript (auto language fallback)
- Transcribe a channel's last N videos in one call

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

There are two ways to run the MCP server:

### 1. Direct Method

To start the MCP server directly:

```bash
uv run python server.py
```

### 2. Configure for Claude.app

Add to your Claude settings without using any package manager this works for windows:
```json
"mcpServers": {
  "youtube": {
    "command": "C:\\Path\\To\\Your\\Project\\.venv\\Scripts\\python.exe",
    "args": ["C:\\Path\\To\\Your\\Project\\server.py"]
  }
}
```

Using the uv package manager (works on Windows):

```json
"mcpServers": {
  "youtube": {
    "command": "uv",
    "args": ["--directory", "C:\\Path\\To\\Your\\Project", "run", "server.py"]
  }
}
```

## Available Tools

The server provides the following tools:

1. `get-channel-videos`: List a channel's most recent videos
   - Parameters:
     - channel: Channel URL, `@handle`, or channel id (`UC...`)
     - max_results: How many recent videos (default: 10)

2. `get-transcript`: Get the transcript of a single video
   - Parameters:
     - video_url: Video URL or 11-char video id
     - languages: Optional preferred language codes, best first

3. `get-channel-transcripts`: Transcribe a channel's latest videos in one call
   - Parameters:
     - channel: Channel URL, `@handle`, or channel id (`UC...`)
     - max_results: How many recent videos to transcribe (default: 5)
     - languages: Optional preferred language codes, best first

## Using with MCP Clients

This server can be used with any MCP-compatible client, such as Claude Desktop App. The tools will be automatically discovered and made available to the client.

### Example Usage

1. Start the server using one of the methods described above
2. Open Claude Desktop App
3. Look for the hammer icon to verify that the YouTube tools are available
4. You can now use commands like:
   - "List the latest videos from https://www.youtube.com/@veritasium"
   - "Get the transcript of this video: [video_url]"
   - "Transcribe the last 5 videos from @veritasium"

## Debugging

If you encounter any issues:

1. Check that all dependencies are installed correctly (`uv sync`)
2. Verify that the server is running and listening for connections
3. Look for any error messages in the server output (logged to stderr)
4. Some videos have subtitles disabled — that is reported per-video, not a server error

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](./LICENSE) file for details.