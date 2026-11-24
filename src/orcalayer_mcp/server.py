"""OrcaLayer MCP server.

A thin Model Context Protocol wrapper over the ``orcalayer`` Python SDK. It
does not reimplement any API logic — every tool delegates to the SDK and
shapes the result for an LLM agent.

Public tools (``leaderboard``, ``wallet_overview``, ``wallet_positions``,
``markets``, ``market_consensus``) work anonymously. ``whale_alerts``,
``pnl_leaderboard`` and ``resolution_events`` (0.5.11) are Premium and need an
API key.

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
from typing import Annotated, Any, Literal

import httpx
from pydantic import Field
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
        "(whale_alerts), the realized PnL ranking over 7 or 30 days "
        "(pnl_leaderboard) and markets just proposed, disputed, resolved or "
        "reopened (resolution_events). Prompts hold ready-made analyses; resources hold the "
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
    "resource (about 190K of ~1.66M scored wallets). It is a quality filter, not a size filter"
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

# November 2026: market categories come from Polymarket's own tags; Esports, Culture and Weather were added and Sports
# no longer includes esports.
_CATEGORY_CODES = {
    "crypto": "CRYPTO",
    "politics": "POLITICS",
    "sports": "SPORTS",
    "esports": "ESPORTS",
    "geopolitics": "GEOPOLITICS",
    "economics": "ECONOMICS",
    "tech/ai": "TECH",
    "tech": "TECH",
    "ai": "TECH",
    "culture": "CULTURE",
    "weather": "WEATHER",
}
_CATEGORY_NAMES = '"Crypto", "Politics", "Sports", "Esports", "Geopolitics", "Economics", "Tech/AI", "Culture", "Weather"'
# Argument description in the tools' input schema (mcptry 16.11.2026: clients read properties.<arg>.description)
_CATEGORY_ARG = ('Restrict to one market category: "Crypto", "Politics", "Sports", "Esports", "Geopolitics", "Economics", '
                 '"Tech/AI", "Culture" or "Weather" (case-insensitive; "tech" and "ai" also work). Omit for all categories.')


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
    sort: Annotated[Literal["pnl", "win_rate", "volume", "trades"], Field(description=(
        'Ranking key: "pnl" (lifetime profit, default), "win_rate", "volume" or "trades". "win_rate" orders by the '
        "leaderboard's stored win rate, which is computed differently from the market_win_rate shown, so rows can look "
        "out of order by that column."))] = "pnl",
    category: Annotated[str | None, Field(description=(
        'Restrict to wallets mainly trading one category: "Crypto", "Politics", "Sports", "Esports", "Geopolitics", '
        '"Economics", "Tech/AI", "Culture" or "Weather" (case-insensitive). Omit for all categories.'))] = None,
    filter: Annotated[Literal["smart", "all"], Field(description=(
        '"smart" = OrcaLayer\'s Smart Money set (default); "all" = every ranked wallet.'))] = "smart",
    limit: Annotated[int, Field(description="How many wallets to return, 1 to 50 (larger values return 50).")] = 20,
    under_radar: Annotated[bool, Field(description=(
        'True = only "Under the radar" wallets: top 10% for form against the price over the last 90 days but not in '
        "Smart Money (and not bots). They are outside Smart Money by definition, so this replaces `filter`."))] = False,
    liquidity_provider: Annotated[bool, Field(description=(
        "True = only Liquidity providers: wallets Polymarket paid $100+ in liquidity rewards over the last 30 days, "
        "on 10+ days. Combines with `filter` and `under_radar`."))] = False,
    habit: Annotated[Literal["favorites", "holds", "exits"] | None, Field(description=(
        'Only wallets with one trading habit over the last 90 days (recounted daily): "favorites" = Buys favorites (half or '
        'more of the money into outcomes bought at 90 cents or more), "holds" = Holds to resolution (95%+ of what it bought '
        'held until the market resolved), "exits" = Exits early (30% or less held). Combines with the other filters. '
        "Omit for no habit filter."))] = None,
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
    """
    limit = max(1, min(limit, 50))
    try:
        if under_radar or liquidity_provider or habit:
            # The orcalayer SDK (0.2.x) has no under_radar / lp / habit arguments yet; the endpoint takes them as plain query parameters.
            q = {"sort": sort, "category": _category(category), "filter": "all" if under_radar else filter, "limit": limit}
            if under_radar:
                q["under_radar"] = 1
            if liquidity_provider:
                q["lp"] = 1
            if habit:
                q["habit"] = habit
            data = _client()._get("whales/leaderboard", q)
        else:
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
            "under_radar": w.get("under_radar"),
            "liquidity_provider": w.get("liquidity_provider"),
            "liquidity_rewards_30d": w.get("liquidity_rewards_30d"),
            "habits": w.get("habits") or [],
        })
    return {
        "wallets": rows,
        "total": data.get("total"),
        "sort": data.get("sort", sort),
        "filter": "+".join(x for x in (("under_radar" if under_radar else filter), ("liquidity_provider" if liquidity_provider else ""),
                                       (f"habit:{habit}" if habit else "")) if x),
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
            "under_radar": _NOTE_UNDER_RADAR,
            "liquidity_provider": _NOTE_LIQUIDITY.replace("liquidity_rewards: ", "liquidity_provider / liquidity_rewards_30d: ", 1),
            "habits": "habits: the wallet's trading-habit labels (favorites, holds, exits); [] = none. " + _NOTE_HABITS,
        },
    }


