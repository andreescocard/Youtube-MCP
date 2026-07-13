import os, sys
from datetime import datetime
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

import asyncio
import lancedb
import logging
import requests
import subprocess
import tempfile
from dotenv import load_dotenv
from langchain_community.tools import YouTubeSearchTool
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, ListToolsResult, TextContent, Tool
from youtube_transcript_api import YouTubeTranscriptApi

DATA_DIR = "./data"

# Ensure directories exist
os.makedirs(DATA_DIR, exist_ok=True)

# Silence noisy third-party loggers
for logger_name in ["asyncio", "urllib3.connectionpool", "mcp.server.lowlevel.server"]:
    logging.getLogger(logger_name).setLevel(logging.WARNING)

# Get current date for log filename
log_filename = datetime.now().strftime('%d-%m-%y') + '.log'
log_filepath = os.path.join(DATA_DIR, log_filename)

# Configure our application logger with proper formatting
logger = logging.getLogger("youtube-mcp")
logger.setLevel(logging.DEBUG)  # Set our logger to DEBUG level
logger.propagate = False  # Prevent double logging

# Create a formatter with proper newlines
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Create and add console handler
console_handler = logging.StreamHandler(sys.stderr)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# Create and add file handler for logging to file
file_handler = logging.FileHandler(log_filepath)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

load_dotenv()

