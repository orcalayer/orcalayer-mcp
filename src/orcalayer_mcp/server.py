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
import threading
import urllib.parse
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Any

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
        "OrcaLayer exposes Polymarket smart-money analytics. Tools: rank profitable "
        "whales (leaderboard), inspect a wallet's profile and positions "
        "(wallet_overview, wallet_positions), search markets where smart money is "
        "clustering (markets), read the smart-money consensus on one market versus "
        "its price (market_consensus), and stream recent whale trades "
        "(whale_alerts, Premium). Prompts give ready-made analyses; resources hold "
        "the classification methodology, a glossary and the REST API reference."
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
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
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
_clients: dict[str | None, OrcaLayer] = {}
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


def _client_for(key: str | None) -> OrcaLayer:
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            if len(_clients) >= _CLIENT_CACHE_MAX:
                # Keys in flight are few; a rare full reset beats an LRU here.
                _clients.clear()
            kwargs: dict[str, Any] = {"api_key": key, "user_agent_suffix": _UA_SUFFIX}
            if key is None and _ANON_BASE_URL:
                kwargs["base_url"] = _ANON_BASE_URL
            client = OrcaLayer(**kwargs)
            _clients[key] = client
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

# Fills before this date sit in OrcaLayer's history table, where part of the
# 2024 rows carry a wrong time (~11 months early; block numbers are right,
# found 06.10.2026, correction pending). Until it is corrected, first/last
# trade dates before the cutoff are not shown as exact dates.
_TRUSTED_TRADE_TIME_FROM = int(datetime(2025, 10, 1, tzinfo=timezone.utc).timestamp())

_SMART_SET = (
    "OrcaLayer's Smart Money set: wallets that pass the quality test in the orcalayer://methodology "
    "resource (about 208K of ~1.4M tracked wallets). It is a quality filter, not a size filter"
)