_NOTE_FORM_VS_PRICE = (
    "form_vs_price: how much more often the wallet won than its entry price implied, in cents per position, over markets "
    "resolved in the last window_days days (only wallets with 20+ such positions; otherwise null). Adjusted for the "
    "favourite / long-shot bias (each position is compared with all traders at the same entry price) and pulled towards "
    "zero for small samples. percentile: rank among wallets with the label, 100 = best. by_entry_price: raw positions, "
    "share won and average entry price per price band. sports_in_play_share: share of sports positions bought after the "
    "game started. under_radar: true when the wallet is in the top 10% for this form but is not in Smart Money (and is "
    "not a bot), so Smart Money lists leave it out (orcalayer.com/methodology#under-the-radar). Past form is not a guarantee."
)

_NOTE_UNDER_RADAR = (
    "under_radar: the wallet is in the top 10% for form against the price over the last 90 days but is not in Smart Money "
    "(and is not a bot), so Smart Money lists leave it out. Most such wallets miss the Smart Money win-rate bar (55%, "
    "counted per position; market_win_rate, one market = one result, can be higher): they buy at mid prices and win more "
    "often than the price implied. Tested before release: marked as of 1 Oct "
    "2026, they beat the price over the next six weeks (about +1.8 cents per position; a typical wallet by less) and few "
    "moved into Smart Money, so it is not a 'rising' mark. Past trades, not a forecast "
    "(orcalayer.com/methodology#under-the-radar)."
)


_NOTE_LIQUIDITY = (
    "liquidity_rewards: Polymarket liquidity rewards paid to the wallet over the last window_days (30) accrual days, read on-chain "
    "and recounted daily (rewards_usd, reward_days). liquidity_provider = true when it was paid $100+ on 10+ different days. "
    "Polymarket pays these for keeping limit orders on the book; they come on top of trading profit, and copying the wallet's "
    "trades does not earn them. The label held month to month in a pre-registered test and is not the Maker style under "
    "another name. It describes how a wallet earns, not how well it trades (orcalayer.com/methodology#liquidity-provider)."
)


def _liquidity(lr):
    """Compact liquidity-rewards block; None when the data is unavailable."""
    if not isinstance(lr, dict) or not lr.get("available"):
        return None
    return {
        "liquidity_provider": lr.get("liquidity_provider"),
        "rewards_usd": lr.get("rewards_usd"),
        "reward_days": lr.get("reward_days"),
        "window_days": lr.get("window_days"),
        "window_end": lr.get("window_end"),
    }


_NOTE_HABITS = (
    "Trading habits over the last window_days (90) days, recounted daily, from the wallet's own trades. favorites (Buys "
    "favorites): half or more of the money it put into positions (fav_share) went into outcomes bought at an average of 90 "
    "cents or more, with 20+ positions and $1,000+ of buys; farmers excluded. It wins often because it buys what is nearly "
    "decided; a win at 90 cents or more pays 10 cents or less per dollar, so its win rate says little about skill. holds "
    "(Holds to resolution): it kept 95% or more of what it bought until the market resolved (hold_share; a sale at an average "
    "of 95 cents or more counts as holding); to repeat its result, repeating its entries is enough. exits (Exits early): "
    "hold_share of 30% or less; its result depends on its exits, so copying only the entries gives a different result. Holds "
    "and exits need 20+ positions resolved in the window and $1,000+ of buys; market makers excluded. Each habit held between "
    "two 11-week windows in a pre-registered test. It describes how a wallet trades, not how well "
    "(orcalayer.com/methodology#habits)."
)


