"""Web tools: fetch a page as text, and web search.

Search runs a chain of keyless, server-rendered engines and takes the first
answer whose hits actually relate to the query:

* duckduckgo (`html.duckduckgo.com/html/`) -- no JS, ~10 parsed hits. Needs a
  browser-ish User-Agent: with Fungi's plain UA the host answers with a 14 KB
  stub page, a Chrome UA gets the full 43 KB page (measured 2026-09-28). Only
  reachable through a proxy from mainland networks, so it runs first only when
  `_proxies()` resolves one.
* bing -- works without a proxy (that is why it was the only engine), but it
  throttles: after a few dozen queries from one address every query, even
  `vLLM`, comes back as Bing's "There are no results" page. It has also served
  a decoy page whose hits belonged to a different query wholesale -- silent
  wrong answers, hence `_relevant`.
* brave -- last resort, mostly HTTP 429 from shared proxies.

HTTP(S) proxies: urllib only honours environment proxies, and a stray NO_PROXY
env var makes getproxies() skip the Windows system (registry) proxy entirely.
_fetch therefore merges the registry proxy back in when the environment has
no real proxy configured, so a running system proxy (e.g. Clash) is used.
"""

import base64
import contextlib
import html as html_mod
import re
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, unquote, urlparse

WEB_TIMEOUT = 15
SEARCH_TIMEOUT = 10
WEB_TRUNCATE = 12000
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
# duckduckgo serves a stub page to anything that does not look like a browser.
SEARCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
# An empty page from an engine is a retryable answer, not an empty result set:
# bing hands out its "no results" page under load (2026-09-28).
SEARCH_ATTEMPTS = 2
SEARCH_RETRY_PAUSE = 0.5
SEARCH_HITS = 8
# Short words carry no evidence that a result page belongs to the query.
NOISE_WORDS = {"the", "and", "for", "with", "how", "what", "does", "that", "from", "into", "about"}


