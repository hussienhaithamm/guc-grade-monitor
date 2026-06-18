#!/usr/bin/env python3
"""
Monitor a GUC transcript page for newly posted grades.

The script intentionally stores only a SHA-256 signature in state/last_seen.json.
It does not persist transcript contents or cookies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, time
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


DEFAULT_TRANSCRIPT_URL = "https://apps.guc.edu.eg/student_ext/Grade/Transcript_001.aspx"
DEFAULT_TARGET_YEAR = "2025-2026"
DEFAULT_TIMEZONE = "Africa/Cairo"
DEFAULT_CHECK_START = "09:00"
DEFAULT_CHECK_END = "17:30"
DEFAULT_STATE_FILE = "state/last_seen.json"
MAX_EMAIL_BODY_CHARS = 12000
MONITOR_STATE_VERSION = 3
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_NTLM_SESSION = None

EVALUATION_KEYWORDS = (
    "evaluate",
    "evaluation",
    "questionnaire",
    "survey",
    "feedback",
)
EVALUATION_ACTION_KEYWORDS = (
    "evaluate",
    "questionnaire",
    "survey",
    "feedback",
)
EVALUATION_REQUIREMENT_MARKERS = (
    "please",
    "required",
    "require",
    "must",
    "before",
    "view",
    "posted",
    "complete",
    "fill",
)


class MonitorError(Exception):
    pass


class AuthError(MonitorError):
    pass


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag.lower() in {"br", "p", "div", "tr", "td", "th", "li", "option", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if tag.lower() in {"p", "div", "tr", "li", "table", "section"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if data and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = []
        for line in raw.splitlines():
            clean = re.sub(r"\s+", " ", line).strip()
            if clean:
                lines.append(clean)
        return "\n".join(lines)


class FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self.selects: list[dict[str, object]] = []
        self._current_select: dict[str, object] | None = None
        self._current_option: dict[str, str] | None = None
        self._option_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name.lower(): value or "" for name, value in attrs}
        tag = tag.lower()

        if tag == "input":
            name = attr.get("name")
            if name:
                self.fields[name] = attr.get("value", "")
            return

        if tag == "select":
            name = attr.get("name")
            if not name:
                return
            self._current_select = {
                "name": name,
                "id": attr.get("id", ""),
                "options": [],
            }
            return

        if tag == "option" and self._current_select is not None:
            self._current_option = {
                "value": attr.get("value", ""),
                "selected": "selected" in attr,
            }
            self._option_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "option" and self._current_select is not None and self._current_option is not None:
            option = dict(self._current_option)
            option["text"] = re.sub(r"\s+", " ", "".join(self._option_text)).strip()
            self._current_select["options"].append(option)
            if option.get("selected"):
                self.fields[str(self._current_select["name"])] = option.get("value", "")
            self._current_option = None
            self._option_text = []
            return

        if tag == "select" and self._current_select is not None:
            self.selects.append(self._current_select)
            self._current_select = None

    def handle_data(self, data: str) -> None:
        if self._current_option is not None:
            self._option_text.append(data)


class PageDiagnosticsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.forms: list[str] = []
        self.inputs: list[str] = []
        self.selects: list[str] = []
        self.anchors: list[str] = []
        self.meta_refreshes: list[str] = []
        self.script_sources: list[str] = []
        self.inline_script_parts: list[str] = []
        self._in_title = False
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name.lower(): value or "" for name, value in attrs}
        tag = tag.lower()

        if tag == "title":
            self._in_title = True
            return

        if tag == "form":
            self.forms.append(attr.get("action", ""))
            return

        if tag == "input":
            self.inputs.append(attr.get("name", "") or attr.get("id", "") or attr.get("type", ""))
            return

        if tag == "select":
            self.selects.append(attr.get("name", "") or attr.get("id", ""))
            return

        if tag == "a":
            href = attr.get("href")
            if href:
                self.anchors.append(href)
            return

        if tag == "meta" and attr.get("http-equiv", "").lower() == "refresh":
            self.meta_refreshes.append(attr.get("content", ""))
            return

        if tag == "script":
            src = attr.get("src")
            if src:
                self.script_sources.append(src)
            self._in_script = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif self._in_script:
            self.inline_script_parts.append(data)


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status: int
    text: str


def normalize_year(value: str) -> str:
    return re.sub(r"\D+", "", value)


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def declared_charset(content_type: str) -> str | None:
    match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    return match.group(1) if match else None


def decode_response_body(body: bytes, content_type: str = "") -> str:
    if not body:
        return ""

    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        return body.decode("utf-16", errors="replace")
    if body.startswith(b"\xef\xbb\xbf"):
        return body.decode("utf-8-sig", errors="replace")

    even_bytes = body[0::2]
    odd_bytes = body[1::2]
    even_null_ratio = even_bytes.count(0) / max(len(even_bytes), 1)
    odd_null_ratio = odd_bytes.count(0) / max(len(odd_bytes), 1)
    if odd_null_ratio > 0.25 and even_null_ratio < 0.05:
        return body.decode("utf-16le", errors="replace")
    if even_null_ratio > 0.25 and odd_null_ratio < 0.05:
        return body.decode("utf-16be", errors="replace")

    charset = declared_charset(content_type)
    candidates = [charset] if charset else []
    candidates.extend(["utf-8", "windows-1256", "cp1252"])

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        normalized = candidate.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue

    return body.decode("utf-8", errors="replace")


def within_check_window(now: datetime) -> bool:
    if parse_bool(env("FORCE_CHECK"), False):
        return True

    skip_days = {
        day.strip().lower()
        for day in env("SKIP_DAYS", "friday").split(",")
        if day.strip()
    }
    if now.strftime("%A").lower() in skip_days:
        return False

    start = parse_hhmm(env("CHECK_START", DEFAULT_CHECK_START))
    end = parse_hhmm(env("CHECK_END", DEFAULT_CHECK_END))
    current = now.time().replace(second=0, microsecond=0)
    return start <= current <= end


def load_urls() -> list[str]:
    urls_json = env("MONITOR_URLS_JSON")
    if urls_json:
        data = json.loads(urls_json)
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            raise MonitorError("MONITOR_URLS_JSON must be a JSON array of URL strings.")
        return data

    urls_raw = env("MONITOR_URLS")
    if urls_raw:
        return [url.strip() for url in urls_raw.split(",") if url.strip()]

    transcript_url = env("TRANSCRIPT_URL", DEFAULT_TRANSCRIPT_URL)
    return [transcript_url]


def canonicalize_transcript_url(url: str) -> str:
    """Remove GUC's generated transcript URL marker for hashing/display."""
    parsed = urlsplit(url)
    if not parsed.path.lower().endswith("/grade/transcript_001.aspx"):
        return url

    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() != "v"
        ]
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def transcript_url_with_generated_v(url: str, generated_v: str) -> str:
    parsed = urlsplit(canonicalize_transcript_url(url))
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "v"
    ]
    query_items.append(("v", generated_v))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query_items), parsed.fragment))


