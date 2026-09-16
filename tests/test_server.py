"""Unit tests for the pure, network-free parts of the server."""

import json

import pytest

import server as s


# --- video id parsing ------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("  https://www.youtube.com/watch?v=dQw4w9WgXcQ  ", "dQw4w9WgXcQ"),
])
def test_extract_video_id_accepts_real_videos(value, expected):
    assert s.extract_video_id(value) == expected


@pytest.mark.parametrize("value", [
    # Regression: the old loose fallback turned these into bogus 11-char ids.
    "https://example.com/some-article-about-things",
    "https://www.youtube.com/playlist?list=PLabcdefghijk1234",
    "https://www.youtube.com/@veritasium",
    "https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA",
    "https://vimeo.com/dQw4w9WgXcQ",
    "the last video from my channel please",
    "",
    None,
])
def test_extract_video_id_refuses_non_videos(value):
    assert s.extract_video_id(value) is None


# --- credential redaction --------------------------------------------------
def test_redact_strips_proxy_credentials():
    msg = "ProxyError: failed to connect to http://bob:hunter2@proxy.example.com:8080"
    out = s.redact(msg)
    assert "hunter2" not in out and "bob" not in out
    assert "proxy.example.com:8080" in out


def test_redact_leaves_ordinary_text_alone():
    assert s.redact("mail me at someone@example.com") == "mail me at someone@example.com"


def test_safe_err_is_single_line_redacted_and_capped():
    err = RuntimeError("line one\nline two http://u:p@host:1 " + "x" * 1000)
    out = s.safe_err(err)
    assert "\n" not in out
    assert "u:p" not in out
    assert len(out) <= s._MAX_ERR_CHARS


# --- block detection -------------------------------------------------------
@pytest.mark.parametrize("message", [
    "IpBlocked: YouTube is blocking requests from your IP",
    "HTTP Error 429: Too Many Requests",
    "Sign in to confirm you're not a bot",
    "HTTP Error 403: Forbidden",
])
def test_is_block_detects_blocks(message):
    assert s._is_block(RuntimeError(message)) is True


@pytest.mark.parametrize("message", [
    "Subtitles are disabled for this video",
    "No json3 caption format for en",
])
def test_is_block_ignores_ordinary_failures(message):
    assert s._is_block(RuntimeError(message)) is False


def test_is_block_handles_none():
    assert s._is_block(None) is False


# --- argument coercion (what small function-calling models actually send) ---
def test_as_int_coerces_null_and_strings():
    assert s.as_int({"max_chars": None}, "max_chars", 8000) == 8000
    assert s.as_int({"max_chars": ""}, "max_chars", 8000) == 8000
    assert s.as_int({"max_chars": "500"}, "max_chars", 8000) == 500
    assert s.as_int({"max_chars": "abc"}, "max_chars", 8000) == 8000
    assert s.as_int({}, "max_chars", 8000) == 8000
    assert s.as_int({"max_chars": 0}, "max_chars", 8000) == 0


def test_as_int_clamps():
    assert s.as_int({"n": 999}, "n", 5, lo=1, hi=15) == 15
    assert s.as_int({"n": -4}, "n", 5, lo=1, hi=15) == 1


def test_as_bool_coerces_strings_and_null():
    assert s.as_bool({"strip_ads": "true"}, "strip_ads") is True
    assert s.as_bool({"strip_ads": "False"}, "strip_ads") is False
    assert s.as_bool({"strip_ads": None}, "strip_ads", True) is True
    assert s.as_bool({}, "strip_ads") is False


def test_as_languages_accepts_list_string_and_null():
    assert s.as_languages({"languages": ["pt", "en"]}) == ["pt", "en"]
    assert s.as_languages({"languages": "pt, en"}) == ["pt", "en"]
    assert s.as_languages({"languages": None}) == s.DEFAULT_LANGUAGES
    assert s.as_languages({"languages": []}) == s.DEFAULT_LANGUAGES
    assert s.as_languages({}) == s.DEFAULT_LANGUAGES


def test_as_text_rejects_missing_with_a_usable_message():
    with pytest.raises(ValueError, match="channel"):
        s.as_text({}, "channel")
    with pytest.raises(ValueError):
        s.as_text({"channel": "   "}, "channel")


# --- transcript formatting -------------------------------------------------
def _snips(n=40, step=10.0):
    return [{"text": f"word{i}", "start": i * step, "duration": step} for i in range(n)]


