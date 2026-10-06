# orcalayer-mcp

[![PyPI](https://img.shields.io/pypi/v/orcalayer-mcp.svg)](https://pypi.org/project/orcalayer-mcp/)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)
[![MCP Badge](https://lobehub.com/badge/mcp/orcalayer-orcalayer-mcp)](https://lobehub.com/mcp/orcalayer-orcalayer-mcp)
[![Glama MCP](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp/badges/score.svg)](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp)
[![orcalayer-mcp MCP server](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp/badges/card.svg)](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp)

Model Context Protocol (MCP) server for the [OrcaLayer API](https://orcalayer.com) —
Polymarket whale and market analytics inside Claude, Cursor and other MCP clients.

Two ways to use it:

- **Hosted, nothing to install:** `https://orcalayer.com/mcp` (Streamable HTTP).
  Add it as a connector in Claude (Settings → Connectors → Add custom connector,
  paste the URL) or in any client that supports remote MCP servers.
- **Local (stdio):** `uvx orcalayer-mcp`, for Claude Desktop and other local clients.

Either way it is a thin wrapper over the [`orcalayer`](https://pypi.org/project/orcalayer/)
Python SDK and exposes six read-only tools:

| Tool | What it does | Key |
|---|---|---|
| `leaderboard` | Rank smart-money whales by P&L, win rate or volume | No |
| `wallet_overview` | Wallet profit tracking: profile, P&L and win-rate summary | No |
| `wallet_positions` | A wallet's largest open positions | No |
| `markets` | Track smart money flows: search markets where smart whales are accumulating | No |
| `market_consensus` | Smart-money consensus on one market vs its price (head-count + capital-weighted) | No |
| `whale_alerts` | Real-time alerts on profitable wallets: live feed of smart-whale trades | Premium |

Public tools work anonymously. `whale_alerts` needs a Premium API key
([get one](https://orcalayer.com/pricing)): on the hosted server send it as the
`Authorization: Bearer <key>` request header (clients that support request
headers for a connector), on the local server set the `ORCALAYER_API_KEY`
environment variable. Without a key the tool answers with a short notice, not
an error.

Every tool carries `readOnlyHint: true`: nothing is created, changed or
deleted on your behalf. Data is read from the OrcaLayer API only.

> **New (2026-07-22):** the Premium SSE stream (`/api/public/v1/live/trades`)
> now carries `settlement_type` (`MINT` / `MERGE` / `COMPLEMENTARY` / `null`)
> on every event — live-derived settlement mechanics, shadow-verified
> **100.000% accurate on MINT/MERGE**. It describes how the match settled,
> **not trader intent** (~80% of all fills settle as MINT; `null` = honest
> refusal, not "not a mint"). Details: the `orcalayer://api-reference`
> resource or [orcalayer.com/docs/api](https://orcalayer.com/docs/api).

## Prompts

Ready-to-use prompts for common analytics scenarios:

| Prompt | What it does |
|---|---|
| `analyze_wallet` | Full wallet analysis — smart money or farmer? |
| `find_divergence` | Markets where smart money disagrees with the current price |
| `hedge_check` | Whether a wallet's profit was real alpha or a hedge structure |
| `territorial_markets_review` | Ukraine territorial markets with ISW frontline overlay |

In Claude Desktop, pick a prompt from the prompt menu (the `+` / slash-command
picker) — each one orchestrates the tools above for you.

## Resources

Read-only context the model can pull directly — no tool call needed:

| Resource URI | Content |
|---|---|
| `orcalayer://methodology` | How smart money is filtered from farmers, hedgers and market-makers |
| `orcalayer://glossary` | Prediction-markets glossary |
| `orcalayer://api-reference` | OrcaLayer REST API reference (endpoints, auth, rate limits) |

## Use with Claude Desktop

Add this to your `claude_desktop_config.json`
(`%APPDATA%\Claude\claude_desktop_config.json` on Windows,
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "orcalayer": {
      "command": "uvx",
      "args": ["orcalayer-mcp"]
    }
  }
}
```

The public tools work as-is. For the Premium `whale_alerts` tool, add your
API key ([get one](https://orcalayer.com/pricing)):

```json
{
  "mcpServers": {
    "orcalayer": {
      "command": "uvx",
      "args": ["orcalayer-mcp"],
      "env": { "ORCALAYER_API_KEY": "sk_orca_your_key_here" }
    }
  }
}
```

Restart Claude Desktop after editing the config.

## Use the hosted server

The same six tools, prompts and resources are served at
`https://orcalayer.com/mcp` over Streamable HTTP. No account is needed for the
public tools. In Claude: Settings → Connectors → Add custom connector → paste
the URL. For `whale_alerts`, send your Premium key as the
`Authorization: Bearer <key>` request header where your client supports
connector headers; keys are never accepted in the URL.

## Self-host over HTTP

```bash
pip install orcalayer-mcp
orcalayer-mcp --http --host 127.0.0.1 --port 8020 --path /mcp
```

The server is stateless and answers plain JSON, so any reverse proxy with TLS
in front of it will do (forward `POST /mcp` to `127.0.0.1:8020/mcp`). The
`ORCALAYER_MCP_HTTP=1`, `ORCALAYER_MCP_HOST`, `ORCALAYER_MCP_PORT` and
`ORCALAYER_MCP_PATH` environment variables are equivalent to the flags.
`ORCALAYER_MCP_ANON_BASE_URL` optionally routes anonymous calls to a different
API host (the hosted server points it at its local backend); calls that carry
a key always go to `https://orcalayer.com`. Put your own per-client rate limit
in the proxy: the server itself does not limit callers.

## License

MIT. See [LICENSE](LICENSE).

Data is provided for informational purposes only and is not financial advice.

---

mcp-name: io.github.orcalayer/orcalayer-mcp
