# orcalayer-mcp

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