async def serve(read, write, options):
    """Run the YouTube MCP server."""
    logger.info("Initializing YouTube MCP Server")
    server = Server(name="youtube-mcp-server")
    logger.debug("Created MCP Server instance")
    
    youtube_search = YouTubeSearchTool()
    logger.debug("Initialized YouTube Search Tool")
    
    embeddings = GoogleGenerativeAIEmbeddings(
        model="models/embedding-001",
        google_api_key=os.getenv("GOOGLE_API_KEY"),
        task_type="retrieval_document"  # Set default task type for document storage
    )
    logger.debug("Initialized Google Generative AI Embeddings")
    
    db = lancedb.connect('youtube_db')
    logger.debug("Connected to LanceDB database")
    
    # Create videos table if it doesn't exist
    try:
        videos_table = db.open_table("videos")
        logger.debug("Opened existing videos table")
    except:
        import pyarrow as pa
        videos_table = db.create_table(
            "videos",
            schema=pa.schema([
                pa.field("id", pa.string(), nullable=False),
                pa.field("text", pa.string()),
                pa.field("metadata", pa.struct([]), nullable=True),  # JSON field as struct
                pa.field("vector", pa.list_(pa.float32(), 768))  # Fixed-size list for vector
            ]),
            mode="create"
        )
        logger.debug("Created new videos table")
    
    def clean_text(text: str) -> str:
        """Clean text to remove problematic characters."""
        try:
            # Replace emojis and special characters with their text equivalents
            # or remove them if no good replacement exists
            return text.encode('ascii', 'ignore').decode('ascii')
        except Exception:
            return str(text)

    def extract_video_id(video_url: str) -> str:
        """Normalize common YouTube URL formats into a video id."""
        if len(video_url) == 11 and "/" not in video_url and "?" not in video_url:
            return video_url

        parsed = urlparse(video_url)
        query = parse_qs(parsed.query)
        if "v" in query and query["v"]:
            return query["v"][0]

        path_parts = [part for part in parsed.path.split("/") if part]
        if not path_parts:
            return video_url

        if path_parts[0] == "shorts" and len(path_parts) >= 2:
            return path_parts[1]
        if path_parts[0] == "embed" and len(path_parts) >= 2:
            return path_parts[1]
        return path_parts[-1]

    def resolve_cookie_file(explicit_path: Optional[str] = None) -> Optional[str]:
        """Resolve a Netscape cookie jar from env vars or a caller-supplied path."""
        candidates = [
            explicit_path,
            os.getenv("YOUTUBE_COOKIES_FILE"),
            os.getenv("YOUTUBE_COOKIE_FILE"),
            os.getenv("YOUTUBE_COOKIES_PATH"),
            os.getenv("YT_DLP_COOKIES_FILE"),
            os.path.join(os.getcwd(), "cookies.txt"),
            os.path.join(os.getcwd(), "youtube-cookies.txt"),
            os.path.expanduser("~/.config/youtube-mcp/cookies.txt"),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            expanded = os.path.expanduser(candidate)
            if os.path.isfile(expanded):
                return expanded
        return None

    def build_http_client(cookie_file: Optional[str] = None) -> requests.Session:
        """Build a requests session with optional cookies loaded."""
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            }
        )
        resolved = resolve_cookie_file(cookie_file)
        if resolved:
            jar = MozillaCookieJar()
            jar.load(resolved, ignore_discard=True, ignore_expires=True)
            session.cookies = jar
            logger.info("Loaded YouTube cookies from %s", resolved)
        return session

    def parse_vtt_text(vtt_text: str) -> str:
        """Convert a VTT file into plain text."""
        lines = []
        for raw_line in vtt_text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("WEBVTT") or line.startswith("NOTE"):
                continue
            if "-->" in line:
                continue
            if line.isdigit():
                continue
            lines.append(line)
        return clean_text(" ".join(lines).strip())

    def fetch_transcript_via_ytdlp(video_url: str, languages: Optional[List[str]] = None, cookie_file: Optional[str] = None) -> tuple[str, str]:
        """Fallback transcript fetch using yt-dlp auto captions."""
        languages = languages or ["pt", "pt-BR", "en"]
        cookie = resolve_cookie_file(cookie_file)
        with tempfile.TemporaryDirectory(prefix="youtube-mcp-") as tmpdir:
            cmd = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--cookies",
                cookie,
                "--write-auto-subs",
                "--sub-langs",
                ",".join(languages),
                "--skip-download",
                "--ignore-no-formats-error",
                "--no-playlist",
                "-o",
                str(Path(tmpdir) / "%(id)s.%(ext)s"),
                video_url,
            ]
            cmd = [part for part in cmd if part is not None]
            completed = subprocess.run(cmd, capture_output=True, text=True)
            if completed.returncode != 0:
                raise RuntimeError((completed.stderr or completed.stdout or "yt-dlp failed").strip())

            vtt_files = sorted(Path(tmpdir).glob("*.vtt"))
            if not vtt_files:
                raise RuntimeError("yt-dlp did not produce any subtitle files")

            chosen = None
            for language in languages:
                matches = [p for p in vtt_files if f".{language}." in p.name or p.name.endswith(f".{language}.vtt")]
                if matches:
                    chosen = matches[0]
                    break
            if chosen is None:
                chosen = vtt_files[0]

            transcript_text = parse_vtt_text(chosen.read_text(encoding="utf-8", errors="ignore"))
            if not transcript_text:
                raise RuntimeError("yt-dlp subtitle file was empty")
            return transcript_text, f"yt-dlp:{chosen.name}"

    def fetch_transcript_text(video_url: str, languages: Optional[List[str]] = None, cookie_file: Optional[str] = None) -> tuple[str, str]:
        """Fetch transcript text using youtube-transcript-api with cookie fallback."""
        languages = languages or ["pt", "pt-BR", "en"]
        video_id = extract_video_id(video_url)
        attempts: list[str] = []

        for candidate_cookie in (None, cookie_file):
            try:
                api = YouTubeTranscriptApi(
                    http_client=build_http_client(candidate_cookie)
                    if resolve_cookie_file(candidate_cookie)
                    else None
                )
                fetched = api.fetch(video_id, languages=languages)
                snippets = getattr(fetched, "snippets", None)
                if snippets is None:
                    snippets = list(fetched)
                transcript_lines = []
                for snippet in snippets:
                    text = getattr(snippet, "text", None)
                    if text is None and isinstance(snippet, dict):
                        text = snippet.get("text")
                    if not text:
                        continue
                    transcript_lines.append(text.strip())
                transcript_text = clean_text("\n".join(transcript_lines).strip())
                if transcript_text:
                    source = "youtube-transcript-api"
                    if resolve_cookie_file(candidate_cookie):
                        source += "+cookies"
                    return transcript_text, source
                attempts.append("youtube-transcript-api returned an empty transcript")
            except Exception as exc:
                attempts.append(f"api:{candidate_cookie or 'public'}: {exc}")

        try:
            transcript_text, source = fetch_transcript_via_ytdlp(video_url, languages=languages, cookie_file=cookie_file)
            return transcript_text, source
        except Exception as exc:
            attempts.append(f"ytdlp:{exc}")

        raise RuntimeError("; ".join(attempts) if attempts else "Transcript not found")

    # Register the list_tools handler
    @server.list_tools()
    async def list_tools() -> List[Tool]:
        """Handle listing available tools"""
        logger.info("Handling list_tools request")
        tools = [
            Tool(
                name="search-youtube",
                description="Search for YouTube videos based on a query",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query string"},
                        "max_results": {"type": "integer", "description": "Maximum number of results to return", "default": 5}
                    },
                    "required": ["query"]
                }
            ),
            Tool(
                name="get-transcript",
                description="Get the transcript of a YouTube video",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "video_url": {"type": "string", "description": "URL of the YouTube video"},
                        "cookies_path": {
                            "type": "string",
                            "description": "Optional path to a Netscape cookies.txt export for authenticated transcript access"
                        },
                        "languages": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Preferred transcript languages in order",
                            "default": ["pt", "pt-BR", "en"]
                        }
                    },
                    "required": ["video_url"]
                }
            ),
            Tool(
                name="store-video-info",
                description="Store video information and transcript in the vector database",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "video_url": {"type": "string", "description": "URL of the YouTube video"},
                        "cookies_path": {
                            "type": "string",
                            "description": "Optional path to a Netscape cookies.txt export for authenticated transcript access"
                        },
                        "languages": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Preferred transcript languages in order",
                            "default": ["pt", "pt-BR", "en"]
                        },
                        "metadata": {"type": "object", "description": "Optional metadata about the video"}
                    },
                    "required": ["video_url"]
                }
            ),
            Tool(
                name="search-transcripts",
                description="Search stored video transcripts using semantic search",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "integer", "description": "Maximum number of results to return", "default": 3}
                    },
                    "required": ["query"]
                }
            )
        ]
        logger.debug(f"Returning {len(tools)} tools: {[tool.name for tool in tools]}")
        return tools

    # Register the call_tool handler
    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            logger.info(f"Handling tool call: {name} with arguments: {arguments}")

            if name == "search-youtube":
                max_results = int(arguments.get("max_results", 5))
                results = youtube_search.run(f"{arguments['query']},{max_results}")
                # Properly parse and clean results
                parsed_results = eval(results)  # Consider using json.loads() if results are JSON
                cleaned_results = clean_text(str(parsed_results))
                return [TextContent(type="text", text=cleaned_results)]
            
            elif name == "get-transcript":
                languages = arguments.get("languages") or ["pt", "pt-BR", "en"]
                video_url = arguments["video_url"]
                transcript_text, source = fetch_transcript_text(
                    video_url=video_url,
                    languages=languages,
                    cookie_file=arguments.get("cookies_path"),
                )
                return [
                    TextContent(
                        type="text",
                        text=f"[source={source}]\n{transcript_text}",
                    )
                ]
            
            elif name == "store-video-info":
                languages = arguments.get("languages") or ["pt", "pt-BR", "en"]
                video_url = arguments["video_url"]
                transcript_text, source = fetch_transcript_text(
                    video_url=video_url,
                    languages=languages,
                    cookie_file=arguments.get("cookies_path"),
                )
                cleaned_content = clean_text(transcript_text)
                vector = embeddings.embed_documents(
                    [cleaned_content],
                    task_type="retrieval_document"
                )[0]
                # Use provided metadata and add video metadata from document if available
                cleaned_metadata = {
                    k: clean_text(str(v)) if isinstance(v, str) else v
                    for k, v in arguments.get("metadata", {}).items()
                }
                # Add video_id to metadata
                cleaned_metadata["video_id"] = extract_video_id(video_url)
                cleaned_metadata["transcript_source"] = source
                videos_table.add([{
                    "id": video_url,
                    "text": cleaned_content,
                    "metadata": cleaned_metadata,
                    "vector": vector
                }])
                return [TextContent(type="text", text=f"Successfully stored video information for {cleaned_metadata.get('video_id')} from {source}")]
                
            elif name == "search-transcripts":
                query_vector = embeddings.embed_query(
                    arguments["query"],
                    task_type="retrieval_query"
                )
                results = videos_table.search(
                    query_vector
                ).limit(int(arguments.get("limit", 3))).to_pandas()
                
                formatted_results = []
                for result in results.itertuples():
                    formatted_results.append({
                        "video_url": result.id,
                        "metadata": getattr(result, "metadata", {}),
                        "text_sample": result.text[:200] + "..." if len(result.text) > 200 else result.text,
                        "score": float(getattr(result, "_4", 0.0))  # LanceDB score is in the last column
                    })
                return [TextContent(type="text", text=str(formatted_results))]
                
            else:
                raise ValueError(f"Unknown tool: {name}")
                
        except Exception as e:
            logger.error(f"Error handling tool call: {e}", exc_info=True)
            raise McpError(INVALID_PARAMS, str(e))

    # Create initialization options
    options = server.create_initialization_options()
    logger.debug(f"Created initialization options: {options}")
    
    # Properly use stdio_server as an async context manager
    await server.run(read, write, options, raise_exceptions=True)

async def main():
    try:
        logger.info("Starting YouTube MCP Server...")
        options = {
            "protocolVersion": "0.1.0",
            "capabilities": {}
        }
        async with stdio_server() as (read, write):
            await serve(read, write, options)
    except KeyboardInterrupt:
        logger.info("Server shutting down gracefully...")
        print("\nServer shutting down gracefully...", file=sys.stderr)
    except Exception as e:
        logger.error(f"Fatal error occurred: {e}", exc_info=True)
        print(f"\nFatal error occurred: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Server shutting down...")