def transcript_url_candidates(url: str) -> list[str]:
    candidates = [url]
    canonical_url = canonicalize_transcript_url(url)
    if canonical_url != url:
        candidates.append(canonical_url)
    return candidates


def ntlm_credentials() -> tuple[str, str] | None:
    username = env("GUC_USERNAME")
    password = env("GUC_PASSWORD")
    if username and password:
        return username, password
    return None


def ensure_auth_configured(cookie_header: str | None) -> None:
    if ntlm_credentials() or cookie_header:
        return
    raise MonitorError(
        "Authentication is not configured. Set GUC_USERNAME and GUC_PASSWORD "
        "as GitHub secrets, or set SESSION_COOKIE as a fallback."
    )


def request_url(
    url: str,
    cookie_header: str | None,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
    referer: str | None = None,
) -> FetchResult:
    credentials = ntlm_credentials()
    if credentials:
        return request_url_with_ntlm(
            url,
            credentials,
            method=method,
            data=data,
            referer=referer,
        )

    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    if referer:
        headers["Referer"] = referer

    body = None
    if data is not None:
        body = urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "")
            body = decode_response_body(response.read(), content_type)
            return FetchResult(
                url=url,
                final_url=response.geturl(),
                status=response.status,
                text=body,
            )
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise AuthError(
                f"{url} returned HTTP {exc.code}. The university session cookie is missing, expired, or not accepted."
            ) from exc
        raise MonitorError(f"{url} returned HTTP {exc.code}.") from exc
    except URLError as exc:
        raise MonitorError(f"Could not reach {url}: {exc.reason}") from exc