def _habits(h):
    """Compact trading-habits block; None when the data is unavailable. Numbers are null under 10 positions in the window."""
    if not isinstance(h, dict) or not h.get("available"):
        return None
    return {
        "habits": h.get("habits") or [],
        "fav_share": h.get("fav_share"),
        "fav_positions": h.get("fav_positions"),
        "fav_buy_usd": h.get("fav_buy_usd"),
        "hold_share": h.get("hold_share"),
        "hold_positions": h.get("hold_positions"),
        "hold_buy_usd": h.get("hold_buy_usd"),
        "window_days": h.get("window_days"),
        "computed_at": h.get("computed_at"),
    }


def _form_vs_price(f):
    """Compact form-vs-price block from the overview payload; None when the wallet has no label."""
    if not isinstance(f, dict) or not f.get("available"):
        return None
    return {
        "edge_cents": f.get("edge_cents"),
        "percentile": f.get("percentile"),
        "positions": f.get("positions"),
        "window_days": f.get("window_days"),
        "by_entry_price": f.get("by_entry_price"),
        "sports_in_play_share": f.get("sports_in_play_share"),
        "under_radar": f.get("under_radar"),
        "as_of": f.get("as_of"),
    }


_NOTE_STYLE = (
    "style: how the wallet trades over the last window_days (30) days, recounted weekly; only for wallets with 30+ own fills "
    "and $1,000+ volume in the window, otherwise null. styles is a subset of maker (60%+ of volume from its own limit orders "
    "that others filled), inplay (30%+ of buys on sports and half or more of those after the game started) and fast (half or "
    "more of the money it put in sold again within 2 hours); the shares behind them are given. It describes how a wallet "
    "trades, not how well: each style held month to month in a pre-registered test, and no reliable evidence was found that "
    "these wallets are harder to copy (orcalayer.com/methodology#style)."
)


def _style(st):
    """Compact trading-style block from the overview payload; None when the wallet has no style row."""
    if not isinstance(st, dict) or not st.get("available"):
        return None
    return {
        "styles": st.get("styles") or [],
        "maker_share": st.get("maker_share"),
        "sports_buy_share": st.get("sports_buy_share"),
        "in_play_share": st.get("in_play_share"),
        "fast_share": st.get("fast_share"),
        "fills": st.get("fills"),
        "window_days": st.get("window_days"),
        "as_of": st.get("as_of"),
    }


@mcp.tool(title="Wallet overview", annotations=_read_only("Wallet overview"))
def wallet_overview(
    address: Annotated[str | int, Field(description="0x wallet address (proxy or signer) or OrcaLayer nickname.")],
) -> dict:
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

    # 0.5.10 (28.10.2026): the overview's stats.pnl_7d is realized P&L of the positions that CLOSED in the past 7 days,
    # including what they earned before the window: 2-3x Polymarket's week on wallets that closed long-held winners.
    # pnl_7d is now the change over the 1W hourly series, the number the wallet page and Polymarket's 1W chart show;
    # the closed-positions sum stays under its own name. A failure here must not cost the caller the overview.
    pnl_7d = None
    try:
        hist = _client()._get(f"wallet/{urllib.parse.quote(str(wallet_ref))}/pnl-history", {"period": "1W"}, poll=False)
        points = (hist or {}).get("points") or []
        if len(points) >= 2:
            pnl_7d = round(float(points[-1]["p"]) - float(points[0]["p"]), 2)
    except Exception:  # noqa: BLE001 — optional context, like rankings
        pnl_7d = None

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
            "pnl_7d": pnl_7d,
            "realized_pnl_closed_7d": stats.get("pnl_7d"),
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
        "form_vs_price": _form_vs_price(data.get("form_vs_price")),
        "style": _style(data.get("style")),
        "liquidity_rewards": _liquidity(data.get("liquidity_rewards")),
        "habits": _habits(data.get("habits")),
        # True when heavy side-stats timed out: core stats above are still
        # valid, some derived fields may be missing.
        "degraded": data.get("degraded", False),
        "as_of": data.get("as_of"),
        "notes": {
            "total_pnl": _NOTE_PNL,
            "pnl_7d": (
                "pnl_7d: change in the wallet's Polymarket P&L over the past 7 days, open positions included, from the "
                "hourly series behind Polymarket's 1W profile chart; null when unavailable. Polymarket's weekly "
                "leaderboard can differ for fast-moving wallets. realized_pnl_closed_7d: realized P&L of the positions "
                "that closed in the past 7 days, including what they gained or lost before the window, so it can "
                "exceed pnl_7d and even lifetime total_pnl."
            ),
            "market_win_rate": _NOTE_MARKET_WIN_RATE,
            "profit_factor": (
                "profit_factor: net profit factor from OrcaLayer's net-position P&L per resolved market, not capped. The leaderboard "
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
            "form_vs_price": _NOTE_FORM_VS_PRICE,
            "style": _NOTE_STYLE,
            "liquidity_rewards": _NOTE_LIQUIDITY,
            "habits": "habits: " + _NOTE_HABITS + " null when unavailable.",
        },
    }


