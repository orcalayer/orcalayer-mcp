# orcalayer-mcp

[![PyPI](https://img.shields.io/pypi/v/orcalayer-mcp.svg)](https://pypi.org/project/orcalayer-mcp/)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)
[![MCP Badge](https://lobehub.com/badge/mcp/orcalayer-orcalayer-mcp)](https://lobehub.com/mcp/orcalayer-orcalayer-mcp)
[![Glama MCP](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp/badges/score.svg)](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp)
[![orcalayer-mcp MCP server](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp/badges/card.svg)](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp)

Model Context Protocol (MCP) server for the [OrcaLayer API](https://orcalayer.com) —
Polymarket whale and market analytics inside Claude Desktop and other MCP clients.

It is a thin stdio wrapper over the [`orcalayer`](https://pypi.org/project/orcalayer/)
Python SDK and exposes five tools:

| Tool | What it does | Key |
|---|---|---|
| `leaderboard` | Rank smart-money whales by P&L, win rate or volume | No |
| `wallet_overview` | A wallet's profile and performance summary | No |
| `wallet_positions` | A wallet's largest open positions | No |
| `markets` | Search markets where smart whales are clustering | No |
| `whale_alerts` | Live feed of recent smart-whale trades | Premium |

Public tools work anonymously. `whale_alerts` needs a Premium API key
([get one](https://orcalayer.com/pricing)) supplied via the
`ORCALAYER_API_KEY` environment variable.

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
      "env": { "ORCALAYER_API_KEY": "ol_your_key" }
    }
  }
}
```

Restart Claude Desktop after editing the config.

## License

MIT. See [LICENSE](LICENSE).

Data is provided for informational purposes only and is not financial advice.
