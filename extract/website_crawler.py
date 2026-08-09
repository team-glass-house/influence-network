from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html import unescape
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import requests
from lxml import etree, html

CRAWLER_VERSION = "website-crawler-v1"
WORD_RE = re.compile(r"\b[\w’'-]+\b", re.UNICODE)
PLACEHOLDERS = {
    "",
    "na",
    "n/a",
    "n\\a",
    "n a",
    "none",
    "not applicable",
    "notapplicable",
    "not available",
    "notavailable",
}
NON_HTML_EXTENSIONS = (
    ".7z",
    ".avi",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".png",
    ".pdf",
    ".rar",
    ".svg",
    ".wav",
    ".webp",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
)


@dataclass(frozen=True)
class CrawlConfig:
    max_pages: int = 10
    max_words: int = 100_000
    timeout_seconds: float = 15.0
    delay_seconds: float = 0.25
    user_agent: str = "influence-network-transparency-index/1.0"

    def __post_init__(self) -> None:
        if self.max_pages < 1:
            raise ValueError("max_pages must be positive")
        if self.max_words < 1:
            raise ValueError("max_words must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")

    @property
    def policy_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @property
    def policy_hash(self) -> str:
        return hashlib.sha256(self.policy_json.encode()).hexdigest()[:16]


@dataclass
class CrawlResult:
    requested_url: str
    normalized_url: str
    final_url: str | None
    status: str
    http_status: int | None
    word_count: int | None
    capped_word_count: int | None
    pages_crawled: int
    page_urls: list[str]
    error: str | None
    robots_allowed: bool | None
    retrieved_at: str
    crawler_version: str = CRAWLER_VERSION


def normalize_url(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().strip("\"'()_").lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if (
        cleaned in PLACEHOLDERS
        or "@" in cleaned
        or "\\" in cleaned
        or cleaned.startswith("see ")
    ):
        return None
    scheme_match = re.match(
        r"^(https?|hhtp)(?=[:;./\s])[:;.\s]*/{0,2}\s*(.*)$", cleaned
    )
    if scheme_match:
        scheme = "http" if scheme_match.group(1) == "hhtp" else scheme_match.group(1)
        cleaned = f"{scheme}://{scheme_match.group(2)}"
    if any(char.isspace() for char in cleaned):
        return None
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    try:
        parts = urlsplit(cleaned)
        hostname = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    if hostname in {"http", "https"} or any(char.isspace() for char in parts.netloc):
        return None
    try:
        is_ip_address = bool(ipaddress.ip_address(hostname))
    except ValueError:
        is_ip_address = False
    labels = hostname.rstrip(".").split(".")
    if (
        not is_ip_address
        and len(labels) < 2
        or any(
            not label or label.startswith("-") or label.endswith("-")
            for label in labels
        )
    ):
        return None
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _same_site(candidate: str, root: str) -> bool:
    candidate_host = urlsplit(candidate).hostname
    root_host = urlsplit(root).hostname
    return bool(
        candidate_host
        and root_host
        and (candidate_host == root_host or candidate_host.endswith(f".{root_host}"))
    )


def _word_count(document: str) -> int:
    try:
        root = html.fromstring(document)
    except (ValueError, etree.ParserError):
        return 0
    for node in root.xpath("//script|//style|//noscript|//svg|//template"):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    return len(WORD_RE.findall(unescape(root.text_content())))


def _links(document: str, page_url: str, root_url: str) -> list[str]:
    try:
        root = html.fromstring(document)
    except (ValueError, etree.ParserError):
        return []
    links: list[str] = []
    for href in root.xpath("//a[@href]/@href"):
        href = href.strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue
        try:
            candidate, _ = urldefrag(urljoin(page_url, href))
        except ValueError:
            continue
        normalized = normalize_url(candidate)
        if normalized and _same_site(normalized, root_url):
            path = urlsplit(normalized).path.lower()
            if path.endswith(NON_HTML_EXTENSIONS):
                continue
            links.append(normalized)
    return list(dict.fromkeys(links))


def _robots_allowed(
    session: requests.Session, root_url: str, config: CrawlConfig
) -> tuple[bool, bool | None]:
    robots_url = urljoin(root_url, "/robots.txt")
    try:
        response = session.get(robots_url, timeout=config.timeout_seconds)
    except requests.RequestException:
        return True, None
    if response.status_code == 404:
        return True, True
    if not response.ok:
        return False, False
    parser = response.text
    from urllib.robotparser import RobotFileParser

    robot_parser = RobotFileParser()
    robot_parser.parse(parser.splitlines())
    return robot_parser.can_fetch(config.user_agent, root_url), True


def crawl_website(
    url: str,
    config: CrawlConfig | None = None,
    session: requests.Session | None = None,
) -> CrawlResult:
    config = config or CrawlConfig()
    normalized = normalize_url(url)
    retrieved_at = datetime.now(UTC).isoformat()
    if normalized is None:
        return CrawlResult(
            requested_url=url,
            normalized_url="",
            final_url=None,
            status="no_website",
            http_status=None,
            word_count=None,
            capped_word_count=None,
            pages_crawled=0,
            page_urls=[],
            error="invalid or missing URL",
            robots_allowed=None,
            retrieved_at=retrieved_at,
        )

    own_session = session is None
    session = session or requests.Session()
    session.headers.update({"User-Agent": config.user_agent})
    try:
        allowed, robots_seen = _robots_allowed(session, normalized, config)
        if not allowed:
            return CrawlResult(
                requested_url=url,
                normalized_url=normalized,
                final_url=None,
                status="blocked",
                http_status=None,
                word_count=None,
                capped_word_count=None,
                pages_crawled=0,
                page_urls=[],
                error="robots.txt disallowed crawling",
                robots_allowed=False,
                retrieved_at=retrieved_at,
            )

        queue = [normalized]
        queued = {normalized}
        page_urls: list[str] = []
        total_words = 0
        first_status: int | None = None
        final_url: str | None = None
        error: str | None = None
        while queue and len(page_urls) < config.max_pages:
            page_url = queue.pop(0)
            if page_urls and config.delay_seconds:
                time.sleep(config.delay_seconds)
            try:
                response = session.get(
                    page_url,
                    timeout=config.timeout_seconds,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                error = str(exc)
                continue
            if first_status is None:
                first_status = response.status_code
            final_url = response.url
            page_urls.append(page_url)
            if response.status_code >= 400:
                error = f"HTTP {response.status_code}"
                continue
            content_type = response.headers.get("content-type", "").lower()
            if content_type and "html" not in content_type:
                continue
            total_words += _word_count(response.text)
            if total_words >= config.max_words:
                total_words = config.max_words
                break
            for link in _links(response.text, response.url, normalized):
                if link not in queued:
                    queued.add(link)
                    queue.append(link)

        if not page_urls:
            return CrawlResult(
                requested_url=url,
                normalized_url=normalized,
                final_url=final_url,
                status="error",
                http_status=first_status,
                word_count=None,
                capped_word_count=None,
                pages_crawled=0,
                page_urls=[],
                error=error or "no pages fetched",
                robots_allowed=robots_seen,
                retrieved_at=retrieved_at,
            )
        status = "partial" if error else "success"
        return CrawlResult(
            requested_url=url,
            normalized_url=normalized,
            final_url=final_url,
            status=status,
            http_status=first_status,
            word_count=total_words,
            capped_word_count=min(total_words, config.max_words),
            pages_crawled=len(page_urls),
            page_urls=page_urls,
            error=error,
            robots_allowed=robots_seen,
            retrieved_at=retrieved_at,
        )
    finally:
        if own_session:
            session.close()