def _took_side(net: dict | None) -> dict | None:
    """head_count.took_side from the API's whales.net: wallets that took a side, the YES share of them and of their dollars."""
    if not isinstance(net, dict) or net.get("yes") is None:
        return None
    y, n = int(net.get("yes") or 0), int(net.get("no") or 0)
    return {
        "yes": y,
        "no": n,
        "no_side": net.get("no_side"),
        "dust": net.get("dust"),   # 0.5.21: wallets with under $10 on their side, not counted in yes / no
        "yes_pct": round(100.0 * y / (y + n), 1) if (y + n) > 0 else None,
        "yes_usd_pct": net.get("yes_pct_usd"),
    }


def _market_makers(mm):
    """0.5.22: market makers among the market's holders, per side, for all holders and for Smart Money ones (the API's
    market_makers, OrcaLayer backend 21.11.2026); None when the market has no Polymarket position snapshot."""
    if not isinstance(mm, dict):
        return None

    def _side(b):
        b = b if isinstance(b, dict) else {}
        return {
            "yes_wallets": int(b.get("yes_wallets") or 0),
            "no_wallets": int(b.get("no_wallets") or 0),
            "yes_invested_usd": round(float(b.get("yes_invested") or 0), 2),
            "no_invested_usd": round(float(b.get("no_invested") or 0), 2),
        }

    rule = mm.get("rule") if isinstance(mm.get("rule"), dict) else {}
    return {
        "rule": {
            "maker_share_min": rule.get("maker_share_min"),
            "fills_30d_min": rule.get("fills_30d_min"),
        },
        "all_holders": _side(mm.get("all")),
        "smart_money": _side(mm.get("smart")),
    }


def _in_form(f):
    """In-form wallets block from the market payload (top 10% by Form vs price, per side); None when absent."""
    if not isinstance(f, dict):
        return None
    return {
        "shown": bool(f.get("show")),
        "reason_hidden": f.get("reason"),
        "yes": f.get("yes"),
        "no": f.get("no"),
        "total": f.get("total"),
        "lean": f.get("lean"),
        "positions_synced_at": f.get("positions_synced_at"),
    }


