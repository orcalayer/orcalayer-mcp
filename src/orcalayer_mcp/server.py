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
import os
import threading
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
            client = OrcaLayer(api_key=key, user_agent_suffix=_UA_SUFFIX)
            _clients[key] = client
        return client


def _client() -> OrcaLayer:
    """The SDK client for the current call (anonymous or keyed)."""
    return _client_for(_request_api_key())


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
    """Rank the most successful Polymarket whales (smart-money traders).

    Use this to find top traders by realized profit, win rate, or volume —
    optionally narrowed to one market category. Returns each whale's wallet,
    name, total P&L, win rate, profit factor and resolved-market count.

    Args:
        sort: Ranking key — "pnl" (default), "win_rate", "volume" or "trades".
        category: Restrict to one category, e.g. "Crypto", "Sports",
            "Politics", "Geopolitics", "Economics", "Tech/AI". None = all.
        filter: "smart" (curated profitable whales, default) or "all".
        limit: How many whales to return (1–100).
    """
    limit = max(1, min(limit, 100))
    try:
        return _client().leaderboard(
            sort=sort, category=category, filter=filter, limit=limit
        )
    except OrcaLayerError as exc:
        raise _real_failure(exc)


@mcp.tool(title="Wallet overview", annotations=_READ_ONLY)
def wallet_overview(address: str) -> dict:
    """Summarize one wallet's trading profile and performance.

    Wallet profit tracking for any Polymarket address: lifetime P&L, win
    rate, ROI-style stats (profit factor), volume and activity. Accepts a 0x
    wallet address or an OrcaLayer nickname. Returns a compact summary —
    identity, lifetime activity, and P&L stats — rather than the full raw
    record, so it stays readable for heavy wallets.

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

    return {
        "profile": {
            "name": profile.get("name"),
            "pseudonym": profile.get("pseudonym"),
            "address": profile.get("address"),
            "proxy_wallet": profile.get("proxy_wallet"),
        },
        "overview": {
            "total_trades": overview.get("total_trades"),
            "total_markets": overview.get("total_markets"),
            "total_volume": overview.get("total_volume"),
            "profit_factor": overview.get("profit_factor"),
            "active_positions": overview.get("active_count"),
            "closed_positions": overview.get("closed_count"),
            "median_hold_days": overview.get("median_hold_days"),
            "first_trade": overview.get("first_trade"),
            "last_trade": overview.get("last_trade"),
        },
        "stats": {
            "resolved_markets": stats.get("resolved"),
            "wins": stats.get("wins"),
            "losses": stats.get("losses"),
            "win_rate": stats.get("win_rate"),
            "total_pnl": stats.get("total_pnl"),
            "profit_factor": stats.get("profit_factor"),
            "is_smart": stats.get("is_smart"),
            "unrealized_pnl": stats.get("unrealized_pnl"),
            "pnl_24h": stats.get("pnl_24h"),
            "pnl_7d": stats.get("pnl_7d"),
        },
        # Volume share per market category (e.g. {"CRYPTO": 86.1, ...}) and
        # leaderboard rankings — cheap, high-signal context for an agent.
        # Passed through as-is; rankings may be null for wallets off the board.
        "categories": data.get("categories"),
        "rankings": data.get("rankings"),
        # True when heavy side-stats timed out: core stats above are still
        # valid, some derived fields may be missing.
        "degraded": data.get("degraded", False),
        "as_of": data.get("as_of"),
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
    """Search Polymarket markets, optionally where smart whales are clustering.

    Use this to track smart money flows: find markets by topic and surface
    where smart money is accumulating right now. Returns each market's
    question, YES price, smart-whale counts on each side, volume and days
    left.

    Args:
        q: Free-text query; also accepts a Polymarket URL or slug. "" browses.
        category: One of "Crypto", "Geopolitics", "Sports", "Politics",
            "Economics", "Tech/AI". None = all.
        min_volume: Minimum market volume in USD.
        min_whales: Minimum number of smart whales active in the market.
        limit: How many markets to return (1–100).
    """
    limit = max(1, min(limit, 100))
    try:
        return _client().markets(
            q,
            category=category,
            min_volume=min_volume,
            min_whales=min_whales,
            limit=limit,
        )
    except OrcaLayerError as exc:
        raise _real_failure(exc)


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
    ``head_count`` (one whale = one vote) and ``capital_weighted`` (dollars
    invested per side). Head-count is the weaker signal — a $5 wallet counts
    the same as a $500K one — so when the two disagree, trust the capital
    split more. On cheap longshots (YES under ~15 cents) head-count skews
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
            "Head-count consensus is a weak signal: a $5 wallet counts the "
            "same as a $500K one. Prefer the capital-weighted split when the "
            "two disagree. On cheap longshots (YES < ~15c) head-count skews "
            "YES structurally. Divergence from price is positioning "
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
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
