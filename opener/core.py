"""Website URL -> 3-line cold-email opener.

Pipeline (each step is a plain function so it is easy to test and explain):

    normalize_url -> fetch_site -> extract_text   (code)
    draft_opener                                  (model)
    verify_draft  -> retry once with feedback     (code)

The model must return, for every line, the exact phrase from the site it is
based on. verify_draft() checks that each phrase really appears in the text we
scraped, so the opener can't claim things the site doesn't say.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
USER_AGENT = "Mozilla/5.0 (compatible; ColdOpenerBot/0.1; +https://github.com/)"
TIMEOUT_S = 10
MAX_BYTES = 2_000_000          # stop reading a page after ~2 MB
MAX_SOURCE_CHARS = 12_000      # ~3K tokens sent to the model
MAX_EXTRA_PAGES = 2            # e.g. /about and /pricing
EXTRA_PAGE_HINTS = ("about", "pricing", "team", "story", "founder", "company")
MAX_WORDS_PER_LINE = 25
# Filler that makes cold email sound templated; a hit triggers the retry.
BANNED_PHRASES = (
    "i hope this finds you", "i came across", "unique", "smart alternative",
    "balancing act", "game-changer", "game changer", "revolutionize", "impressive",
    "i was blown away", "love what you", "quick question",
)


class OpenerError(Exception):
    """A problem the user should see (bad URL, site unreachable, no API key)."""


@dataclass
class Page:
    url: str
    text: str


@dataclass
class Result:
    url: str
    lines: list[str]
    evidence: list[str]
    founder: str | None
    warnings: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    model: str = DEFAULT_MODEL
    attempts: int = 1
    usage: dict = field(default_factory=dict)

    def as_text(self) -> str:
        return "\n".join(self.lines)


# --------------------------------------------------------------------------- #
# 1. URL handling (code)
# --------------------------------------------------------------------------- #

def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise OpenerError("Enter a website address.")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname or "." not in parsed.hostname:
        raise OpenerError(f"'{raw}' doesn't look like a website address.")
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{parsed.path or '/'}"


def _assert_public_host(url: str) -> None:
    """Refuse internal addresses so the hosted demo can't be used to probe
    private networks (SSRF)."""
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise OpenerError(f"Couldn't find {host}. Check the spelling.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise OpenerError(f"{host} points to a private address; refusing to fetch it.")


# --------------------------------------------------------------------------- #
# 2. Fetching (code)
# --------------------------------------------------------------------------- #

def _get(url: str, session: requests.Session) -> tuple[str, str]:
    """GET with manual redirects (each hop re-checked), timeout and size cap.
    Returns (final_url, html)."""
    for _ in range(5):
        _assert_public_host(url)
        resp = session.get(url, timeout=TIMEOUT_S, stream=True, allow_redirects=False)
        if resp.is_redirect or resp.is_permanent_redirect:
            url = urljoin(url, resp.headers.get("Location", ""))
            resp.close()
            continue
        if resp.status_code >= 400:
            raise OpenerError(f"{url} returned HTTP {resp.status_code}.")
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype:
            raise OpenerError(f"{url} isn't an HTML page ({ctype or 'unknown type'}).")
        body = b""
        for chunk in resp.iter_content(65536):
            body += chunk
            if len(body) > MAX_BYTES:
                break
        resp.close()
        return url, body.decode(resp.encoding or "utf-8", errors="replace")
    raise OpenerError("Too many redirects.")


def _clean_text(html: str) -> tuple[str, BeautifulSoup]:
    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []
    if soup.title and soup.title.string:
        parts.append(f"TITLE: {soup.title.string.strip()}")
    for name in ("description", "og:description", "twitter:description"):
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            parts.append(f"DESCRIPTION: {tag['content'].strip()}")
            break
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        tag.decompose()
    body_text = soup.get_text(separator="\n")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in body_text.splitlines()]
    seen: set[str] = set()
    for ln in lines:
        if len(ln) < 3 or ln.lower() in seen:
            continue
        seen.add(ln.lower())
        parts.append(ln)
    return "\n".join(parts), soup


def _extra_links(base_url: str, soup: BeautifulSoup) -> list[str]:
    """Pick up to MAX_EXTRA_PAGES same-site links, preferring ones whose URL
    path (not just link text) matches an earlier hint like 'about'."""
    base_host = urlparse(base_url).hostname
    scored: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0].split("?")[0].rstrip("/")
        p = urlparse(href)
        if p.hostname != base_host or p.path in ("", "/") or p.path.count("/") > 2:
            continue
        path, text = p.path.lower(), a.get_text(" ").lower()
        for rank, hint in enumerate(EXTRA_PAGE_HINTS):
            score = rank if hint in path else (rank + 10 if hint in text else None)
            if score is not None:
                scored[href] = min(score, scored.get(href, 99))
                break
    return sorted(scored, key=scored.get)[:MAX_EXTRA_PAGES]


def fetch_site(url: str) -> list[Page]:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    try:
        final_url, html = _get(url, session)
    except requests.RequestException as e:
        raise OpenerError(f"Couldn't load {url}: {e.__class__.__name__}.")
    text, soup = _clean_text(html)
    pages = [Page(final_url, text)]
    for link in _extra_links(final_url, soup):
        try:
            u, h = _get(link, session)
            pages.append(Page(u, _clean_text(h)[0]))
        except (OpenerError, requests.RequestException):
            continue  # extra pages are nice-to-have
    return pages


# --------------------------------------------------------------------------- #
# 3. Build the source text the model sees (code)
# --------------------------------------------------------------------------- #

def build_source(pages: list[Page]) -> str:
    budget = MAX_SOURCE_CHARS
    chunks = []
    for i, page in enumerate(pages):
        share = budget if i == len(pages) - 1 else max(budget // 2, 2000)
        snippet = page.text[:share]
        chunks.append(f"=== PAGE: {page.url} ===\n{snippet}")
        budget -= len(snippet)
        if budget <= 0:
            break
    return "\n\n".join(chunks)


# --------------------------------------------------------------------------- #
# 4. Draft (model)
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You write the first three lines of a cold email to the founder of a small software company.

Rules:
- Use ONLY facts stated in SOURCE. Never invent customers, numbers, funding, awards or features.
- Line 1: a specific, concrete observation about what they built or who it's for (not generic praise).
- Line 2: a sharp, relevant thought that follows from line 1 (a trade-off, a likely challenge, or why it stood out).
- Line 3: a short, low-pressure question that invites a reply. If a PITCH is given, connect to it lightly; don't sell.
- Each line: one sentence, at most 20 words. Plain, specific, human tone, like a peer who actually read the site.
- Avoid filler: no "I hope this finds you well", "I came across", "unique challenges", "smart alternative", "balancing act", "impressive", no exclamation marks, no emojis.
- Do not mention their revenue, team size or anything not on the site.
- For each line give "evidence": a phrase copied EXACTLY (character for character) from SOURCE that the line relies on, 3-15 words. For line 3, evidence may be "" ONLY if it names no product, company, feature or fact.
- "founder": the founder's name only if SOURCE explicitly names a founder/CEO, else null. "founder_evidence": the exact phrase naming them, or "".
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "founder": {"type": "string", "nullable": True},
        "founder_evidence": {"type": "string"},
        "lines": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "evidence": {"type": "string"}},
                "required": ["text", "evidence"],
            },
        },
    },
    "required": ["founder", "founder_evidence", "lines"],
}


_CLIENT = None


def _client():
    """One shared client. (A throwaway client gets garbage-collected and the
    SDK closes its HTTP connection mid-request.)"""
    global _CLIENT
    if _CLIENT is None:
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise OpenerError("No GEMINI_API_KEY found. Add it to a .env file (see README).")
        from google import genai
        _CLIENT = genai.Client(api_key=key)
    return _CLIENT


def draft_opener(source: str, pitch: str = "", feedback: str = "", model: str = DEFAULT_MODEL) -> tuple[dict, dict]:
    """Returns (parsed_json, token_usage)."""
    from google.genai import types

    prompt = f"SOURCE:\n{source}\n\nPITCH: {pitch.strip() or '(none)'}"
    if feedback:
        prompt += f"\n\nYOUR PREVIOUS ATTEMPT FAILED THESE CHECKS, FIX THEM:\n{feedback}"
    from google.genai import errors

    try:
        resp = _client().models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                temperature=0.7,
                max_output_tokens=1024,
            ),
        )
    except errors.APIError as e:  # the SDK has already retried transient errors
        if e.code == 429:
            raise OpenerError("Gemini quota reached (429). Free-tier limits reset at midnight Pacific; try later.")
        if e.code in (500, 503, 504):
            raise OpenerError(f"Gemini is overloaded right now ({e.code}). Try again in a minute.")
        if e.code in (400, 401, 403):
            raise OpenerError(f"Gemini rejected the request ({e.code}). Check GEMINI_API_KEY and GEMINI_MODEL.")
        raise OpenerError(f"Gemini error {e.code}: {e.message}")
    try:
        data = json.loads(resp.text)
    except (TypeError, json.JSONDecodeError):
        raise OpenerError("The model returned something that wasn't valid JSON.")
    u = getattr(resp, "usage_metadata", None)
    usage = {
        "input_tokens": getattr(u, "prompt_token_count", None),
        "output_tokens": getattr(u, "candidates_token_count", None),
        "thinking_tokens": getattr(u, "thoughts_token_count", None),
    }
    return data, usage


# --------------------------------------------------------------------------- #
# 5. Verify (code)
# --------------------------------------------------------------------------- #

def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_draft(data: dict, source: str, pitch: str = "") -> list[str]:
    """Return a list of problems; empty means the draft passed."""
    problems: list[str] = []
    src = _norm(source)
    lines = data.get("lines") or []
    if len(lines) != 3:
        return [f"Expected exactly 3 lines, got {len(lines)}."]
    for i, line in enumerate(lines, 1):
        text = (line.get("text") or "").strip()
        ev = (line.get("evidence") or "").strip()
        if not text:
            problems.append(f"Line {i} is empty.")
        if len(text.split()) > MAX_WORDS_PER_LINE:
            problems.append(f"Line {i} is {len(text.split())} words; max is {MAX_WORDS_PER_LINE}.")
        if i < 3 and not ev:
            problems.append(f"Line {i} has no evidence quote.")
        if ev and len(ev.split()) < 3:
            problems.append(f'Line {i} evidence "{ev}" is too short to prove anything; quote at least 3 words.')
        if ev and _norm(ev) not in src:
            problems.append(f'Line {i} evidence not found on the site: "{ev}"')
        low = _norm(text)
        for phrase in BANNED_PHRASES:
            if phrase in low:
                problems.append(f'Line {i} uses filler "{phrase}"; say something specific instead.')
        # Any capitalised name (after the first word) must appear on the site or in the pitch,
        # so line 3 can't slip in competitors, customers or tools the site never mentions.
        for m in re.finditer(r"\b[A-Z][\w.+-]*[A-Za-z0-9]", text):
            word = m.group()
            if m.start() == 0 or word == "I":
                continue
            if _norm(word) not in src and _norm(word) not in _norm(pitch):
                problems.append(f'Line {i} mentions "{word}", which isn\'t on the site.')
    founder = (data.get("founder") or "").strip()
    founder_ev = _norm(data.get("founder_evidence") or "")
    if founder and (not founder_ev or founder_ev not in src or _norm(founder) not in founder_ev):
        problems.append(f'Founder name "{founder}" isn\'t backed by an exact quote from the site.')
    return problems


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

_CACHE: dict[tuple[str, str, str], tuple[float, Result]] = {}
CACHE_TTL_S = 24 * 3600


def generate(raw_url: str, pitch: str = "", model: str = DEFAULT_MODEL, use_cache: bool = True) -> Result:
    url = normalize_url(raw_url)
    key = (url, pitch.strip(), model)
    if use_cache and key in _CACHE and time.time() - _CACHE[key][0] < CACHE_TTL_S:
        return _CACHE[key][1]

    pages = fetch_site(url)
    source = build_source(pages)
    warnings: list[str] = []
    if len(source) < 400:
        warnings.append("Very little text found. The site may load its content with JavaScript, so the opener may be thin.")

    data, usage = draft_opener(source, pitch, model=model)
    problems = verify_draft(data, source, pitch)
    attempts = 1
    if problems:  # one retry, telling the model exactly what failed
        attempts = 2
        data2, usage2 = draft_opener(source, pitch, feedback="\n".join(problems), model=model)
        problems2 = verify_draft(data2, source, pitch)
        usage = {k: (usage.get(k) or 0) + (usage2.get(k) or 0) for k in usage}
        if len(problems2) <= len(problems):
            data, problems = data2, problems2
    warnings += [f"Check failed: {p}" for p in problems]

    founder = data.get("founder")
    if founder and any("Founder name" in p for p in problems):
        founder = None
    result = Result(
        url=url,
        lines=[l["text"].strip() for l in data["lines"]],
        evidence=[(l.get("evidence") or "").strip() for l in data["lines"]],
        founder=founder,
        warnings=warnings,
        pages=[p.url for p in pages],
        model=model,
        attempts=attempts,
        usage=usage,
    )
    if use_cache and not problems:
        _CACHE[key] = (time.time(), result)
    return result