@mcp.tool(title="Wallet open positions", annotations=_read_only("Wallet open positions"))
def wallet_positions(
    address: Annotated[str | int, Field(description="0x wallet address (proxy or signer) or OrcaLayer nickname.")],
    limit: Annotated[int, Field(description=(
        "How many open positions to return, 1 to 50. The wallet may hold more; the response reports how many were "
        "left out."))] = 15,
) -> dict:
    """List a wallet's largest open positions by current value.

    Accepts a 0x wallet address or an OrcaLayer nickname. Returns the
    positions with the largest current value first, in a compact form, plus a
    count of how many more are not shown.
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
    q: Annotated[str | int, Field(description=(
        'Free-text query; also accepts a Polymarket URL or slug. "" browses. Open markets only.'))] = "",
    category: Annotated[str | None, Field(description=_CATEGORY_ARG)] = None,
    min_volume: Annotated[float | None, Field(description="Minimum market volume in USD. Omit for no minimum.")] = None,
    min_whales: Annotated[int | None, Field(description=(
        "Minimum number of Smart Money wallets holding a position in the market (either side). Omit for no "
        "minimum."))] = None,
    limit: Annotated[int, Field(description="How many markets to return, 1 to 50 (larger values return 50).")] = 20,
) -> dict:
    """Search Polymarket markets, optionally where Smart Money wallets cluster.

    Finds markets by topic, slug or URL and shows how many wallets from
    OrcaLayer's Smart Money set hold each side right now. Returns each
    market's id, question, YES price, smart_wallets_yes / smart_wallets_no,
    volume, end date and days left. The counts are wallets, whatever their
    position size; the busiest markets count hundreds, a handful over a
    thousand. The dollar split on one market is in the market_consensus tool.
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
def market_consensus(
    market: Annotated[str | int, Field(description=(
        "Market id, Polymarket market or event slug, polymarket.com URL, or 0x condition id."))],
) -> dict:
    """Smart-money consensus on one Polymarket market versus its current price.

    Returns how Smart Money wallets are positioned on one market: how many
    hold YES and NO, how much capital each side has invested, the current
    market price, and the gap between that positioning and the price.

    Two consensus reads are returned side by side and can disagree:
    ``head_count`` (one Smart Money wallet = one vote; wallets from
    OrcaLayer's Smart Money set; the busiest markets count hundreds, a handful
    over a thousand; a wallet holding both sides is in both counts, while
    ``head_count.took_side`` counts each wallet once, on the side holding more
    of its dollars and only with $10 or more on that side, as the OrcaLayer
    market page does; ``dust`` is how many were left out under $10) and
    ``capital_weighted`` (dollars invested per side). In head_count a $5
    wallet weighs the same as a $500K one, so it is the weaker of the two
    reads; the capital split reflects the money at stake. On cheap longshots
    (YES under ~15 cents) head_count leans YES structurally. The divergence
    fields measure positioning against the price; they are not evidence that
    the price is wrong.

    ``market_makers`` counts the market makers among the holders on each
    side, for all holders and for the Smart Money ones (a market maker: 90%+
    of its last 30 days of volume came from its own resting orders that others
    filled, over 5,000+ fills, the Market maker label of the OrcaLayer market
    page). A market maker's position is inventory it trades around, not a view
    on the outcome. These wallets are still counted in head_count and
    capital_weighted; null when the market has no position snapshot.

    An event slug or event URL (a Polymarket event groups several markets)
    returns the event's markets with their ids and Smart Money wallet counts
    instead, since the consensus is per market.
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
            # distinct Smart Money wallets holding the market; both_sides of them are in both counts above
            "wallets": whales.get("wallets"),
            "both_sides": whales.get("both_sides"),
            # each wallet once, on the side holding more of its dollars (the headline read of the OrcaLayer market page);
            # no_side = wallets the market page sets apart, holding both sides
            "took_side": _took_side(whales.get("net")),
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
        "in_form_wallets": _in_form(data.get("in_form")),
        "market_makers": _market_makers(data.get("market_makers")),
        "caveats": (
            "head_count counts wallets from " + _SMART_SET + ", whatever their "
            "position size (the busiest markets count hundreds, a handful over a "
            "thousand; a wallet holding both sides is in both counts, took_side "
            "counts it once and only with $10+ on its side), and a $5 wallet "
            "counts the same as a $500K one; the capital-weighted split reflects "
            "the money at stake and carries more information when the two "
            "disagree. On cheap longshots (YES < ~15c) head-count leans YES "
            "structurally. Divergence from price is positioning information, "
            "not evidence that the market is mispriced. in_form_wallets: wallets "
            "from the top 10% by Form vs price holding each side (positions of "
            "$10+, each wallet on its bigger side); shown is false on crypto, "
            "within a day of the end or the game start, with fewer than 3 such "
            "wallets or a YES price outside 5-95c. In a pre-registered test on "
            "closed markets the side held by 2/3+ of three or more of them won "
            "more often than its price implied (orcalayer.com/methodology"
            "#in-form-wallets); it describes positions, not a forecast. "
            "market_makers: a market maker's position is inventory it trades "
            "around, not a view on the outcome; those wallets are still in "
            "head_count and capital_weighted."
        ),
    }


# ── premium tool ─────────────────────────────────────────────────────────────

@mcp.tool(title="Whale trade alerts (Premium)", annotations=_read_only("Whale trade alerts (Premium)"))
def whale_alerts(
    minutes: Annotated[int, Field(description=(
        "Look-back window in minutes, 1 to 1440 (24 hours); values outside the range are clamped to it."))] = 60,
    min_usd: Annotated[float, Field(description="Minimum trade size in USD.")] = 1000,
    category: Annotated[str | None, Field(description=_CATEGORY_ARG)] = None,
    limit: Annotated[int, Field(description="How many alerts to return, 1 to 50 (larger values return 50).")] = 25,
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


# ── Premium: window PnL and market resolution events (0.5.11) ────────────────
# Viktor's Premium plan, 03.11.2026 (backend c8fc488): both moved into Premium; the SDK has no methods for them yet, so
# they go through its generic Premium GET (the site proxy forwards /api/public/v1/<path> to /api/v2/<path>).

def _premium_notice(tool: str) -> str:
    return (
        f"{tool} is a Premium feature and needs an OrcaLayer API key. Get one at https://orcalayer.com/pricing. "
        "On the hosted server (https://orcalayer.com/mcp) send it as the request header "
        "`Authorization: Bearer <key>`; for the local stdio server set the ORCALAYER_API_KEY environment variable."
    )


def _premium_get(key: str, path: str, params: dict) -> Any:
    try:
        return _client_for(key)._get(path, params, premium_only=True)
    except AuthenticationError as exc:
        if exc.status_code == 403:
            raise ToolError(
                "Your API key is valid but your plan does not include Premium access. "
                "Upgrade at https://orcalayer.com/pricing."
            )
        raise ToolError("Your API key was rejected (invalid or expired). Check it at https://orcalayer.com/settings.")
    except OrcaLayerError as exc:
        raise _real_failure(exc)


_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@mcp.tool(title="Realized PnL leaderboard for 7 / 30 days (Premium)",
          annotations=_read_only("Realized PnL leaderboard for 7 / 30 days (Premium)"))
def pnl_leaderboard(
    window: Annotated[int, Field(description="7 or 30: Polymarket days in the window, ending on `as_of`.",
                                 json_schema_extra={"enum": [7, 30]})] = 7,
    order: Annotated[str, Field(description='"desc" = biggest realized profit first (default), "asc" = biggest loss first.',
                                json_schema_extra={"enum": ["desc", "asc"]})] = "desc",
    as_of: Annotated[str | None, Field(description=(
        "YYYY-MM-DD, the last day of the window (7-day windows from 2026-06-07, 30-day from 2026-06-30). Omit for the "
        "latest ranked day."))] = None,
    min_resolved: Annotated[int, Field(description=(
        "0, 1, 5 or 20: only wallets that held a position in at least that many markets resolved inside the window."),
        json_schema_extra={"enum": [0, 1, 5, 20]})] = 0,
    limit: Annotated[int, Field(description="How many wallets, 1 to 100 (larger values return 100).")] = 20,
) -> Any:
    """Polymarket wallets ranked by realized PnL inside a 7- or 30-day window
    (Premium).

    Who made (or lost) the most over the last week or month, not over all
    time. Realized PnL is Polymarket's own definition: a position counts when
    its market resolves or when it is sold, after fees. Rankings are rebuilt
    every night; every response carries the exact UTC window and whether the
    latest days are still provisional. Methodology:
    https://orcalayer.com/docs#window-pnl

    Needs an OrcaLayer Premium API key (hosted server: the
    ``Authorization: Bearer <key>`` header; stdio: ORCALAYER_API_KEY).
    Without a key the result is a short notice, not an error.
    """
    key = _request_api_key()
    if not key:
        return _premium_notice("pnl_leaderboard")
    if window not in (7, 30):
        raise ToolError("window must be 7 or 30.")
    order = str(order or "desc").strip().lower()
    if order not in ("desc", "asc"):
        raise ToolError('order must be "desc" (biggest profit first) or "asc" (biggest loss first).')
    if as_of is not None and not _DAY_RE.fullmatch(str(as_of).strip()):
        raise ToolError("as_of must be a date YYYY-MM-DD, or None for the latest ranked day.")
    if min_resolved not in (0, 1, 5, 20):
        raise ToolError("min_resolved must be 0, 1, 5 or 20.")
    limit = max(1, min(int(limit), 100))
    data = _premium_get(key, "business/pnl-leaderboard", {
        "window": window, "order": order, "as_of": as_of.strip() if as_of else None,
        "min_resolved": min_resolved, "limit": limit,
    })
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return data
    return {
        "window": {k: data.get(k) for k in ("as_of", "window", "window_start_utc", "window_end_utc",
                                            "provisional", "settled_through", "ranked_count")},
        "order": data.get("order"),
        "rows": [
            {k: r.get(k) for k in ("rank", "wallet", "pnl_usd", "volume_usd", "trades", "resolved_markets",
                                   "resolved_won", "pm_data_gap")}
            for r in data["rows"] if isinstance(r, dict)
        ],
        "notes": {
            "pnl_usd": "Realized PnL inside the window, after fees (Polymarket's definition); not lifetime P&L.",
            "provisional": "true = the latest days can still gain wallets; final once as_of <= settled_through.",
            "pm_data_gap": "Non-null = Polymarket's own daily data has a gap for this wallet (missing volume, USD).",
            "wallet_pages": "https://orcalayer.com/wallet/<wallet> for the full profile.",
        },
    }


_RESOLUTION_TYPES = ("proposed", "disputed", "resolved", "reopened")


@mcp.tool(title="Market resolution events (Premium)", annotations=_read_only("Market resolution events (Premium)"))
def resolution_events(
    minutes: Annotated[int, Field(description="Look-back window in minutes, 1 to 20160 (14 days).")] = 60,
    types: Annotated[list[str] | None, Field(description=(
        'Any of "proposed", "disputed", "resolved", "reopened". Omit for all four.'))] = None,
    category: Annotated[str | None, Field(description=_CATEGORY_ARG)] = None,
    min_volume: Annotated[float, Field(description="Only markets with at least this traded volume, in USD.")] = 0,
    limit: Annotated[int, Field(description="How many events, 1 to 200 (larger values return 200).")] = 50,
) -> Any:
    """Markets that were just proposed, disputed, resolved or reopened on
    Polymarket (Premium).

    Each event is one step of a market's settlement: ``proposed`` (an outcome
    was proposed to the UMA oracle), ``disputed``, ``resolved`` (settled, with
    ``outcome.winner``) and ``reopened`` (a closed market is trading again).
    Polymarket itself does not notify about these. Events are detected within
    about 10 minutes and kept for 14 days.

    Needs an OrcaLayer Premium API key (hosted server: the
    ``Authorization: Bearer <key>`` header; stdio: ORCALAYER_API_KEY).
    Without a key the result is a short notice, not an error.
    """
    key = _request_api_key()
    if not key:
        return _premium_notice("resolution_events")
    t = sorted({str(x).strip().lower() for x in (types or []) if str(x).strip()})
    bad = [x for x in t if x not in _RESOLUTION_TYPES]
    if bad:
        raise ToolError(f"Unknown event type(s): {', '.join(bad)}. Accepted: {', '.join(_RESOLUTION_TYPES)}.")
    minutes = max(1, min(int(minutes), 14 * 1440))
    limit = max(1, min(int(limit), 200))
    data = _premium_get(key, "markets/resolution-events", {
        "minutes": minutes, "types": ",".join(t) or None, "categories": _category(category),
        "min_volume": max(0.0, float(min_volume)), "limit": limit,
    })
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return data
    return {
        "events": data["events"],
        "count": len(data["events"]),
        "has_more": data.get("has_more"),
        "notes": {
            "order": "Oldest first from `minutes` ago; has_more = there are more events in the window (raise limit or narrow it).",
            "outcome": ("outcome.winner is set only on resolved events: \"Yes\" / \"No\", the outcome name, or \"50-50\"; "
                        "outcome.prices are the market prices when the event was read."),
            "detected_at": "When OrcaLayer saw the change (within ~10 min of it on Polymarket).",
            "market": "market.id opens at https://orcalayer.com/market/<id>; market.volume is lifetime traded volume, USD.",
        },
    }


# ── tool list per caller (0.5.7) ─────────────────────────────────────────────
# Viktor 07.10.2026: on the hosted server a request without a key does not see
# the Premium tool. Claude's directory connectors carry no key header, so for
# them whale_alerts could only ever answer "needs a key"; the directory review
# expects every listed tool to work, and keyed access outside OAuth is not a
# directory auth mode. Clients that send a key (Claude Code, Cursor, MCP
# Inspector, custom connectors with headers) still list all of them. The stdio
# package keeps listing it, with the notice, so local users can find Premium.
# A keyless call by name still returns the notice (the SDK only logs "not
# listed"; FastMCP registers call_tool without input validation).
_PREMIUM_TOOLS = frozenset({"whale_alerts", "pnl_leaderboard", "resolution_events"})  # 0.5.11: + the two plan tools
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
(1.5B+ on-chain fills across 5M+ markets, from 3.2M+ wallets) and computes
positions, win rates, rankings and badges from that record with its own
formulas. Polymarket's own figures appear in two named places: a second
opinion on the Smart Money badge, and the lifetime P&L headline on a wallet
page.

## Who is scored
Only wallets with at least 10 resolved markets are scored (about 1.66M,
November 2026).

## Smart money
A wallet is flagged **smart money** only when ALL four hold:
- win rate >= 55% measured **by resolved markets** (not by individual trades),
- positive total P&L across its resolved markets,
- not flagged as a farmer or bot,
- not losing by Polymarket's own count: if the lifetime profit Polymarket
  reports, no more than three days old, is zero or negative, the badge is
  withheld. This check can only remove a badge, never grant one; it removes
  roughly one candidate in twenty.
About 190K wallets qualify (November 2026).

## Farmer filter
A farmer's fingerprint is an average buy price above 95c: such an average
cannot be accumulated by taking real uncertain positions, and it manufactures
"flawless" 95-100% win rates on near-decided markets. On 27.10.2026 farmers
were 64% of scored wallets with a win rate of 90% or more, and about one in
five profitable ones. Bots (more than 500,000 trades at an average size under
$20) are detected separately; Polymarket's protocol router contracts are
excluded platform-wide.

## NegRisk correction
On linked multi-outcome (NegRisk) markets the resolved profit is halved, and
when a wallet traded both sides of a market the $1 of a minted or merged full
set is charged half to each side, so a multi-outcome win counts as one win.
Win rate is the share of a wallet's resolved markets that ended in profit:
markets, not individual fills.

## P&L method
For every wallet, market and outcome OrcaLayer keeps a running net position:
tokens bought minus tokens sold, dollars paid minus dollars received. When the
market resolves, the result is what the remaining tokens pay out minus the net
dollars put in. A market is a win when that result is above zero, and total
P&L is the sum over resolved markets only. For a settled market this gives the
same total as first-in, first-out lot accounting: lot order changes when
profit is recognised, not how much.

## Polymarket's numbers, and what OrcaLayer counts beyond them
- The lifetime P&L headline on a wallet page is Polymarket's figure; OrcaLayer
  shows its own computation when Polymarket has none.
- Win rate, wins and losses, and profit factor are OrcaLayer's, per resolved
  market.
- The index also counts what a profile page does not show: a losing position
  its owner never redeemed is still a loss, minted and merged full sets are
  part of what a position cost, and win rate covers every resolved market a
  wallet touched, not only the positions it chose to close.
- Because the two are counted differently, they can differ. When a fresh
  Polymarket figure and OrcaLayer's computation disagree on whether a wallet
  is up or down and both amounts are above $100, the wallet page marks
  OrcaLayer's derived statistics as low confidence.

## Smart money consensus
A market's consensus is a head count of Smart Money wallets on Yes against No
(one wallet, one vote; a wallet holding both sides counts on both), plus a net
reading: the share of dollars on Yes among wallets that take one side. When
the two disagree, the dollar reading is the stronger one. Consensus describes
positioning against the current price; it is not evidence that the price is
wrong.

## ISW frontline signals
For Ukraine territorial-control markets OrcaLayer compares each market's point
or zone with ISW's daily map: control, advances and 24-hour gains for capture
markets, plus infiltration for markets that resolve on Russian forces entering
a place. CRITICAL means the condition already looks met on the map; below
that, HIGH (about 500 m), MEDIUM (about 1.5 km), LOW (about 3 km), otherwise
SAFE. A level is a reading of ISW's map, not a resolution.

Full version: https://orcalayer.com/methodology
"""