def test_format_transcript_groups_into_paragraphs():
    out = s.format_transcript(_snips(10), include_timestamps=False, max_chars=0)
    assert "\n\n" in out  # 10 snippets * 10s spans several 30s paragraphs
    assert "word0" in out and "word9" in out


def test_format_transcript_timestamps_one_line_each():
    out = s.format_transcript(_snips(3), include_timestamps=True, max_chars=0)
    assert out.splitlines()[0].startswith("[0:00] ")
    assert len(out.splitlines()) == 3


def test_format_transcript_truncates_and_reports_resume_offset():
    out = s.format_transcript(_snips(200), include_timestamps=False, max_chars=100)
    assert "truncated" in out
    assert "offset=" in out


def test_format_transcript_offset_paging_covers_the_whole_text():
    full = s.format_transcript(_snips(200), max_chars=0)
    first = s.format_transcript(_snips(200), max_chars=200)
    offset = int(first.split("offset=")[1].split(" ")[0].rstrip("]").rstrip(","))
    rest = s.format_transcript(_snips(200), max_chars=0, offset=offset)
    body = rest.split("\n\n[end of transcript")[0]
    assert full.endswith(body.strip())


def test_format_transcript_offset_beyond_end_is_safe():
    assert "end of transcript" in s.format_transcript(_snips(3), max_chars=0, offset=10_000)


def test_format_transcript_skips_blank_snippets():
    out = s.format_transcript([{"text": "  ", "start": 0.0}, {"text": "hi", "start": 1.0}], max_chars=0)
    assert out == "hi"


def test_fmt_ts():
    assert s._fmt_ts(0) == "0:00"
    assert s._fmt_ts(61) == "1:01"
    assert s._fmt_ts(3661) == "1:01:01"


# --- caption track selection ----------------------------------------------
def test_pick_caption_lang_prefers_exact_then_base_then_any():
    assert s._pick_caption_lang({"en": [], "pt": []}, ["pt", "en"]) == "pt"
    assert s._pick_caption_lang({"pt-BR": [], "en": []}, ["pt"]) == "pt-BR"
    assert s._pick_caption_lang({"de": []}, ["pt", "en"]) == "de"


# --- json3 parsing ---------------------------------------------------------
def test_parse_json3():
    data = {"events": [
        {"tStartMs": 0, "dDurationMs": 1500, "segs": [{"utf8": "hello "}, {"utf8": "world"}]},
        {"tStartMs": 2000, "segs": [{"utf8": "\n"}]},          # whitespace-only: dropped
        {"tStartMs": 3000, "dDurationMs": 500, "segs": [{"utf8": "again"}]},
    ]}
    out = s._parse_json3(data)
    assert [x["text"] for x in out] == ["hello world", "again"]
    assert out[0]["start"] == 0.0 and out[0]["duration"] == 1.5
    assert out[1]["start"] == 3.0


def test_parse_json3_empty():
    assert s._parse_json3({}) == []


# --- ad stripping ----------------------------------------------------------
def test_strip_ad_snippets_removes_only_the_sponsor_window():
    snippets = [{"text": "a", "start": 5.0}, {"text": "ad", "start": 30.0}, {"text": "b", "start": 90.0}]
    chapters = [{"start_time": 20.0, "end_time": 60.0, "title": "Sponsor"}]
    assert [x["text"] for x in s._strip_ad_snippets(snippets, chapters)] == ["a", "b"]


def test_strip_ad_snippets_without_chapters_is_a_noop():
    snippets = [{"text": "a", "start": 5.0}]
    assert s._strip_ad_snippets(snippets, []) is snippets


def test_ad_chapter_regex():
    assert s.AD_CHAPTER_RE.search("Sponsor segment")
    assert s.AD_CHAPTER_RE.search("A quick ad")
    assert not s.AD_CHAPTER_RE.search("Introduction")


# --- snippet normalisation -------------------------------------------------
class _Obj:
    def __init__(self, text, start, duration):
        self.text, self.start, self.duration = text, start, duration


def test_to_dicts_handles_objects_dicts_and_junk():
    out = s._to_dicts([_Obj("a", 1.0, 2.0), {"text": "b", "start": "3.5"}, {"text": "c", "start": "nope"}])
    assert out[0] == {"text": "a", "start": 1.0, "duration": 2.0}
    assert out[1]["start"] == 3.5
    assert out[2]["start"] == 0.0
    assert json.dumps(out)  # must stay JSON-serialisable for the disk cache


