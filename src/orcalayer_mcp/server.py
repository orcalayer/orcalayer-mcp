"""OrcaLayer MCP server.

A thin Model Context Protocol wrapper over the ``orcalayer`` Python SDK. It
does not reimplement any API logic — every tool delegates to the SDK and
shapes the result for an LLM agent.

Public tools (``leaderboard``, ``wallet_overview``, ``wallet_positions``,
``markets``, ``market_consensus``) work anonymously. ``whale_alerts`` is
Premium and needs an API key.

Two transports (0.4.0):

* stdio (default): ``orcalayer-mcp`` or ``python -m orcalayer_mcp``, for
  Claude Desktop and other local clients. The Premium key comes from the
  ``ORCALAYER_API_KEY`` environment variable.
* Streamable HTTP: ``orcalayer-mcp --http [--host H] [--port P] [--path /mcp]``,
  for the hosted server (https://orcalayer.com/mcp) and self-hosting behind a
  reverse proxy. The Premium key is read per request from the
  ``Authorization: Bearer <key>`` or ``X-API-Key`` header, so one process
  serves many users and never holds a key of its own. Requests without a
  header are anonymous (public tools only).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import threading
import urllib.parse
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from orcalayer import (
    AuthenticationError,
    OrcaLayer,
    OrcaLayerError,
    WalletComputingError,
)

mcp = FastMCP(
    "orcalayer",
    instructions=(
        "OrcaLayer provides read-only Polymarket smart-money analytics. Tools: a "
        "ranking of profitable traders (leaderboard), a wallet's profile and open "
        "positions (wallet_overview, wallet_positions), market search with Smart "
        "Money wallet counts (markets) and the Smart Money consensus on one market "
        "versus its price (market_consensus). Requests that carry an OrcaLayer "
        "Premium API key also get recent trades by Smart Money wallets "
        "(whale_alerts). Prompts hold ready-made analyses; resources hold the "
        "classification methodology, a glossary and the REST API reference."
    ),
)

try:
    _MCP_VERSION = _pkg_version("orcalayer-mcp")
except PackageNotFoundError:  # running from a source checkout without install
    _MCP_VERSION = "dev"

# serverInfo.version in the MCP initialize handshake. FastMCP does not take a
# version argument and the low-level Server falls back to the SDK's own
# package version, so clients and directories saw "1.29.0" (the mcp SDK) next
# to package 0.3.1 (seen in Glama's instance logs, 21.08.2026). Report ours.
mcp._mcp_server.version = _MCP_VERSION

# Every tool is a read-only lookup against the OrcaLayer API: nothing is
# created, changed or deleted, calls are safe to repeat, and the data comes
# from outside the client (openWorldHint). The directory review requires a
# title and a readOnlyHint on each tool, and Claude uses readOnlyHint to run
# the tool without a per-call confirmation.
def _read_only(title: str) -> ToolAnnotations:
    """Annotations for a read-only tool. 0.5.4: the title goes inside the
    annotations as well (annotations.title): the Anthropic directory portal reads
    it from there and flagged all six tools while only the tool-level title was set."""
    return ToolAnnotations(
        title=title, readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    )

# ── API clients ──────────────────────────────────────────────────────────────
# 0.4.0: the key is resolved per call, not per process. Over stdio it is the
# ORCALAYER_API_KEY environment variable, as before. Over HTTP it is the
# calling request's Authorization / X-API-Key header, so one hosted process
# serves many users with their own keys (or none). The SDK constructor is
# cheap — no network call, no key validation — so clients are built lazily
# and cached per key; a wrong key only surfaces when a Premium tool is called.
_ENV_API_KEY = os.environ.get("ORCALAYER_API_KEY") or None
_UA_SUFFIX = f"orcalayer-mcp/{_MCP_VERSION}"
_clients: dict[tuple[str | None, float | None], OrcaLayer] = {}
_clients_lock = threading.Lock()
_CLIENT_CACHE_MAX = 256

# 0.4.1: where ANONYMOUS calls go. Unset (every local install): the SDK's
# default, https://orcalayer.com. On the hosted server it is the backend on
# the same machine (http://127.0.0.1:8000), so the anonymous traffic of every
# connector user does not share one public-IP rate-limit bucket at the API;
# the per-client limits live in the reverse proxy in front of /mcp instead.
# Calls that carry a key are unaffected and keep going through the public
# host, where the key's own limit and usage journal apply. The backend treats
# localhost as a trusted caller, so the public tools must keep calling public
# endpoints only, never Premium ones; whale_alerts goes out with its key.
_ANON_BASE_URL = os.environ.get("ORCALAYER_MCP_ANON_BASE_URL") or None


def _request_api_key() -> str | None:
    """The Premium key for the current tool call, or None for anonymous.

    HTTP transport: ``Authorization: Bearer <key>`` or ``X-API-Key: <key>`` on
    the request that carried this call. Never read from the query string.
    Otherwise (stdio, or an HTTP request without a header) the environment
    variable, which the hosted server simply does not set.
    """
    request = None
    try:
        request = mcp.get_context().request_context.request
    except (ValueError, LookupError, AttributeError):
        request = None  # no active MCP request (stdio handshake, tests)
    if request is not None:
        headers = getattr(request, "headers", None) or {}
        auth = (headers.get("authorization") or "").strip()
        if auth[:7].lower() == "bearer ":
            key = auth[7:].strip()
            if key:
                return key
        key = (headers.get("x-api-key") or "").strip()
        if key:
            return key
    return _ENV_API_KEY


def _client_for(key: str | None, timeout: float | None = None) -> OrcaLayer:
    """SDK client for a key (None = anonymous); ``timeout`` overrides the SDK's
    30 s default for lookups that must fail fast (0.5.6, market_consensus)."""
    cache_key = (key, timeout)
    with _clients_lock:
        client = _clients.get(cache_key)
        if client is None:
            if len(_clients) >= _CLIENT_CACHE_MAX:
                # Keys in flight are few; a rare full reset beats an LRU here.
                _clients.clear()
            kwargs: dict[str, Any] = {"api_key": key, "user_agent_suffix": _UA_SUFFIX}
            if key is None and _ANON_BASE_URL:
                kwargs["base_url"] = _ANON_BASE_URL
            if timeout is not None:
                kwargs["timeout"] = timeout
            client = OrcaLayer(**kwargs)
            _clients[cache_key] = client
        return client


def _client() -> OrcaLayer:
    """The SDK client for the current call (anonymous or keyed)."""
    return _client_for(_request_api_key())


# ── field shaping (0.5.0) ────────────────────────────────────────────────────
# The REST API serves several bases under one name: the leaderboard's
# `win_rate` / `profit_factor` and the wallet overview's are computed
# differently (checked on 18 wallets 06.10.2026: they never matched), while
# `market_win_rate` agrees between the two everywhere. A model comparing two
# tools must not meet two different numbers under the same label, so the tools
# expose one explicitly named field per concept and a short `notes` block.

_SMART_SET = (
    "OrcaLayer's Smart Money set: wallets that pass the quality test in the orcalayer://methodology "
    "resource (about 208K of ~1.4M tracked wallets). It is a quality filter, not a size filter"
)


def _num(value: Any) -> Any:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _trade_date(ts: Any) -> str | None:
    """ISO date (UTC) of a trade timestamp."""
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


def _resolved(wins: Any, losses: Any) -> int | None:
    w, lo = _num(wins), _num(losses)
    return int(w) + int(lo) if w is not None and lo is not None else None


_NOTE_MARKET_WIN_RATE = (
    "market_win_rate: share of resolved markets the wallet won, one market = one result "
    "(market_wins / market_losses over resolved_markets). Same basis and value in leaderboard and wallet_overview."
)
_NOTE_PNL = (
    "total_pnl: the wallet's lifetime profit and loss in USD as Polymarket reports it "
    "(OrcaLayer's own reconstruction when Polymarket's figure is not synced)."
)
_NOTE_VOLUME = (
    "indexed_volume_usd / indexed_trades: the wallet's fills in OrcaLayer's on-chain index. Not Polymarket's volume "
    "figure: winning shares redeem at $1 without a trade, so P&L can exceed this volume, and fills before "
    "October 2025 are only partly indexed, so for older wallets it can be well below Polymarket's number."
)
_NOTE_TRADE_DATES = (
    "last_trade / first_trade: UTC dates of the wallet's fills in OrcaLayer's index. Fills before October 2025 are "
    "only partly indexed, so for older wallets first_trade can be later than their real first Polymarket trade."
)


# ── error handling ───────────────────────────────────────────────────────────

def _scrub(message: str) -> str:
    """Defensively strip the caller's API key from any text before it leaves
    the server.

    The SDK's own exception messages never carry the key, but this guarantees
    it can never leak through an error string even if that changes upstream.
    """
    key = _request_api_key()
    if key:
        return message.replace(key, "***")
    return message


def _real_failure(exc: OrcaLayerError) -> ToolError:
    """Map a genuine SDK failure (429/5xx/bad address/...) to a tool error.

    Raising the returned ``ToolError`` makes FastMCP return an ``isError``
    tool result carrying this text, so the agent sees a real failure it can
    retry or report (e.g. a 429 with its Retry-After hint) — not a masked
    "internal error". The API key is never included in the text, and on the
    hosted server the internal API address is shown as the public one (0.5.6).
    """
    message = _scrub(str(exc))
    if _ANON_BASE_URL:
        message = message.replace(_ANON_BASE_URL.rstrip("/"), "https://orcalayer.com")
    return ToolError(message)


# ── input checks (0.5.6) ─────────────────────────────────────────────────────
# The directory review asks for actionable errors on invalid input instead of
# silently accepted values. Found on 07.10.2026: an unknown category was
# dropped by the leaderboard (all wallets came back) and matched nothing in
# markets / whale_alerts; "Tech/AI", the name these docstrings use, reached the
# leaderboard and the alerts as "TECH/AI", which they do not know (they use
# "TECH"); "0x123" and empty addresses returned empty profiles or a bare 404.

_CATEGORY_CODES = {
    "crypto": "CRYPTO",
    "politics": "POLITICS",
    "sports": "SPORTS",
    "geopolitics": "GEOPOLITICS",
    "economics": "ECONOMICS",
    "tech/ai": "TECH",
    "tech": "TECH",
    "ai": "TECH",
}
_CATEGORY_NAMES = '"Crypto", "Politics", "Sports", "Geopolitics", "Economics", "Tech/AI"'


def _category(value: str | None) -> str | None:
    """API category code for a category name (case-insensitive); None = all."""
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("", "all"):
        return None
    code = _CATEGORY_CODES.get(v)
    if code is None:
        raise ToolError(f"Unknown category {value!r}. Accepted categories: {_CATEGORY_NAMES}; none = all.")
    return code


_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")


def _wallet_ref(address: str | int) -> str:
    """A 0x address (format-checked) or a username, stripped."""
    a = str(address).strip()
    if not a:
        raise ToolError(
            "address is empty. Accepted: a wallet address (0x followed by 40 hexadecimal characters) "
            "or a Polymarket username."
        )
    if a[:2].lower() == "0x" and not _ADDRESS_RE.fullmatch(a):
        raise ToolError(
            f"{a!r} is not a valid wallet address: a Polygon address is 0x followed by 40 hexadecimal characters."
        )
    return a


_MARKET_ID_RE = re.compile(r"\d+|0x[0-9a-fA-F]{64}")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*[a-z0-9]")
# A known market resolves in ~1 s even cold; an unknown one sends the API into
# a slow fallback (30-60 s, 07.10.2026), so a reference that search did not
# resolve gets a short deadline and a plain "not found".
_MARKET_LOOKUP_TIMEOUT = 10.0
_MARKET_ACCEPTED = (
    "Accepted: a market id (e.g. 559652), a Polymarket market or event slug, a polymarket.com URL, "
    "or a 0x condition id. The markets tool searches markets by topic."
)


def _market_ref(market: str | int) -> str:
    """The id / slug / condition id inside a market reference or a polymarket.com URL."""
    m = str(market).strip()
    low = m.lower()
    if low.startswith(("http://", "https://")) or low.startswith(("polymarket.com/", "www.polymarket.com/")):
        parsed = urllib.parse.urlparse(m if "://" in m else "https://" + m)
        parts = [p for p in parsed.path.split("/") if p]
        # polymarket.com/event/<event>[/<market>] and /market/<slug>: the last segment.
        m = parts[-1] if parts else ""
    if m[:2].lower() == "0x" or m.isdigit():
        return m
    return m.lower()


# ── public tools ─────────────────────────────────────────────────────────────

@mcp.tool(title="Smart-money leaderboard", annotations=_read_only("Smart-money leaderboard"))
def leaderboard(
    sort: Literal["pnl", "win_rate", "volume", "trades"] = "pnl",
    category: str | None = None,
    filter: Literal["smart", "all"] = "smart",
    limit: int = 20,
) -> dict:
    """Rank Polymarket traders from OrcaLayer's Smart Money set (or all wallets).

    Returns the top traders by lifetime profit, win rate, volume or trade
    count, optionally within one market category. Per wallet:
    name, total_pnl (Polymarket's lifetime P&L), market_win_rate with
    market_wins / market_losses / resolved_markets, profit_factor (capped at
    99.99, with profit_factor_capped), indexed_volume_usd, indexed_trades,
    main category, average entry price, open positions and the last trade date.
    `total` is how many wallets match the filter. A `notes` block defines each
    field.

    Args:
        sort: Ranking key: "pnl" (default), "win_rate", "volume" or "trades".
            "win_rate" orders by the leaderboard's stored win rate, which is
            computed differently from the market_win_rate shown, so rows can
            look out of order by that column.
        category: Restrict to wallets mainly trading one category: "Crypto",
            "Politics", "Sports", "Geopolitics", "Economics" or "Tech/AI"
            (case-insensitive). None = all.
        filter: "smart" (OrcaLayer's Smart Money set, default) or "all".
        limit: How many wallets to return, 1–50 (larger values return 50).
    """
    limit = max(1, min(limit, 50))
    try:
        data = _client().leaderboard(
            sort=sort, category=_category(category), filter=filter, limit=limit
        )
    except OrcaLayerError as exc:
        raise _real_failure(exc)

    rows = []
    for w in data.get("whales", []) or []:
        pf = _num(w.get("profit_factor"))
        rows.append({
            "wallet": w.get("wallet"),
            "name": w.get("name"),
            "total_pnl": w.get("total_pnl"),
            "market_win_rate": w.get("market_win_rate"),
            "market_wins": w.get("market_wins"),
            "market_losses": w.get("market_losses"),
            "resolved_markets": _resolved(w.get("market_wins"), w.get("market_losses")),
            "profit_factor": pf,
            "profit_factor_capped": bool(pf is not None and pf >= 99.99),
            "indexed_volume_usd": w.get("total_volume"),
            "indexed_trades": w.get("total_trades"),
            "main_category": w.get("main_category"),
            "avg_entry_price": w.get("avg_entry_price"),
            "open_positions": w.get("active_count"),
            "last_trade": _trade_date(w.get("last_trade_ts")),
            "skill_badge": w.get("skill_badge"),
        })
    return {
        "wallets": rows,
        "total": data.get("total"),
        "sort": data.get("sort", sort),
        "filter": filter,
        "notes": {
            "total": (
                "How many wallets match the filter. filter='smart' means " + _SMART_SET + "."
            ),
            "market_win_rate": _NOTE_MARKET_WIN_RATE,
            "profit_factor": (
                "profit_factor: the leaderboard's gross profit / gross loss over closed positions, capped at 99.99; "
                "profit_factor_capped = true means almost no recorded losses. wallet_overview reports a different, "
                "uncapped net profit factor from OrcaLayer's own P&L reconstruction."
            ),
            "total_pnl": _NOTE_PNL,
            "indexed_volume_usd": _NOTE_VOLUME,
            "last_trade": _NOTE_TRADE_DATES,
            "skill_badge": "skill_badge: stricter weekly mark inside Smart Money (orcalayer.com/methodology#skill-badge).",
        },
    }


@mcp.tool(title="Wallet overview", annotations=_read_only("Wallet overview"))
def wallet_overview(address: str | int) -> dict:
    """Summarize one wallet's trading profile and performance.

    Wallet profit tracking for any Polymarket address: lifetime P&L
    (Polymarket's figure), market_win_rate over resolved markets, a net
    profit factor, indexed volume and activity, category mix and leaderboard
    rankings. Accepts a 0x wallet address or an OrcaLayer nickname. Returns a
    compact summary rather than the full raw record, so it stays readable for
    heavy wallets. A `notes` block defines each field; market_win_rate has
    the same basis and value as in the leaderboard tool.

    While the wallet's stats are still being computed server-side, the result
    is a ``{"status": "computing", "retry_after_seconds": N}`` notice instead of
    data; N is the number of seconds after which the stats are expected to be
    ready.

    Args:
        address: 0x wallet address or OrcaLayer nickname.
    """
    ref = _wallet_ref(address)
    try:
        # poll=False keeps the call non-blocking: a cold heavy wallet raises
        # WalletComputingError at once (no ~60s SDK sleep) so we can surface a
        # "computing, retry later" notice instead of hitting the client timeout.
        data = _client().wallet_overview(ref, poll=False)
    except WalletComputingError as exc:
        # Cache miss on a heavy wallet: stats are not ready yet. Surface an
        # actionable "try again" notice — this is not a failure, so we return
        # rather than raise.
        return {
            "status": "computing",
            "retry_after_seconds": round(exc.retry_after),
            "message": (
                f"This wallet's stats are still being computed. "
                f"Call wallet_overview again in about {round(exc.retry_after)}s."
            ),
        }
    except OrcaLayerError as exc:
        raise _real_failure(exc)

    profile = data.get("profile", {}) or {}
    overview = data.get("overview", {}) or {}
    stats = data.get("stats", {}) or {}
    if not (profile.get("address") or profile.get("proxy_wallet")):
        # 0.5.6: an unknown username comes back as an all-null record.
        raise ToolError(
            f"No Polymarket wallet found for {ref!r}. Accepted: a wallet address (0x followed by 40 hexadecimal "
            "characters) or a Polymarket username."
        )

    # Rankings left the overview payload in a backend performance split
    # (12.05.2026: the overview always carries rankings = null). They live on
    # /wallet/{address}/rankings now, so fetch them here; a failure there must
    # not cost the caller the overview.
    rankings = None
    wallet_ref = profile.get("proxy_wallet") or profile.get("address") or ref
    try:
        raw = _client()._get(f"wallet/{urllib.parse.quote(str(wallet_ref))}/rankings", poll=False)
        r = (raw or {}).get("rankings") or None
        if r:
            rankings = {
                "rank_pnl": r.get("rank_pnl"),
                "rank_win_rate": r.get("rank_winrate"),
                "rank_volume": r.get("rank_volume"),
                "rank_trades": r.get("rank_trades"),
                "rank_profit_factor": r.get("rank_profit_factor"),
                "out_of": r.get("total_traders"),
            }
    except Exception:  # noqa: BLE001 — rankings are optional context
        rankings = None

    first_ts = overview.get("first_trade")
    last_ts = overview.get("last_trade")
    return {
        "profile": {
            "name": profile.get("name"),
            "pseudonym": profile.get("pseudonym"),
            "address": profile.get("address"),
            "proxy_wallet": profile.get("proxy_wallet"),
        },
        "performance": {
            "total_pnl": stats.get("total_pnl"),
            "unrealized_pnl": stats.get("unrealized_pnl"),
            "pnl_24h": stats.get("pnl_24h"),
            "pnl_7d": stats.get("pnl_7d"),
            "market_win_rate": stats.get("market_win_rate"),
            "market_wins": stats.get("market_wins"),
            "market_losses": stats.get("market_losses"),
            "resolved_markets": _resolved(stats.get("market_wins"), stats.get("market_losses")),
            "profit_factor": stats.get("profit_factor"),
            "is_smart": stats.get("is_smart"),
        },
        "activity": {
            "markets_traded": overview.get("total_markets"),
            "indexed_trades": overview.get("total_trades"),
            "indexed_volume_usd": overview.get("total_volume"),
            "open_positions": overview.get("active_count"),
            "closed_positions": overview.get("closed_count"),
            "median_hold_days": overview.get("median_hold_days"),
            "first_trade": _trade_date(first_ts),
            "last_trade": _trade_date(last_ts),
        },
        # Volume share per market category (e.g. {"CRYPTO": 86.1, ...}).
        "categories": data.get("categories"),
        "rankings": rankings,
        # True when heavy side-stats timed out: core stats above are still
        # valid, some derived fields may be missing.
        "degraded": data.get("degraded", False),
        "as_of": data.get("as_of"),
        "notes": {
            "total_pnl": _NOTE_PNL,
            "market_win_rate": _NOTE_MARKET_WIN_RATE,
            "profit_factor": (
                "profit_factor: net profit factor from OrcaLayer's FIFO P&L reconstruction, not capped. The leaderboard "
                "tool shows a different figure (gross over closed positions, capped at 99.99)."
            ),
            "is_smart": (
                "is_smart: the wallet is in " + _SMART_SET + ". The test uses OrcaLayer's stored win rate, which "
                "is computed differently from market_win_rate, so a Smart Money wallet can show a market_win_rate "
                "below 55%."
            ),
            "indexed_volume_usd": _NOTE_VOLUME,
            "closed_positions": (
                "closed_positions counts positions, open_positions currently held ones; one market can hold "
                "several positions, so closed_positions can exceed resolved_markets."
            ),
            "median_hold_days": (
                "median_hold_days: median days a position was held, over positions closed in the last 90 days; "
                "null when the wallet closed none in that window."
            ),
            "trade_dates": _NOTE_TRADE_DATES,
            "rankings": "rankings: position among `out_of` tracked wallets, 1 = best; null when the wallet is not ranked.",
        },
    }


@mcp.tool(title="Wallet open positions", annotations=_read_only("Wallet open positions"))
def wallet_positions(address: str | int, limit: int = 15) -> dict:
    """List a wallet's largest open positions by current value.

    Accepts a 0x wallet address or an OrcaLayer nickname. Returns the
    positions with the largest current value first, in a compact form, plus a
    count of how many more are not shown.

    Args:
        address: 0x wallet address or OrcaLayer nickname.
        limit: How many positions to return (1–50). The wallet may hold more;
            the response reports how many were omitted.
    """
    capped = max(1, min(limit, 50))
    ref = _wallet_ref(address)
    try:
        # The API ignores the page limit and returns the wallet's full set
        # unordered, so we fetch all of them and do the top-N selection here.
        data = _client().wallet_positions(ref, limit=500)
    except OrcaLayerError as exc:
        raise _real_failure(exc)

    rows = data.get("positions", []) or []
    # The API does not guarantee ordering — sort by current value before
    # taking the top-N, otherwise "top" positions would be arbitrary.
    rows.sort(key=lambda p: p.get("current_value") or 0, reverse=True)
    top = rows[:capped]
    # `count` is the wallet's full open-position total and is independent of
    # the page size, so "more not shown" is honest.
    total = data.get("count")
    if not isinstance(total, int):
        total = len(rows)
    shown = [
        {
            "question": p.get("question"),
            "outcome": p.get("outcome"),
            "side": p.get("side"),
            "current_value": p.get("current_value"),
            "pnl": p.get("pnl"),
            "pnl_pct": p.get("pnl_pct"),
            "avg_entry": p.get("avg_entry"),
            "current_price": p.get("current_price"),
            "category": p.get("category"),
        }
        for p in top
    ]
    return {
        "positions": shown,
        "shown": len(shown),
        "total_open": total,
        "more_not_shown": max(0, total - len(shown)),
    }


@mcp.tool(title="Market search", annotations=_read_only("Market search"))
def markets(
    q: str | int = "",
    category: str | None = None,
    min_volume: float | None = None,
    min_whales: int | None = None,
    limit: int = 20,
) -> dict:
    """Search Polymarket markets, optionally where Smart Money wallets cluster.

    Finds markets by topic, slug or URL and shows how many wallets from
    OrcaLayer's Smart Money set hold each side right now. Returns each
    market's id, question, YES price, smart_wallets_yes / smart_wallets_no,
    volume, end date and days left. The counts are wallets, whatever their
    position size; popular markets have thousands. The dollar split on one
    market is in the market_consensus tool.

    Args:
        q: Free-text query; also accepts a Polymarket URL or slug. "" browses.
            Open markets only.
        category: One of "Crypto", "Politics", "Sports", "Geopolitics",
            "Economics", "Tech/AI" (case-insensitive). None = all.
        min_volume: Minimum market volume in USD.
        min_whales: Minimum number of Smart Money wallets holding a position
            in the market (either side).
        limit: How many markets to return, 1–50 (larger values return 50).
    """
    limit = max(1, min(limit, 50))
    category_code = _category(category)
    try:
        # 0.5.2: q, address and market accept numbers too (a numeric market id,
        # a nickname or search term made of digits): MCP Inspector and some
        # models send them as JSON numbers, which a str-only parameter rejected.
        data = _client().markets(
            str(q),
            category=category_code,
            min_volume=min_volume,
            min_whales=min_whales,
            limit=limit,
        )
    except OrcaLayerError as exc:
        raise _real_failure(exc)

    shown = [
        {
            "id": m.get("id"),
            "question": m.get("question"),
            "slug": m.get("slug"),
            "event_slug": m.get("event_slug"),
            "category": m.get("category"),
            "price_yes": m.get("price_yes"),
            "smart_wallets_yes": m.get("whales_yes"),
            "smart_wallets_no": m.get("whales_no"),
            "volume_usd": m.get("volume"),
            "end_date": m.get("end_date"),
            "days_left": m.get("days_left"),
        }
        for m in (data.get("markets", []) or [])
    ]
    return {
        "markets": shown,
        "total": data.get("total"),
        "notes": {
            "smart_wallets_yes": _NOTE_SMART_WALLETS,
            "volume_usd": _NOTE_MARKET_VOLUME,
        },
    }


_NOTE_SMART_WALLETS = (
    "smart_wallets_yes / smart_wallets_no: how many wallets from " + _SMART_SET
    + " hold YES / NO in this market, counted regardless of position size (a $5 holder counts like a "
    "$500K one). market_consensus gives the capital-weighted split."
)
_NOTE_MARKET_VOLUME = "volume_usd: the market's total traded volume in USD as Polymarket reports it."
_EVENT_MARKETS_SHOWN = 15


def _event_markets(event_slug: str, rows: list[dict]) -> dict:
    """market_consensus answer for an event reference: the event's markets (0.5.6)."""
    rows = sorted(rows, key=lambda r: _num(r.get("volume")) or 0, reverse=True)
    return {
        "event": event_slug,
        "markets_in_event": len(rows),
        "markets": [
            {
                "id": r.get("id"),
                "question": r.get("question"),
                "price_yes": r.get("price_yes"),
                "smart_wallets_yes": r.get("whales_yes"),
                "smart_wallets_no": r.get("whales_no"),
                "volume_usd": r.get("volume"),
                "end_date": r.get("end_date"),
            }
            for r in rows[:_EVENT_MARKETS_SHOWN]
        ],
        "notes": {
            "event": (
                f"{event_slug!r} is a Polymarket event that groups {len(rows)} open markets; the consensus is "
                f"computed per market, and each market's id here is a valid market reference for market_consensus. "
                f"Listed: up to {_EVENT_MARKETS_SHOWN} markets with the largest volume."
            ),
            "smart_wallets_yes": _NOTE_SMART_WALLETS,
            "volume_usd": _NOTE_MARKET_VOLUME,
        },
    }


@mcp.tool(title="Smart-money consensus on a market", annotations=_read_only("Smart-money consensus on a market"))
def market_consensus(market: str | int) -> dict:
    """Smart-money consensus on one Polymarket market versus its current price.

    Returns how Smart Money wallets are positioned on one market: how many
    hold YES and NO, how much capital each side has invested, the current
    market price, and the gap between that positioning and the price.

    Two consensus reads are returned side by side and can disagree:
    ``head_count`` (one Smart Money wallet = one vote; wallets from
    OrcaLayer's Smart Money set, so popular markets count thousands) and
    ``capital_weighted`` (dollars invested per side). In head_count a $5
    wallet weighs the same as a $500K one, so it is the weaker of the two
    reads; the capital split reflects the money at stake. On cheap longshots
    (YES under ~15 cents) head_count leans YES structurally. The divergence
    fields measure positioning against the price; they are not evidence that
    the price is wrong.

    An event slug or event URL (a Polymarket event groups several markets)
    returns the event's markets with their ids and Smart Money wallet counts
    instead, since the consensus is per market.

    Args:
        market: Market id, Polymarket market or event slug, polymarket.com
            URL, or 0x condition id.
    """
    ref = _market_ref(market)
    if not ref:
        raise ToolError("market is empty. " + _MARKET_ACCEPTED)
    if not (_MARKET_ID_RE.fullmatch(ref) or _SLUG_RE.fullmatch(ref)):
        raise ToolError(f"{str(market).strip()!r} is not a market reference. " + _MARKET_ACCEPTED)

    key = _request_api_key()
    if not _MARKET_ID_RE.fullmatch(ref):
        # 0.5.6: slugs go through market search first (~1 s). It knows open
        # markets by their own slug and by their event's slug; a miss (closed
        # market or unknown slug) falls through to the direct lookup below.
        try:
            found = (_client_for(key).markets(ref, limit=100) or {}).get("markets") or []
        except OrcaLayerError:
            found = []
        exact = [r for r in found if (r.get("slug") or "").lower() == ref]
        event = [r for r in found if (r.get("event_slug") or "").lower() == ref]
        if exact:
            ref = str(exact[0].get("id"))
        elif len(event) == 1:
            ref = str(event[0].get("id"))
        elif event:
            return _event_markets(ref, event)

    try:
        data = _client_for(key, timeout=_MARKET_LOOKUP_TIMEOUT).market(ref)
    except OrcaLayerError as exc:
        if isinstance(exc.__cause__, httpx.TimeoutException):
            raise ToolError(f"No Polymarket market found for {ref!r} (lookup timed out). " + _MARKET_ACCEPTED)
        raise _real_failure(exc)
    if not isinstance(data, dict) or data.get("error") or not (data.get("market") or {}).get("id"):
        raise ToolError(f"No Polymarket market found for {ref!r}. " + _MARKET_ACCEPTED)

    mkt = data.get("market", {}) or {}
    whales = data.get("whales", {}) or {}
    price_yes = mkt.get("price_yes")

    smart_yes = float(data.get("yes_team_total_invested") or 0)
    smart_no = float(data.get("no_team_total_invested") or 0)
    all_yes = float(data.get("yes_team_size_total_invested") or 0)
    all_no = float(data.get("no_team_size_total_invested") or 0)

    def _pct(a: float, b: float) -> float | None:
        return round(100.0 * a / (a + b), 1) if (a + b) > 0 else None

    head_yes_pct = whales.get("yes_pct")
    cap_yes_pct = _pct(smart_yes, smart_no)
    price_pct = round(price_yes * 100, 1) if price_yes is not None else None

    return {
        "market": {
            "id": mkt.get("id"),
            "question": mkt.get("question"),
            "slug": mkt.get("slug"),
            "condition_id": mkt.get("condition_id"),
            "price_yes": price_yes,
            "price_no": mkt.get("price_no"),
            "volume": mkt.get("volume"),
            "end_date": mkt.get("end_date"),
            "closed": bool(mkt.get("closed")),
        },
        "head_count": {
            "yes_whales": whales.get("yes"),
            "no_whales": whales.get("no"),
            "total": whales.get("total"),
            "yes_pct": head_yes_pct,
        },
        "capital_weighted": {
            "smart_yes_invested_usd": round(smart_yes, 2),
            "smart_no_invested_usd": round(smart_no, 2),
            "smart_yes_pct": cap_yes_pct,
            "all_whales_yes_invested_usd": round(all_yes, 2),
            "all_whales_no_invested_usd": round(all_no, 2),
            "all_whales_yes_pct": _pct(all_yes, all_no),
        },
        "divergence_pp": {
            "head_count_vs_price": (
                round(head_yes_pct - price_pct, 1)
                if head_yes_pct is not None and price_pct is not None
                else None
            ),
            "capital_vs_price": (
                round(cap_yes_pct - price_pct, 1)
                if cap_yes_pct is not None and price_pct is not None
                else None
            ),
        },
        "last_24h": data.get("smart_money_24h"),
        "caveats": (
            "head_count counts wallets from " + _SMART_SET + ", whatever their "
            "position size, so popular markets count thousands and a $5 wallet "
            "counts the same as a $500K one; the capital-weighted split reflects "
            "the money at stake and carries more information when the two "
            "disagree. On cheap longshots (YES < ~15c) head-count leans YES "
            "structurally. Divergence from price is positioning information, "
            "not evidence that the market is mispriced."
        ),
    }


# ── premium tool ─────────────────────────────────────────────────────────────

@mcp.tool(title="Whale trade alerts (Premium)", annotations=_read_only("Whale trade alerts (Premium)"))
def whale_alerts(
    minutes: int = 60,
    min_usd: float = 1000,
    category: str | None = None,
    limit: int = 25,
) -> Any:
    """Recent trades by Smart Money wallets: real-time alerts on profitable
    wallets (Premium).

    Returns trades by Smart Money wallets in the last ``minutes`` over
    ``min_usd`` in size: who traded, buy or sell, side, amount, price, the
    wallet's position after the trade, and the market. Covers profitable
    Polymarket wallets opening, adding to or closing positions.

    Needs an OrcaLayer Premium API key: on the hosted server it is the
    ``Authorization: Bearer <key>`` request header (the hosted server lists
    this tool only for requests that carry one), on the local stdio server the
    ORCALAYER_API_KEY environment variable. Without a key the result is a
    short notice on how to get one (no API call is made and it is not an
    error).

    Args:
        minutes: Lookback window in minutes, 1–1440 (24h); values outside
            the range are clamped to it.
        min_usd: Minimum trade size in USD.
        category: Restrict to one market category: "Crypto", "Politics",
            "Sports", "Geopolitics", "Economics" or "Tech/AI"
            (case-insensitive). None = all.
        limit: How many alerts to return, 1–50 (larger values return 50).
    """
    # Pre-check the key so the "needs Premium" path is an actionable message,
    # never a failure and never an API round-trip.
    key = _request_api_key()
    if not key:
        return (
            "whale_alerts is a Premium feature and needs an OrcaLayer API key. "
            "Get one at https://orcalayer.com/pricing. On the hosted server "
            "(https://orcalayer.com/mcp) send it as the request header "
            "`Authorization: Bearer <key>`; for the local stdio server set the "
            "ORCALAYER_API_KEY environment variable."
        )

    limit = max(1, min(limit, 50))
    minutes = max(1, min(minutes, 1440))
    min_usd = max(0.0, min_usd)
    category_code = _category(category)
    try:
        data = _client_for(key).whale_alerts(
            minutes=minutes, min_usd=min_usd, category=category_code, limit=limit
        )
    except AuthenticationError as exc:
        # Distinguish a rejected key (401) from a valid key on a non-Premium
        # plan (403). Both are real failures the agent should report.
        if exc.status_code == 403:
            raise ToolError(
                "Your API key is valid but your plan does not include Premium "
                "access. Upgrade at https://orcalayer.com/pricing."
            )
        raise ToolError(
            "Your API key was rejected (invalid or expired). Check it at "
            "https://orcalayer.com/settings."
        )
    except OrcaLayerError as exc:
        raise _real_failure(exc)

    # 0.5.3: same naming rules as the other tools. The whale's `win_rate` here
    # is the leaderboard's stored figure, not market_win_rate, so it is renamed;
    # each alert gets an ISO time next to its unix timestamp (live data, the
    # history-table date issue does not apply).
    if not isinstance(data, dict) or not isinstance(data.get("alerts"), list):
        return data
    alerts = []
    for a in data["alerts"]:
        if not isinstance(a, dict):
            alerts.append(a)
            continue
        a = dict(a)
        whale = a.get("whale")
        if isinstance(whale, dict) and "win_rate" in whale:
            whale = dict(whale)
            whale["leaderboard_win_rate"] = whale.pop("win_rate")
            a["whale"] = whale
        ts = _num(a.get("timestamp"))
        if ts:
            a["time_utc"] = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        alerts.append(a)
    return {
        "alerts": alerts,
        "count": data.get("count", len(alerts)),
        "params": data.get("params"),
        "notes": {
            "whale": (
                "whale.leaderboard_win_rate is the leaderboard's stored win rate, a different basis from "
                "market_win_rate in the leaderboard and wallet_overview tools; whale.total_pnl is the lifetime "
                "P&L as Polymarket reports it; whale.is_smart: the wallet is in " + _SMART_SET + "."
            ),
            "trade": (
                "trade.action BUY/SELL and side YES/NO of the fill, price per share (0-1), usd_amount; "
                "trade.position_now is the wallet's position on that market after the trade."
            ),
        },
    }


# ── tool list per caller (0.5.7) ─────────────────────────────────────────────
# Viktor 07.10.2026: on the hosted server a request without a key does not see
# the Premium tool. Claude's directory connectors carry no key header, so for
# them whale_alerts could only ever answer "needs a key"; the directory review
# expects every listed tool to work, and keyed access outside OAuth is not a
# directory auth mode. Clients that send a key (Claude Code, Cursor, MCP
# Inspector, custom connectors with headers) still list all six. The stdio
# package keeps listing it, with the notice, so local users can find Premium.
# A keyless call by name still returns the notice (the SDK only logs "not
# listed"; FastMCP registers call_tool without input validation).
_PREMIUM_TOOLS = frozenset({"whale_alerts"})
_HIDE_PREMIUM_WITHOUT_KEY = False  # main() sets it for --http


async def _list_tools_for_caller():
    tools = await mcp.list_tools()
    if _HIDE_PREMIUM_WITHOUT_KEY and not _request_api_key():
        tools = [t for t in tools if t.name not in _PREMIUM_TOOLS]
    return tools


mcp._mcp_server.list_tools()(_list_tools_for_caller)


# ── prompts ──────────────────────────────────────────────────────────────────
# Ready-made analyses an agent can run. Each returns an instruction template that
# drives the tools above; FastMCP derives the prompt arguments from the signature.

@mcp.prompt(description="Analyze a Polymarket wallet — smart money or farmer?")
def analyze_wallet(address: str) -> str:
    return (
        f"Analyze the Polymarket wallet {address}.\n"
        "1. Call wallet_overview to get its profile and performance.\n"
        "2. Call wallet_positions for its largest open positions.\n"
        "3. Judge whether it is smart money or shows farmer / hedger patterns — "
        "weigh market win rate, average entry price, the is_smart flag and profit "
        "factor.\n"
        "4. Conclude: is this a trustworthy directional signal, or noise?"
    )


@mcp.prompt(description="Find markets where smart money disagrees with the current price")
def find_divergence(category: str = "", min_pp: str = "") -> str:
    cat = f" in the {category} category" if category else ""
    pp = min_pp or "10"
    return (
        f"Find Polymarket markets{cat} where smart-money positioning diverges from "
        f"the current YES price by at least {pp} percentage points.\n"
        "1. Call markets (optionally with category and min_whales) to list markets "
        "with heavy smart-whale interest.\n"
        "2. For each, compare the YES price to the smart-whale YES/NO split.\n"
        "3. Surface the markets where the crowd price and the smart-money lean "
        "disagree most, and explain the disagreement."
    )


@mcp.prompt(description="Check if a wallet's recent profit was real alpha or a hedge structure")
def hedge_check(address: str, market_id: str = "") -> str:
    scope = f", focusing on market {market_id}" if market_id else ""
    return (
        f"Assess whether the recent profit of Polymarket wallet {address} is genuine "
        f"directional alpha or an artifact of hedging / market-making{scope}.\n"
        "1. Call wallet_overview for performance, profit factor and category mix.\n"
        "2. Call wallet_positions and look for paired opposite-side positions in the "
        "same or correlated markets (a hallmark of hedging).\n"
        "3. Consider whether wins come from a few resolved markets or broad "
        "consistency.\n"
        "4. Conclude: real edge, or a hedge / MM structure that won't transfer to "
        "copy-trading?"
    )


@mcp.prompt(description="Review Ukraine territorial markets with ISW frontline overlay")
def territorial_markets_review(threat_level: str = "") -> str:
    lvl = (
        f" Prioritise markets at {threat_level} proximity to the front line."
        if threat_level
        else ""
    )
    return (
        "Review the Polymarket Ukraine territorial-control markets against the ISW "
        "(Institute for the Study of War) frontline picture.\n"
        "1. Call markets with category=\"Geopolitics\" to list active Ukraine "
        "territory markets.\n"
        "2. For each, weigh the current YES price against the real frontline "
        "situation and smart-money positioning." + lvl + "\n"
        "3. Flag markets where the price looks mispriced versus the ground truth."
    )


# ── resources ────────────────────────────────────────────────────────────────
# Read-only context an agent can pull without a tool call. Hardcoded so a reader
# never depends on a live fetch; the canonical long-form lives on orcalayer.com.

_METHODOLOGY = """# OrcaLayer classification methodology

OrcaLayer reads every Polymarket trade directly from the Polygon blockchain
(1.5B+ on-chain fills) and classifies the wallets behind them.

## Smart money
A wallet is flagged **smart money** only when ALL of these hold:
- win rate >= 55% measured **by resolved markets** (not by individual trades),
- positive lifetime realized P&L,
- not flagged as a farmer or bot,
- at least 10 resolved markets of history.
About 208K of ~1.42M tracked wallets qualify.

## Farmer filter
A farmer's fingerprint is an average buy price above ~95c: such an average
cannot be accumulated by taking real uncertain positions, and it manufactures
"flawless" 95-100% win rates on near-decided markets. ~16% of high-P&L wallets
are farmers. Bots (very high trade counts at tiny average size) and protocol
router contracts are excluded separately.

## NegRisk correction
On linked multi-outcome (NegRisk) markets, resolved P&L is divided by 2 and
split/merge flows are attributed correctly, removing double-counting so win
rates stay honest.

## P&L method
Per-wallet, per-market FIFO (first-in, first-out), so profit reflects the real
sequence of entries and exits.

Full version: https://orcalayer.com/methodology
"""

_GLOSSARY = """# Prediction-markets glossary (OrcaLayer)

- **Smart money** — wallet with >=55% market win rate, positive P&L, not a
  farmer/bot, 10+ resolved markets. The signal OrcaLayer tracks.
- **Farmer** — wallet whose average buy price is above ~95c; inflates win rate by
  buying near-decided markets. Excluded from smart-money stats.
- **Hedger** — holds offsetting positions on both sides; apparent "profit" can be
  a structure rather than directional edge.
- **Market maker (MM)** — provides liquidity on both sides; P&L is spread capture,
  not a directional call.
- **NegRisk** — Polymarket's linked multi-outcome markets; resolved P&L is halved
  and split/merge flows attributed, so nothing is counted twice.
- **FIFO P&L** — first-in, first-out realized profit, per wallet per market.
- **Profit factor** — gross wins divided by gross losses.
- **Alignment / consensus** — how strongly smart money agrees on one side of a
  market.
- **ISW** — Institute for the Study of War; its daily frontline maps are the
  oracle Polymarket uses to resolve Ukraine territorial markets, surfaced in
  OrcaLayer's Territory monitor.
- **Resolved market** — a market that has settled to a final outcome; win rate is
  computed over these.
"""

_API_REFERENCE = """# OrcaLayer public REST API

Base: https://orcalayer.com
- Public read endpoints: /api/v2/* (no key)
- Premium endpoints: /api/public/v1/* (Bearer or x-api-key)
- Hosted MCP server: https://orcalayer.com/mcp (Streamable HTTP; the five
  public tools need no authentication; whale_alerts is listed for requests
  that carry a Premium key as the `Authorization: Bearer <key>` header)

## Auth
Send a Premium key as `Authorization: Bearer <key>` or `x-api-key: <key>`. Public
read endpoints (wallet profiles, market search, leaderboard) need no key.

## Rate limits
- Anonymous public: 200 requests/minute per IP (plus Cloudflare throttling).
- Premium API key: 600 requests/minute (sliding 60s window, no daily cap).
Every response carries X-RateLimit-* headers.

## Real-time feed
Server-Sent Events (SSE) at /api/public/v1/live/trades (Premium) streams each
smart-money trade as it lands — a long-lived EventSource connection, one JSON
event per trade. (It is SSE, not WebSocket.)

Since 2026-07-22 every event also carries `settlement_type`
("MINT" | "MERGE" | "COMPLEMENTARY" | null) — the settlement mechanics of the
CLOB match, derived live by transaction-grouping ~60-120s before the trade
appears on-chain (shadow-verified: 100.000% accurate on MINT/MERGE, coverage
91.8%). The label describes settlement mechanics, not trader intent: MINT
means the order matched an opposite-side buyer (no seller was in the book),
not that the wallet deliberately split collateral; ~80% of all fills settle as
MINT. The label is tx-level (all fills of one match share it); COMPLEMENTARY
means the tx contains a complementary component; null is an honest refusal
(lone fill at the buffer edge or a complex batch) and does not mean "not a
mint". A second stream, /api/public/v1/live/trades-indexed (Premium), delivers
per-fill `entry_type` from on-chain data 20-45s later with a server-side
?types=mint,merge filter; it trades speed for per-fill fidelity.

## Selected endpoints
- GET /api/v2/whales/leaderboard — ranked smart whales
- GET /api/v2/wallet/{address} — wallet profile + stats
- GET /api/v2/wallet/{address}/positions — open positions
- GET /api/v2/markets/search — market search
- GET /api/public/v1/whale-alerts — recent whale trades (Premium)

Full docs: https://orcalayer.com/docs/api
"""


@mcp.resource(
    "orcalayer://methodology",
    name="OrcaLayer Methodology",
    description="How smart money is filtered from farmers, hedgers and market-makers.",
    mime_type="text/markdown",
)
def methodology_resource() -> str:
    return _METHODOLOGY


@mcp.resource(
    "orcalayer://glossary",
    name="Prediction Markets Glossary",
    description="Terms used in Polymarket analytics: hedge, farmer, NegRisk, smart money, ISW.",
    mime_type="text/markdown",
)
def glossary_resource() -> str:
    return _GLOSSARY


@mcp.resource(
    "orcalayer://api-reference",
    name="OrcaLayer REST API Reference",
    description="Public endpoints, authentication, rate limits and the SSE live feed.",
    mime_type="text/markdown",
)
def api_reference_resource() -> str:
    return _API_REFERENCE


# ── entry point ──────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    """Console-script entry point.

    No arguments: serve stdio (Claude Desktop and other local clients).
    ``--http``: serve Streamable HTTP on ``--host``/``--port`` at ``--path``,
    stateless with plain JSON responses, for hosting behind a reverse proxy
    (the hosted server at https://orcalayer.com/mcp runs exactly this).
    """
    parser = argparse.ArgumentParser(
        prog="orcalayer-mcp",
        description="OrcaLayer MCP server: Polymarket smart-money analytics for MCP clients.",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve Streamable HTTP instead of stdio (env ORCALAYER_MCP_HTTP=1 does the same)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("ORCALAYER_MCP_HOST", "127.0.0.1"),
        help="bind address for --http (default 127.0.0.1: put a reverse proxy in front)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ORCALAYER_MCP_PORT", "8020")),
        help="port for --http (default 8020)",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("ORCALAYER_MCP_PATH", "/mcp"),
        help="URL path of the MCP endpoint for --http (default /mcp)",
    )
    args = parser.parse_args(argv)

    if not args.http and os.environ.get("ORCALAYER_MCP_HTTP", "") not in ("1", "true", "yes"):
        mcp.run()
        return

    global _HIDE_PREMIUM_WITHOUT_KEY
    _HIDE_PREMIUM_WITHOUT_KEY = True

    settings = mcp.settings
    settings.host = args.host
    settings.port = args.port
    settings.streamable_http_path = args.path
    # Stateless: every request is self-contained, so there are no sticky
    # sessions to lose on a restart and any number of processes could share
    # the load. JSON responses instead of SSE streams: nothing long-lived to
    # hold open through Cloudflare and nginx; tools answer in well under a
    # second anyway.
    settings.stateless_http = True
    settings.json_response = True
    # The SDK's default for a 127.0.0.1 bind only accepts "Host: localhost",
    # which would reject every request forwarded by the reverse proxy with the
    # public host name. Host and Origin policy is the proxy's job here.
    settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    # Data minimisation: httpx logs every outgoing API URL at INFO, i.e. each
    # tool call's parameters (wallet addresses, search terms) would land in the
    # host's system journal on every request. The API keeps its own request log
    # with a fixed retention; the MCP process does not need a second copy.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