_GLOSSARY = """# Prediction-markets glossary (OrcaLayer)

- **Smart money** — wallet that passes OrcaLayer's four-part test: >=55% market
  win rate, positive P&L on resolved markets, not a farmer/bot, and not losing by
  Polymarket's own count; only wallets with 10+ resolved markets are scored. The
  signal OrcaLayer tracks.
- **Farmer** — wallet whose average buy price is above 95c; inflates win rate by
  buying near-decided markets. Excluded from smart-money stats.
- **Hedger** — holds offsetting positions on both sides; apparent "profit" can be
  a structure rather than directional edge.
- **Market maker (MM)** — provides liquidity on both sides; P&L is spread capture,
  not a directional call.
- **NegRisk** — Polymarket's linked multi-outcome markets; resolved P&L is halved
  and split/merge flows attributed, so nothing is counted twice.
- **Net-position P&L** — what the remaining tokens pay out at settlement minus the
  net dollars put in, per wallet, market and outcome; resolved markets only.
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
  public tools need no authentication; whale_alerts, pnl_leaderboard and
  resolution_events are listed for requests that carry a Premium key as the
  `Authorization: Bearer <key>` header)

## Auth
Send a Premium key as `Authorization: Bearer <key>` or `x-api-key: <key>`. Public
read endpoints (wallet profiles, market search, leaderboard) need no key.

## Rate limits
- Anonymous public: 200 requests/minute per IP (plus Cloudflare throttling).
- Premium API key: 600 requests/minute (sliding 60s window, no daily cap).
Every response carries X-RateLimit-* headers.

## Real-time feed
Server-Sent Events (SSE) at /api/public/v1/live/trades (Premium) streams every
Polymarket fill as it happens — a long-lived EventSource connection, one JSON
event per filled order (the taker's and each maker's), each naming one wallet.
(It is SSE, not WebSocket.)

Since 2026-11-24 fills on Polymarket's Protocol V2 markets (new exchange, rolling
out since 2026-11-23), which the public activity feed behind the relay does not
carry, come from our own indexer: same event shape plus "source": "indexer",
"log_index" and "indexer_published_at_ms", ~20-60 s after the block (median
~22 s), so sort by "timestamp" if order matters; name / pseudonym / bio /
profileImage / fee are null in them. Events without "source" are unchanged.

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