# --- disk cache ------------------------------------------------------------
def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(s, "_CACHE_TTL", 3600)
    snippets = [{"text": "hi", "start": 0.0, "duration": 1.0}]
    s.cache_store("vid1", ["en"], snippets)
    assert s.cache_load("vid1", ["en"]) == snippets
    assert s.cache_load("vid1", ["pt"]) is None   # keyed on language too
    assert s.cache_load("vid2", ["en"]) is None


def test_cache_disabled_when_ttl_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(s, "_CACHE_TTL", 0)
    s.cache_store("vid1", ["en"], [{"text": "hi", "start": 0.0}])
    assert s.cache_load("vid1", ["en"]) is None
    assert not list(tmp_path.glob("*.json"))


def test_cache_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(s, "_CACHE_TTL", 3600)
    s.cache_store("vid1", ["en"], [{"text": "hi", "start": 0.0}])
    monkeypatch.setattr(s, "_CACHE_TTL", 1)
    path = s._cache_path("vid1", ["en"])
    import os
    os.utime(path, (0, 0))
    assert s.cache_load("vid1", ["en"]) is None


def test_cache_survives_a_corrupt_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(s, "_CACHE_TTL", 3600)
    s._cache_path("vid1", ["en"]).write_text("{not json", "utf-8")
    assert s.cache_load("vid1", ["en"]) is None


# --- proxy rotation --------------------------------------------------------
def test_rotate_proxy_cycles_the_pool(monkeypatch):
    monkeypatch.setattr(s, "_PROXY_URLS", ["http://a", "http://b"])
    monkeypatch.setattr(s, "_proxy_index", 0)
    assert s.current_proxy() == "http://a"
    s.rotate_proxy()
    assert s.current_proxy() == "http://b"
    s.rotate_proxy()
    assert s.current_proxy() == "http://a"


def test_current_proxy_without_a_pool(monkeypatch):
    monkeypatch.setattr(s, "_PROXY_URLS", [])
    assert s.current_proxy() is None
    assert s._requests_proxies() is None


# --- yt-dlp options --------------------------------------------------------
def test_yt_dlp_opts_always_sets_a_socket_timeout():
    assert s._yt_dlp_opts()["socket_timeout"] == s._TIMEOUT


def test_yt_dlp_opts_passes_the_player_client():
    opts = s._yt_dlp_opts("android_vr")
    assert opts["extractor_args"]["youtube"]["player_client"] == ["android_vr"]


def test_yt_dlp_opts_omits_extractor_args_when_unset():
    assert "extractor_args" not in s._yt_dlp_opts(None)


# --- unavailability message ------------------------------------------------
def test_unavailable_message_names_the_block_and_says_not_to_retry():
    msg = s._unavailable_message("vid", RuntimeError("IpBlocked: blocked"), RuntimeError("HTTP Error 429"))
    assert "blocking this host's IP" in msg
    assert "will not help" in msg
    assert "YT_PROXY" in msg


def test_unavailable_message_distinguishes_a_video_without_subtitles():
    msg = s._unavailable_message("vid", RuntimeError("Subtitles are disabled"), RuntimeError("no caption tracks"))
    assert "subtitles are disabled" in msg
    assert "YT_PROXY" not in msg


def test_unavailable_message_redacts_proxy_credentials():
    err = RuntimeError("ProxyError http://bob:hunter2@proxy:8080 IpBlocked")
    assert "hunter2" not in s._unavailable_message("vid", err, err)


# --- channel helpers -------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    ("@veritasium", "https://www.youtube.com/@veritasium"),
    ("veritasium", "https://www.youtube.com/@veritasium"),
    ("https://www.youtube.com/@veritasium", "https://www.youtube.com/@veritasium"),
])
def test_channel_page_url(value, expected):
    assert s._channel_page_url(value) == expected


def test_resolve_channel_id_passes_through_ids_without_network():
    cid = "UCHnyfMqiRRG1u-2MsSQLbXA"
    assert s.resolve_channel_id(cid) == cid
    assert s.resolve_channel_id(f"https://www.youtube.com/channel/{cid}") == cid


def test_resolve_channel_id_rejects_empty():
    with pytest.raises(ValueError):
        s.resolve_channel_id("  ")