def request_url_with_ntlm(
    url: str,
    credentials: tuple[str, str],
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
    referer: str | None = None,
) -> FetchResult:
    global _NTLM_SESSION

    try:
        import requests
        from requests_ntlm import HttpNtlmAuth
    except ImportError as exc:
        raise MonitorError(
            "NTLM login requires dependencies. Run `pip install -r requirements.txt`."
        ) from exc

    if _NTLM_SESSION is None:
        username, password = credentials
        session = requests.Session()
        session.auth = HttpNtlmAuth(username, password)
        _NTLM_SESSION = session

    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer

    try:
        response = _NTLM_SESSION.request(
            method,
            url,
            data=data,
            headers=headers,
            timeout=30,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise MonitorError(f"Could not reach {url}: {exc}") from exc

    if response.status_code in {401, 403}:
        raise AuthError(
            f"{url} returned HTTP {response.status_code}. Check GUC_USERNAME/GUC_PASSWORD "
            "or try including the Windows domain, for example GUC\\username."
        )
    if response.status_code >= 400:
        raise MonitorError(f"{url} returned HTTP {response.status_code}.")

    return FetchResult(
        url=url,
        final_url=response.url,
        status=response.status_code,
        text=decode_response_body(response.content, response.headers.get("Content-Type", "")),
    )


def parse_form(html: str) -> FormParser:
    parser = FormParser()
    parser.feed(html)
    return parser


def find_study_year_selection(html: str, target_year: str) -> tuple[str, str] | None:
    form = parse_form(html)
    target = normalize_year(target_year)

    for select in form.selects:
        name = str(select["name"])
        options = select.get("options", [])
        if not isinstance(options, list):
            continue
        for option in options:
            if not isinstance(option, dict):
                continue
            option_text = str(option.get("text", ""))
            if normalize_year(option_text) == target:
                return name, str(option.get("value", ""))
    return None


def select_transcript_year(result: FetchResult, cookie_header: str | None, target_year: str) -> FetchResult:
    selection = find_study_year_selection(result.text, target_year)
    if selection is None:
        return result

    select_name, selected_value = selection
    form = parse_form(result.text)
    fields = dict(form.fields)
    fields[select_name] = selected_value
    fields["__EVENTTARGET"] = select_name
    fields.setdefault("__EVENTARGUMENT", "")
    fields.setdefault("__LASTFOCUS", "")

    return request_url(
        result.final_url,
        cookie_header,
        method="POST",
        data=fields,
        referer=result.final_url,
    )


def fetch_transcript_once(url: str, cookie_header: str | None, target_year: str) -> FetchResult:
    initial = request_url(url, cookie_header)
    initial_text = html_to_visible_text(initial.text)
    if looks_like_login_page(initial, initial_text):
        raise AuthError(
            f"{url} loaded a login page instead of transcript content. Refresh the stored session cookie."
        )
    initial = follow_transcript_redirect_challenge(initial, cookie_header)

    selected = select_transcript_year(initial, cookie_header, target_year)
    selected_text = html_to_visible_text(selected.text)
    if looks_like_login_page(selected, selected_text):
        raise AuthError(
            f"{url} loaded a login page after selecting {target_year}. Refresh the stored session cookie."
        )
    selected = follow_transcript_redirect_challenge(selected, cookie_header)
    return selected


def fetch_transcript(url: str, cookie_header: str | None, target_year: str) -> FetchResult:
    errors: list[str] = []

    for candidate in transcript_url_candidates(url):
        try:
            result = fetch_transcript_once(candidate, cookie_header, target_year)
            build_monitored_text([result], target_year)
            if candidate != url:
                print(
                    "Generated transcript URL did not contain monitorable content; "
                    "using the stable transcript endpoint instead."
                )
            return result
        except AuthError as exc:
            errors.append(f"{canonicalize_transcript_url(candidate)}: auth failed: {exc}")
        except MonitorError as exc:
            errors.append(f"{canonicalize_transcript_url(candidate)}: {exc}")

    if errors and all(": auth failed:" in error for error in errors):
        raise AuthError("No transcript URL candidate authenticated successfully. " + " | ".join(errors))
    raise MonitorError("No transcript URL candidate contained monitorable transcript content. " + " | ".join(errors))


def html_to_visible_text(html: str) -> str:
    parser = VisibleTextParser()
    parser.feed(html)
    return parser.text()


def looks_like_login_page(result: FetchResult, visible_text: str) -> bool:
    lower_url = result.final_url.lower()
    lower_html = result.text.lower()
    lower_text = visible_text.lower()
    return (
        "login" in lower_url
        or "type=\"password\"" in lower_html
        or "type='password'" in lower_html
        or ("username" in lower_text and "password" in lower_text)
    )


def limited_join(values: Iterable[str], *, limit: int = 5) -> str:
    cleaned = [value.strip() for value in values if value and value.strip()]
    if not cleaned:
        return "none"
    clipped = cleaned[:limit]
    suffix = "" if len(cleaned) <= limit else f", ... +{len(cleaned) - limit} more"
    return ", ".join(clipped) + suffix


def js_string_unescape(value: str) -> str:
    return bytes(value, "utf-8").decode("unicode_escape")


def unpack_packed_javascript(script_text: str) -> list[str]:
    unpacked: list[str] = []
    pattern = re.compile(
        r"eval\(function\(p,a,c,k,e,[rd]\).*?\}\('(?P<payload>(?:\\'|[^'])*)',"
        r"(?P<base>\d+),(?P<count>\d+),'(?P<symbols>(?:\\'|[^'])*)'\.split\('\|'\)",
        re.S,
    )

    for match in pattern.finditer(script_text):
        payload = js_string_unescape(match.group("payload"))
        base = int(match.group("base"))
        count = int(match.group("count"))
        symbols = js_string_unescape(match.group("symbols")).split("|")
        if len(symbols) < count:
            symbols.extend([""] * (count - len(symbols)))

        replacements = {
            encode_base_number(index, base): symbol
            for index, symbol in enumerate(symbols[:count])
            if symbol
        }

        def replace_word(word_match: re.Match[str]) -> str:
            word = word_match.group(0)
            return replacements.get(word, word)

        unpacked.append(re.sub(r"\b\w+\b", replace_word, payload))

    return unpacked


def encode_base_number(value: int, base: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if value == 0:
        return "0"
    result = []
    while value:
        value, remainder = divmod(value, base)
        result.append(digits[remainder])
    return "".join(reversed(result))


def extract_javascript_locations(script_text: str) -> list[str]:
    return re.findall(
        r"""(?:location(?:\.href)?|document\.location|window\.open)\s*(?:=|\()\s*['"]([^'"]+)['"]""",
        script_text,
        re.I,
    )


def extract_javascript_cookie_names(script_text: str) -> list[str]:
    cookie_values = re.findall(r"""document\.cookie\s*=\s*['"]([^'"]+)['"]""", script_text, re.I)
    return [value.split("=", 1)[0] for value in cookie_values if "=" in value]


def extract_transcript_v_arguments(script_text: str) -> list[str]:
    arguments = re.findall(r"""\bsTo\s*\(\s*['"]?([A-Za-z0-9_-]+)['"]?\s*\)""", script_text)
    return [
        argument
        for argument in arguments
        if len(argument) >= 6 and any(char.isdigit() for char in argument)
    ]


def script_texts_from_html(html: str) -> str:
    parser = PageDiagnosticsParser()
    parser.feed(html)
    script_text = "\n".join(parser.inline_script_parts)
    unpacked_text = "\n".join(unpack_packed_javascript(script_text))
    return f"{script_text}\n{unpacked_text}"


def transcript_redirect_challenge_urls(result: FetchResult) -> list[str]:
    script_text = script_texts_from_html(result.text)
    urls = []
    for generated_v in extract_transcript_v_arguments(script_text):
        challenge_url = transcript_url_with_generated_v(result.final_url, generated_v)
        if challenge_url not in urls:
            urls.append(challenge_url)
    return urls


def visible_text_has_monitorable_body(visible: str) -> bool:
    lines = [line.strip() for line in visible.splitlines() if line.strip()]
    return bool(extract_transcript_region(lines) or extract_course_evaluation_request(lines))


def follow_transcript_redirect_challenge(result: FetchResult, cookie_header: str | None) -> FetchResult:
    visible = html_to_visible_text(result.text)
    if visible_text_has_monitorable_body(visible):
        return result

    for challenge_url in transcript_redirect_challenge_urls(result):
        print("Following GUC transcript generated URL challenge.")
        challenged = request_url(challenge_url, cookie_header, referer=result.final_url)
        challenged_text = html_to_visible_text(challenged.text)
        if looks_like_login_page(challenged, challenged_text):
            raise AuthError(
                f"{canonicalize_transcript_url(challenge_url)} loaded a login page after the generated URL challenge."
            )
        return challenged

    return result


def redacted_snippet(text: str, *, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    compact = re.sub(r"""document\.cookie\s*=\s*(['"])(.*?)\1""", "document.cookie=<redacted>", compact, flags=re.I)
    compact = re.sub(r"""(['"])[A-Za-z0-9+/=_-]{24,}\1""", r"\1<redacted>\1", compact)
    if not compact:
        return "none"
    if len(compact) > limit:
        return f"{compact[:limit]}..."
    return compact


def page_diagnostics(result: FetchResult, visible_line_count: int) -> str:
    parser = PageDiagnosticsParser()
    parser.feed(result.text)

    script_text = "\n".join(parser.inline_script_parts)
    inline_aspx_refs = re.findall(r"""['"]([^'"]+\.aspx(?:\?[^'"]*)?)['"]""", script_text, re.I)
    location_refs = extract_javascript_locations(script_text)
    unpacked_scripts = unpack_packed_javascript(script_text)
    unpacked_text = "\n".join(unpacked_scripts)
    unpacked_aspx_refs = re.findall(r"""['"]([^'"]+\.aspx(?:\?[^'"]*)?)['"]""", unpacked_text, re.I)
    unpacked_location_refs = extract_javascript_locations(unpacked_text)
    unpacked_cookie_names = extract_javascript_cookie_names(unpacked_text)
    transcript_v_args = extract_transcript_v_arguments(f"{script_text}\n{unpacked_text}")
    null_chars = result.text.count("\x00")
    whitespace_chars = sum(1 for char in result.text if char.isspace())
    control_chars = sum(1 for char in result.text if ord(char) < 32 and not char.isspace())
    prefix_codepoints = " ".join(f"{ord(char):02x}" for char in result.text[:24]) or "none"

    return (
        f"final_url={canonicalize_transcript_url(result.final_url)}; "
        f"status={result.status}; response_length={len(result.text)}; "
        f"visible_lines={visible_line_count}; "
        f"whitespace_chars={whitespace_chars}; control_chars={control_chars}; null_chars={null_chars}; "
        f"prefix_codepoints={prefix_codepoints}; "
        f"title={limited_join([''.join(parser.title_parts)])}; "
        f"forms={len(parser.forms)}; inputs={len(parser.inputs)}; selects={limited_join(parser.selects)}; "
        f"meta_refresh={limited_join(parser.meta_refreshes)}; "
        f"script_src={limited_join(parser.script_sources)}; "
        f"inline_aspx_refs={limited_join(inline_aspx_refs)}; "
        f"location_refs={limited_join(location_refs)}; "
        f"unpacked_scripts={len(unpacked_scripts)}; "
        f"unpacked_aspx_refs={limited_join(unpacked_aspx_refs)}; "
        f"unpacked_location_refs={limited_join(unpacked_location_refs)}; "
        f"unpacked_cookie_names={limited_join(unpacked_cookie_names)}; "
        f"unpacked_snippet={redacted_snippet(unpacked_text)}; "
        f"transcript_v_args={limited_join(transcript_v_args)}; "
        f"raw_script_snippet={redacted_snippet(script_text)}; "
        f"anchors={limited_join(parser.anchors)}"
    )


def extract_year_section(lines: list[str], target_year: str) -> list[str]:
    year_re = re.compile(r"\b20\d{2}\s*[/-]\s*20\d{2}\b")
    normalized_target = normalize_year(target_year)

    start = None
    for idx, line in enumerate(lines):
        if normalize_year(line).find(normalized_target) != -1:
            start = idx
            break

    if start is None:
        return []

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if year_re.search(lines[idx]):
            end = idx
            break
    return lines[start:end]


def extract_transcript_region(lines: list[str]) -> list[str]:
    info_idx = None
    for idx, line in enumerate(lines):
        if line_is_info_marker(line):
            info_idx = idx
            break

    transcript_indexes = [
        idx
        for idx, line in enumerate(lines)
        if line.rstrip(":").lower() == "transcript"
    ]
    if not transcript_indexes:
        return []

    if info_idx is not None:
        transcript_idx = next((idx for idx in transcript_indexes if idx > info_idx), None)
        start = info_idx
    else:
        transcript_idx = transcript_indexes[0]
        start = transcript_idx

    if transcript_idx is None:
        return []

    end = len(lines)
    for idx in range(transcript_idx + 1, len(lines)):
        if lines[idx].lower().replace(" ", "") == "shortcuts":
            end = idx
            break

    return lines[start:end]


def line_is_info_marker(line: str) -> bool:
    return line.rstrip(":").lower() in {"your info", "student info", "student information"}


def extract_keyword_neighborhood(lines: list[str], keywords: Iterable[str], radius: int = 2) -> list[str]:
    lowered_keywords = tuple(keyword.lower() for keyword in keywords)
    selected_indexes: set[int] = set()
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if any(keyword in lowered for keyword in lowered_keywords):
            for offset in range(-radius, radius + 1):
                neighbor = idx + offset
                if 0 <= neighbor < len(lines):
                    selected_indexes.add(neighbor)
    return [lines[idx] for idx in sorted(selected_indexes)]


def extract_course_evaluation_request(
    lines: list[str],
    keywords: Iterable[str] = EVALUATION_KEYWORDS,
) -> list[str]:
    section = extract_keyword_neighborhood(lines, keywords)
    if not section:
        return []

    section_text = " ".join(section).lower()
    if not any(marker in section_text for marker in ("course", "subject", "module")):
        return []
    if "evaluate" in section_text:
        return section
    if not any(marker in section_text for marker in EVALUATION_REQUIREMENT_MARKERS):
        return []
    return section


def line_is_volatile_for_signature(line: str) -> bool:
    clean = re.sub(r"\s+", " ", line).strip()
    short_date = r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    iso_date = r"\d{4}[/-]\d{1,2}[/-]\d{1,2}"

    if re.fullmatch(short_date, clean) or re.fullmatch(iso_date, clean):
        return True

    return bool(
        re.fullmatch(
            rf"(?:print(?:ed)?|generated|date)(?:\s+on)?\s*:?\s*(?:{short_date}|{iso_date})(?:\s+\d{{1,2}}:\d{{2}}(?::\d{{2}})?)?",
            clean,
            re.I,
        )
    )


def stable_monitored_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if not line_is_volatile_for_signature(line)]


def build_monitored_chunk(result: FetchResult, target_year: str) -> list[str]:
    visible = html_to_visible_text(result.text)
    if looks_like_login_page(result, visible):
        raise AuthError(
            f"{canonicalize_transcript_url(result.url)} loaded a login page instead of transcript content. "
            "Refresh the stored session cookie."
        )

    lines = [line.strip() for line in visible.splitlines() if line.strip()]
    display_url = canonicalize_transcript_url(result.url)
    if not lines:
        raise MonitorError(
            f"No visible transcript text found at {display_url}. "
            f"Diagnostics: {page_diagnostics(result, 0)}."
        )

    transcript_region = stable_monitored_lines(extract_transcript_region(lines))
    year_section = stable_monitored_lines(extract_year_section(lines, target_year))
    stable_lines = stable_monitored_lines(lines)
    evaluation_candidates = transcript_region if transcript_region else stable_lines
    evaluation_section = extract_course_evaluation_request(evaluation_candidates)
    if transcript_region and not evaluation_section:
        evaluation_section = extract_course_evaluation_request(stable_lines, EVALUATION_ACTION_KEYWORDS)

    chunks = [f"URL: {display_url}"]
    if transcript_region:
        if normalize_year(" ".join(transcript_region)).find(normalize_year(target_year)) == -1:
            raise MonitorError(
                f"Transcript content was found at {display_url}, but it does not mention "
                f"the configured academic year {target_year}. This prevents monitoring the wrong study year."
            )
        chunks.append(f"--- Selected academic year {target_year} transcript ---")
        chunks.extend(transcript_region)
        if evaluation_section:
            chunks.append("--- Possible course evaluation request(s) ---")
            chunks.extend(evaluation_section)
        return chunks

    if year_section:
        chunks.append(f"--- Academic year {target_year} ---")
        chunks.extend(year_section)
        if evaluation_section:
            chunks.append("--- Possible course evaluation request(s) ---")
            chunks.extend(evaluation_section)
        return chunks

    if evaluation_section:
        chunks.append("--- Possible course evaluation request(s) ---")
        chunks.extend(evaluation_section)
        return chunks

    raise MonitorError(
        f"Could not find transcript content for academic year {target_year} at {display_url}. "
        "This usually means the page did not load the transcript body or the configured URL is wrong. "
        f"Diagnostics: {page_diagnostics(result, len(lines))}."
    )


def build_monitored_text(results: list[FetchResult], target_year: str) -> str:
    chunks: list[str] = []

    for result in results:
        chunks.extend(build_monitored_chunk(result, target_year))

    return "\n".join(chunks).strip()


def signature(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MonitorError(f"State file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise MonitorError(f"State file must contain a JSON object: {path}")
    return data


def save_state(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def email_excerpt(text: str) -> str:
    if len(text) <= MAX_EMAIL_BODY_CHARS:
        return text
    omitted = len(text) - MAX_EMAIL_BODY_CHARS
    return f"{text[:MAX_EMAIL_BODY_CHARS]}\n\n[truncated {omitted} characters]"


def send_email(subject: str, body: str) -> None:
    smtp_host = env("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(env("SMTP_PORT", "465"))
    smtp_username = env("SMTP_USERNAME")
    smtp_password = env("SMTP_PASSWORD")
    from_email = env("EMAIL_FROM", smtp_username)
    to_email = env("EMAIL_TO", smtp_username)

    if not smtp_username or not smtp_password or not from_email or not to_email:
        raise MonitorError(
            "Email is not configured. Set SMTP_USERNAME and SMTP_PASSWORD secrets, "
            "and set EMAIL_TO if the notification address differs from SMTP_USERNAME."
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_email
    message["To"] = to_email
    message.set_content(body)

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as smtp:
            smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise MonitorError(f"Could not send email through {smtp_host}:{smtp_port}: {exc}") from exc


def smtp_is_configured() -> bool:
    return bool(env("SMTP_USERNAME") and env("SMTP_PASSWORD"))


def send_self_test_email() -> int:
    now = datetime.now(timezone.utc).isoformat()
    send_email(
        subject="GUC grade monitor self-test",
        body=(
            "This is a manual self-test from the GUC grade monitor.\n\n"
            f"Sent at: {now}\n\n"
            "If you received this from GitHub Actions, the cloud email path is working. "
            "Run the workflow again with send_current=true to test GUC login plus transcript fetching."
        ),
    )
    print("Self-test email sent.")
    return 0


def notify_failure(error_type: str, exc: Exception) -> None:
    if not parse_bool(env("NOTIFY_ON_FAILURE"), True) or not smtp_is_configured():
        return

    now = datetime.now(timezone.utc).isoformat()
    try:
        send_email(
            subject=f"GUC grade monitor failed: {error_type}",
            body=(
                "The GUC grade monitor failed instead of completing a check.\n\n"
                f"Failure type: {error_type}\n"
                f"Time: {now}\n"
                f"Error: {exc}\n\n"
                "This alert means the monitor may not notify you about newly posted grades "
                "until the issue is fixed. Check the GitHub Actions run logs."
            ),
        )
    except Exception as notify_exc:
        print(f"WARNING: failed to send failure notification: {notify_exc}", file=sys.stderr)


def run(force: bool) -> int:
    timezone = ZoneInfo(env("TIMEZONE", DEFAULT_TIMEZONE))
    now = datetime.now(timezone)

    if force:
        os.environ["FORCE_CHECK"] = "true"

    if not within_check_window(now):
        print(f"Outside check window at {now.isoformat()}; skipping without fetching.")
        return 0

    urls = load_urls()
    cookie = env("SESSION_COOKIE") or env("COOKIE_HEADER") or env("GUC_COOKIE")
    ensure_auth_configured(cookie)
    state_path = Path(env("STATE_FILE", DEFAULT_STATE_FILE))
    target_year = env("TARGET_YEAR", DEFAULT_TARGET_YEAR)
    allow_first_email = parse_bool(env("ALLOW_FIRST_EMAIL"), False)

    results = [fetch_transcript(url, cookie, target_year) for url in urls]
    monitored_text = build_monitored_text(results, target_year)
    current_signature = signature(monitored_text)
    previous_state = load_state(state_path)
    previous_signature = previous_state.get("signature")
    previous_state_version = previous_state.get("monitor_state_version")
    send_current = parse_bool(env("SEND_CURRENT_TRANSCRIPT"), False)

    state = {
        "signature": current_signature,
        "target_year": target_year,
        "urls": [canonicalize_transcript_url(url) for url in urls],
        "last_change_at": now.isoformat(),
        "monitor_state_version": MONITOR_STATE_VERSION,
    }

    if send_current:
        send_email(
            subject=f"GUC transcript snapshot - {target_year}",
            body=(
                f"Transcript snapshot generated at {now.isoformat()}.\n\n"
                f"Target academic year: {target_year}\n\n"
                "Current monitored transcript/evaluation text:\n\n"
                f"{email_excerpt(monitored_text)}"
            ),
        )
        save_state(state_path, state)
        print("Current transcript snapshot emailed and state updated.")
        return 0

    if not previous_signature:
        if allow_first_email:
            send_email(
                subject="GUC transcript monitor baseline created",
                body=(
                    f"Baseline created at {now.isoformat()}.\n\n"
                    "Current monitored transcript/evaluation text:\n\n"
                    f"{email_excerpt(monitored_text)}"
                ),
            )
            print("Baseline created and email sent.")
        else:
            print("Baseline created. No email sent on first run.")
        save_state(state_path, state)
        return 0

    if previous_state_version != MONITOR_STATE_VERSION:
        save_state(state_path, state)
        print(
            f"Monitor normalization changed to version {MONITOR_STATE_VERSION}. "
            "Baseline updated without sending an update email."
        )
        return 0

    if previous_signature == current_signature:
        print("No transcript/evaluation change detected.")
        return 0

    send_email(
        subject="GUC transcript update detected",
        body=(
            f"A transcript or course-evaluation change was detected at {now.isoformat()}.\n\n"
            f"Target academic year: {target_year}\n\n"
            "Current monitored text:\n\n"
            f"{email_excerpt(monitored_text)}\n\n"
            "Open the student portal to confirm the official grade."
        ),
    )
    save_state(state_path, state)
    print("Change detected. Email sent and state updated.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="ignore the Cairo check window")
    parser.add_argument("--self-test-email", action="store_true", help="send a test email and exit")
    args = parser.parse_args()

    try:
        if args.self_test_email or parse_bool(env("SELF_TEST_EMAIL"), False):
            return send_self_test_email()
        return run(force=args.force)
    except AuthError as exc:
        print(f"AUTH ERROR: {exc}", file=sys.stderr)
        notify_failure("auth", exc)
        return 2
    except MonitorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        notify_failure("monitor", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