def _strip_html(markup: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>", "", markup)
    text = re.sub(r"(?is)<style.*?</style>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\n\s*\n\s*\n", "\n\n", text)
    text = re.sub(r"(?m)^[ \t]+", "", text)
    return text.strip()


def _truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n... [truncated {len(text) - limit} chars] ...\n\n{text[-half:]}"


def _proxies() -> dict:
    proxies = {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https")}
    registry = getattr(urllib.request, "getproxies_registry", None)
    if registry and not proxies:
        with contextlib.suppress(Exception):
            proxies = {k: v for k, v in registry().items() if k in ("http", "https")}
    return proxies


def _fetch(url: str, timeout: int, ua: str = USER_AGENT) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": ua})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(_proxies()))
    with opener.open(request, timeout=timeout) as resp:
        raw = resp.read()
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _unbing(url: str) -> str:
    """bing.com/ck/a?...&u=a1<base64url> tracking links -> real target URL."""
    m = re.search(r"[?&]u=a1([A-Za-z0-9_-]+)", url)
    if not m:
        return url
    raw = m.group(1)
    with contextlib.suppress(Exception):
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    return url


def tool_web(url: str) -> str:
    if not re.match(r"^https?://", url):
        url = f"https://{url}"
    try:
        text = _strip_html(_fetch(url, WEB_TIMEOUT))
    except urllib.error.HTTPError as exc:
        return f"ERROR: Web fetch failed: HTTP {exc.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        return f"ERROR: Web fetch failed: {reason}"
    return _truncate_middle(text, WEB_TRUNCATE)


def _inline(markup: str) -> str:
    """One-line text out of an HTML fragment."""
    return html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", markup))).strip()


def _ddg_target(href: str) -> str:
    """duckduckgo.com/l/?uddg=<urlencoded target> -> the target itself."""
    if href.startswith("//"):
        href = f"https:{href}"
    target = parse_qs(urlparse(href).query).get("uddg", [""])[0]
    return unquote(target) or href


def _search_ddg(query: str) -> str:
    """Parsed duckduckgo results: '1. Title\\n   URL\\n   snippet' per hit."""
    markup = _fetch(
        f"https://html.duckduckgo.com/html/?q={quote(query)}", SEARCH_TIMEOUT, ua=SEARCH_UA
    )
    lines: list[str] = []
    # Split on the title anchors instead of the result containers: the container
    # class changes (result / web-result / result--ad) but the anchors do not.
    for chunk in re.split(r'(?is)class="result__a"', markup)[1:]:
        anchor = re.match(r'(?is)[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', chunk)
        if not anchor:
            continue
        href = html_mod.unescape(anchor.group(1))
        if "y.js" in href or "ad_domain" in href:  # sponsored rows
            continue
        snippet = re.search(r'(?is)class="result__snippet"[^>]*>(.*?)</a>', chunk)
        lines.append(
            f"{len(lines) + 1}. {_inline(anchor.group(2))}\n   {_ddg_target(href)}"
            + (f"\n   {_inline(snippet.group(1))}" if snippet else "")
        )
        if len(lines) == SEARCH_HITS:
            break
    return "\n\n".join(lines)


def _search_bing(query: str) -> str:
    """Parsed Bing results: '1. Title\\n   URL\\n   snippet' per hit."""
    markup = _fetch(f"https://www.bing.com/search?q={quote(query)}&count=10", SEARCH_TIMEOUT)
    blocks = re.findall(r'(?is)<li[^>]*class="b_algo[^"]*"[^>]*>.*?</li>', markup)
    lines: list[str] = []
    for block in blocks[:SEARCH_HITS]:
        m = re.search(r'(?is)<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m:
            continue
        url = _unbing(html_mod.unescape(m.group(1)))
        title = html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        sn = re.search(r"(?is)<p[^>]*>(.*?)</p>", block)
        snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", sn.group(1))) if sn else ""
        snippet = re.sub(r"\s+", " ", snippet).strip()
        lines.append(
            f"{len(lines) + 1}. {title}\n   {url}" + (f"\n   {snippet}" if snippet else "")
        )
    return "\n\n".join(lines)


def _search_brave(query: str) -> str:
    """Fallback: full stripped Brave results page (works on some networks)."""
    text = _strip_html(_fetch(f"https://search.brave.com/search?q={quote(query)}", SEARCH_TIMEOUT))
    return _truncate_middle(text, WEB_TRUNCATE) if len(text) >= 50 else ""


def _relevant(query: str, results: str) -> bool:
    """Does this result page belong to the query at all?

    Bing served a page of US high-school links for an AI-agents query
    (2026-09-28). Fabricated-looking hits are worse than no hits, so a page
    earns trust only by containing a content word of the query.
    """
    tokens = [
        word
        for word in re.split(r"[^\w]+", query.lower())
        if len(word) >= 4 and word not in NOISE_WORDS
    ]
    if not tokens:
        return True
    low = results.lower()
    return any(word in low for word in tokens)


def tool_web_search(query: str) -> str:
    engines = {"duckduckgo": _search_ddg, "bing": _search_bing, "brave": _search_brave}
    # Only duckduckgo needs the proxy; without one it just burns the timeout.
    order = ("duckduckgo", "bing", "brave") if _proxies() else ("bing", "duckduckgo", "brave")

    failures: list[str] = []
    empty = 0
    for name in order:
        engine = engines[name]
        for attempt in range(SEARCH_ATTEMPTS):
            try:
                results = engine(query)
            except urllib.error.HTTPError as exc:
                failures.append(f"{name} HTTP {exc.code}")
                break
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                failures.append(f"{name} {getattr(exc, 'reason', exc)}")
                break
            if results and _relevant(query, results):
                return results
            if results:
                failures.append(f"{name} returned unrelated hits")
                break
            empty += 1
            if attempt + 1 < SEARCH_ATTEMPTS:
                time.sleep(SEARCH_RETRY_PAUSE)
    if failures:
        detail = "; ".join(dict.fromkeys(failures))
        return f"ERROR: Search failed ({detail[:200]})"
    if empty:
        return f"(no results for '{query}')"
    return "ERROR: Search failed (no engine configured)"
