"""OrcaLayer MCP server.

A thin Model Context Protocol (stdio) wrapper over the ``orcalayer`` Python
SDK. It does not reimplement any API logic — every tool delegates to the SDK
and shapes the result for an LLM agent.

Public tools (``leaderboard``, ``wallet_overview``, ``wallet_positions``,
``markets``) work anonymously. ``whale_alerts`` is Premium and needs an API
key supplied via the ``ORCALAYER_API_KEY`` environment variable.

Run it as ``orcalayer-mcp`` (console script) or ``python -m orcalayer_mcp``.
"""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from orcalayer import (
    AuthenticationError,
    OrcaLayer,
    OrcaLayerError,
    WalletComputingError,
)

mcp = FastMCP("orcalayer")

# A single shared client for the process. The SDK constructor is cheap — it
# makes no network call and does not validate the key — so an anonymous start
# and a bad-key start both come up cleanly; a wrong key only surfaces when a
# Premium tool is actually called.
try:
    _MCP_VERSION = _pkg_version("orcalayer-mcp")
except PackageNotFoundError:  # running from a source checkout without install
    _MCP_VERSION = "dev"

_API_KEY = os.environ.get("ORCALAYER_API_KEY") or None
_client = OrcaLayer(
    api_key=_API_KEY,
    user_agent_suffix=f"orcalayer-mcp/{_MCP_VERSION}",
)


# ── error handling ───────────────────────────────────────────────────────────

def _scrub(message: str) -> str:
    """Defensively strip the API key from any text before it leaves the server.

    The SDK's own exception messages never carry the key, but this guarantees
    it can never leak through an error string even if that changes upstream.
    """
    if _API_KEY:
        return message.replace(_API_KEY, "***")
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

@mcp.tool()
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
    try:
        return _client.leaderboard(
            sort=sort, category=category, filter=filter, limit=limit
        )
    except OrcaLayerError as exc:
        raise _real_failure(exc)


@mcp.tool()
def wallet_overview(address: str) -> dict:
    """Summarize one wallet's trading profile and performance.

    Accepts a 0x wallet address or an OrcaLayer nickname. Returns a compact
    summary — identity, lifetime activity, and P&L stats — rather than the
    full raw record, so it stays readable for heavy wallets.

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
        data = _client.wallet_overview(address, poll=False)
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


@mcp.tool()
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
        data = _client.wallet_positions(address, limit=500)
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


@mcp.tool()
def markets(
    q: str = "",
    category: str | None = None,
    min_volume: float | None = None,
    min_whales: int | None = None,
    limit: int = 20,
) -> dict:
    """Search Polymarket markets, optionally where smart whales are clustering.

    Use this to find markets by topic and surface ones with heavy smart-money
    interest. Returns each market's question, YES price, smart-whale counts on
    each side, volume and days left.

    Args:
        q: Free-text query; also accepts a Polymarket URL or slug. "" browses.
        category: One of "Crypto", "Geopolitics", "Sports", "Politics",
            "Economics", "Tech/AI". None = all.
        min_volume: Minimum market volume in USD.
        min_whales: Minimum number of smart whales active in the market.
        limit: How many markets to return (1–100).
    """
    try:
        return _client.markets(
            q,
            category=category,
            min_volume=min_volume,
            min_whales=min_whales,
            limit=limit,
        )
    except OrcaLayerError as exc:
        raise _real_failure(exc)


# ── premium tool ─────────────────────────────────────────────────────────────

@mcp.tool()
def whale_alerts(
    minutes: int = 60,
    min_usd: float = 1000,
    category: str | None = None,
    limit: int = 25,
) -> Any:
    """Recent trades by smart-money whales — a live alerts feed (Premium).

    Returns whale trades in the last ``minutes`` over ``min_usd`` in size:
    who traded, buy/sell, side, amount, price and the market.

    Requires a Premium API key set via the ORCALAYER_API_KEY environment
    variable. Without a key this returns a short notice on how to get one (it
    does not call the API and is not an error).

    Args:
        minutes: Lookback window in minutes (max 1440 = 24h).
        min_usd: Minimum trade size in USD.
        category: Restrict to one market category. None = all.
        limit: How many alerts to return (1–100).
    """
    # Pre-check the key so the "needs Premium" path is an actionable message,
    # never a failure and never an API round-trip.
    if not _API_KEY:
        return (
            "whale_alerts is a Premium feature and needs an API key. "
            "Get one at https://orcalayer.com/pricing and set it as the "
            "ORCALAYER_API_KEY environment variable for this MCP server."
        )

    try:
        return _client.whale_alerts(
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


# ── entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