def _num(value: Any) -> Any:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _trade_date(ts: Any) -> str | None:
    """ISO date of a trade timestamp, or a plain statement when it cannot be trusted."""
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    if t < _TRUSTED_TRADE_TIME_FROM:
        return "before 2025-10 (exact date not available)"
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
    "last_trade / first_trade: dates of fills in OrcaLayer's index. Dates before October 2025 are shown only as "
    "'before 2025-10' while a timestamp correction of the older history is pending."
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
    "internal error". The API key is never included in the text.
    """
    return ToolError(_scrub(str(exc)))


# ── public tools ─────────────────────────────────────────────────────────────

@mcp.tool(title="Smart-money leaderboard", annotations=_READ_ONLY)
def leaderboard(
    sort: str = "pnl",
    category: str | None = None,
    filter: str = "smart",
    limit: int = 20,
) -> dict:
    """Rank Polymarket traders from OrcaLayer's Smart Money set (or all wallets).

    Use this to find top traders by lifetime profit, win rate, volume or trade
    count, optionally narrowed to one market category. Returns per wallet:
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
        category: Restrict to one category, e.g. "Crypto", "Sports",
            "Politics", "Geopolitics", "Economics", "Tech/AI". None = all.
        filter: "smart" (OrcaLayer's Smart Money set, default) or "all".
        limit: How many wallets to return (1–100).
    """
    limit = max(1, min(limit, 100))
    try:
        data = _client().leaderboard(
            sort=sort, category=category, filter=filter, limit=limit
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


@mcp.tool(title="Wallet overview", annotations=_READ_ONLY)
def wallet_overview(address: str) -> dict:
    """Summarize one wallet's trading profile and performance.

    Wallet profit tracking for any Polymarket address: lifetime P&L
    (Polymarket's figure), market_win_rate over resolved markets, a net
    profit factor, indexed volume and activity, category mix and leaderboard
    rankings. Accepts a 0x wallet address or an OrcaLayer nickname. Returns a
    compact summary rather than the full raw record, so it stays readable for
    heavy wallets. A `notes` block defines each field; market_win_rate has
    the same basis and value as in the leaderboard tool.

    If the wallet's stats are still being computed server-side, this returns
    a ``{"status": "computing", "retry_after_seconds": N}`` notice instead of
    data — call the tool again after that delay.

    Args:
        address: 0x wallet address or OrcaLayer nickname.
    """
    try:
        # poll=False keeps the call non-blocking: a cold heavy wallet raises
        # WalletComputingError at once (no ~60s SDK sleep) so we can surface a
        # "computing, retry later" notice instead of hitting the client timeout.
        data = _client().wallet_overview(address, poll=False)
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

    # Rankings left the overview payload in a backend performance split
    # (12.05.2026: the overview always carries rankings = null). They live on
    # /wallet/{address}/rankings now, so fetch them here; a failure there must
    # not cost the caller the overview.
    rankings = None
    wallet_ref = profile.get("proxy_wallet") or profile.get("address") or address
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


@mcp.tool(title="Wallet open positions", annotations=_READ_ONLY)
def wallet_positions(address: str, limit: int = 15) -> dict:
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
    try:
        # The API ignores the page limit and returns the wallet's full set
        # unordered, so we fetch all of them and do the top-N selection here.
        data = _client().wallet_positions(address, limit=500)
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


@mcp.tool(title="Market search", annotations=_READ_ONLY)
def markets(
    q: str = "",
    category: str | None = None,
    min_volume: float | None = None,
    min_whales: int | None = None,
    limit: int = 20,
) -> dict:
    """Search Polymarket markets, optionally where Smart Money wallets cluster.

    Use this to track smart money flows: find markets by topic and see how
    many wallets from OrcaLayer's Smart Money set hold each side right now.
    Returns each market's id, question, YES price, smart_wallets_yes /
    smart_wallets_no, volume, end date and days left. The counts are wallets,
    whatever their position size; popular markets have thousands. For the
    dollar split on one market use market_consensus.

    Args:
        q: Free-text query; also accepts a Polymarket URL or slug. "" browses.
        category: One of "Crypto", "Geopolitics", "Sports", "Politics",
            "Economics", "Tech/AI". None = all.
        min_volume: Minimum market volume in USD.
        min_whales: Minimum number of Smart Money wallets holding a position
            in the market (either side).
        limit: How many markets to return (1–100).
    """
    limit = max(1, min(limit, 100))
    try:
        data = _client().markets(
            q,
            category=category,
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
            "smart_wallets_yes": (
                "smart_wallets_yes / smart_wallets_no: how many wallets from " + _SMART_SET
                + " hold YES / NO in this market, counted regardless of position size (a $5 holder counts like a "
                "$500K one). market_consensus gives the capital-weighted split."
            ),
            "volume_usd": "volume_usd: the market's total traded volume in USD as Polymarket reports it.",
        },
    }


@mcp.tool(title="Smart-money consensus on a market", annotations=_READ_ONLY)
def market_consensus(market: str) -> dict:
    """Smart-money consensus on one Polymarket market versus its current price.

    Shows where smart money stands on a market: how many profitable
    smart-money whales hold YES vs NO, how much capital each side has
    invested, the current market price, and the divergence between
    smart-money positioning and that price. Use it to answer "what does
    smart money think about this market?" and to spot markets where smart
    money disagrees with the crowd.

    Two consensus reads are returned side by side and can disagree:
    ``head_count`` (one Smart Money wallet = one vote; wallets from
    OrcaLayer's Smart Money set, so popular markets count thousands) and
    ``capital_weighted`` (dollars invested per side). Head-count is the
    weaker signal — a $5 wallet counts the same as a $500K one — so when the
    two disagree, trust the capital split more. On cheap longshots (YES under ~15 cents) head-count skews
    YES structurally. Divergence from price is positioning information, not
    proof the market is mispriced; never present it as "the market is wrong".

    Args:
        market: Market id, Polymarket slug or URL, or 0x condition id.
    """
    # Accept a full Polymarket URL by reducing it to its slug.
    m = market.strip()
    if m.startswith("http"):
        m = m.rstrip("/").rsplit("/", 1)[-1]
    try:
        data = _client().market(m)
    except OrcaLayerError as exc:
        raise _real_failure(exc)

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
            "counts the same as a $500K one. Prefer the capital-weighted split "
            "when the two disagree. On cheap longshots (YES < ~15c) head-count "
            "skews YES structurally. Divergence from price is positioning "
            "information, not proof the market is mispriced."
        ),
    }


# ── premium tool ─────────────────────────────────────────────────────────────

@mcp.tool(title="Whale trade alerts (Premium)", annotations=_READ_ONLY)
def whale_alerts(
    minutes: int = 60,
    min_usd: float = 1000,
    category: str | None = None,
    limit: int = 25,
) -> Any:
    """Recent trades by smart-money whales — real-time alerts on profitable
    wallets (Premium).

    Returns whale trades in the last ``minutes`` over ``min_usd`` in size:
    who traded, buy/sell, side, amount, price and the market. Use it to get
    alerts when profitable Polymarket wallets open or close positions.

    Requires an OrcaLayer Premium API key: on the hosted server it is the
    ``Authorization: Bearer <key>`` request header, on the local stdio server
    the ORCALAYER_API_KEY environment variable. Without a key this returns a
    short notice on how to get one (it does not call the API and is not an
    error).

    Args:
        minutes: Lookback window in minutes (max 1440 = 24h).
        min_usd: Minimum trade size in USD.
        category: Restrict to one market category. None = all.
        limit: How many alerts to return (1–100).
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

    limit = max(1, min(limit, 100))
    try:
        return _client_for(key).whale_alerts(
            minutes=minutes, min_usd=min_usd, category=category, limit=limit
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
(1.2B+ on-chain fills across ~1.39M markets) and classifies the wallets behind
them.

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
  and split/merge flows attributed, to avoid double-counting.
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
- Hosted MCP server: https://orcalayer.com/mcp (Streamable HTTP; the same six
  tools as this package; no authentication for the public tools, a Premium
  key for whale_alerts as the `Authorization: Bearer <key>` request header)

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
91.8%). Read it as mechanics, NOT trader intent: MINT means the order matched
an opposite-side buyer (no seller was in the book), not that the wallet
deliberately split collateral; ~80% of all fills settle as MINT. The label is
tx-level (all fills of one match share it); COMPLEMENTARY means the tx
contains a complementary component; null is an honest refusal (lone fill at
the buffer edge or a complex batch) — never read null as "not a mint".
A second stream, /api/public/v1/live/trades-indexed (Premium), delivers
per-fill `entry_type` from on-chain data 20-45s later with a server-side
?types=mint,merge filter — use it when per-fill fidelity matters more than
speed.

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
