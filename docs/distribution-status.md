# orcalayer-mcp — Distribution Status & Infrastructure

> Snapshot of MCP server distribution channels, verification status, and supporting
> infrastructure as of 2026-06-28. Drop this into the Claude Code project repository
> (`docs/distribution-status.md` or similar) so future sessions know what already exists
> and don't duplicate work.

---

## TL;DR

`orcalayer-mcp` (v0.3.0, PyPI publish pending token) is published and active across **6 MCP distribution channels**, with the official MCP registry (registry.modelcontextprotocol.io) as the 7th once v0.3.0 lands on PyPI.
Supporting infrastructure includes `gh` CLI authenticated as `ekocam`, `glama.json` for
ownership verification, `smithery.yaml` for runtime config, `.smithery/manifest.json`
for reproducible Smithery bundle rebuilds, GitHub Actions CI (py3.10/3.11/3.12 matrix +
lint), and a GitHub Release `v0.2.0` with auto-imported changelog.

---

## 1. Distribution channels

### 1.1 Glama.ai

| Property | Value |
|---|---|
| Listing URL | https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp |
| Release page | https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp/admin/dockerfile/releases |
| Status | LIVE + verified ownership (re-claimed 2026-08-20 by Viktor after it showed UNCLAIMED that day; "Server maintainers are verified by Glama" badge and Admin tab back) |
| Latest release | v0.2.0 published 2026-06-28 09:06 (`latest` badge) |
| Docker image | `registry.glama.ai/mcp-sz4g2fpc7g:c2mylpel92` |
| Categories | Blockchain, Finance, Cryptocurrency |
| Profile score | A / A / B (2026-08-20 check; was 67% Class B on 28.06) |
| Ownership claim mechanism | `glama.json` with `maintainers: ["ekocam"]` |
| Score badge (in README) | `https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp/badges/score.svg` |
| Card badge (in README) | `https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp/badges/card.svg` |

Auto-publishes new releases on each successful build pipeline (triggered by repo
push). Build log: visible under Admin → Dockerfile → "View Build Test".

### 1.2 mcp.so

| Property | Value |
|---|---|
| Listing URL | https://mcp.so/server/orcalayer-mcp |
| Status | LIVE published (submitted 2026-06-22, approved within hours) |
| Tags | polymarket, prediction-markets, crypto, blockchain, polygon, analytics, smart-money |
| Install command | `uvx orcalayer-mcp` |
| Submission method | Form on https://mcp.so/submit (GitHub OAuth) |

Re-submissions / edits possible via the `My Servers` admin area.

### 1.3 LobeHub

| Property | Value |
|---|---|
| Listing URL | https://lobehub.com/mcp/orcalayer-orcalayer-mcp |
| Status | Listed, ownership unverified, score 45/100 (re-scan triggered manually 2026-06-28) |
| Category | Финансы и акции (Finance & Stocks) |
| Required badge for ownership verification | `[![MCP Badge](https://lobehub.com/badge/mcp/orcalayer-orcalayer-mcp)](https://lobehub.com/mcp/orcalayer-orcalayer-mcp)` (must be in README) |
| Score boost mechanism | Adding tools/prompts/resources is detected via MCP RPC probe; LobeHub crawls PyPI + GitHub on a multi-day schedule |

`Обновить метаданные` button on the score tab can be pressed to force a re-scan.
If verification doesn't complete within 24h after badge push, the rendered badge URL
might be off — verify by opening the badge URL directly in a browser.

### 1.4 awesome-mcp-servers (community list)

| Property | Value |
|---|---|
| PR URL | https://github.com/punkpeye/awesome-mcp-servers/pull/8763 |
| Status | MERGED (~July 2026); mcpservers.org mirror auto-picked the entry |
| Category placed | 💰 Finance & Fintech |
| Bot check enforcement | Glama score badge must appear **inline** in the list entry (not just PR body) |
| Final entry format | `- [orcalayer/orcalayer-mcp](https://github.com/orcalayer/orcalayer-mcp) 🐍 ☁️ [![orcalayer/orcalayer-mcp MCP server](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp/badges/score.svg)](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp) - Polymarket whale tracking & smart-money analytics. Filtered leaderboard (airdrop-farmers excluded), hedge detection, NegRisk correction, Ukraine territorial markets with ISW frontline overlay.` |
| Pending action | none — merged |

Once merged, mirrors (`glama.ai`, `mcpservers.org`) auto-pickup the entry.

### 1.5 Smithery.ai

| Property | Value |
|---|---|
| Listing URL | https://smithery.ai/server/orcalayer/orcalayer-mcp |
| Releases page | https://smithery.ai/servers/orcalayer/orcalayer-mcp/releases |
| Runtime MCP URL | https://orcalayer-mcp--orcalayer.run.tools |
| Status | LIVE, public visibility, score **60/100 permanent** (see caveat below) |
| Namespace | `orcalayer` (org-owned, matches GitHub/Glama/mcp.so/LobeHub) |
| Publish mechanism | `npx @smithery/cli mcp publish ./orcalayer-mcp.mcpb -n orcalayer/orcalayer-mcp` |
| Bundle source | `.smithery/manifest.json` in repo (built locally via `npx @anthropic-ai/mcpb pack`) |
| Auto-discovery from `smithery.yaml` | Disabled by Smithery — manual publish required |
| First publish | 2026-06-28, release ID `29c88524` |

**Score ceiling — 60/100 accepted as permanent state.** Investigated 2026-07-01:
Smithery has an internal inconsistency between two of their own components —
their MCPB bundle validator (used at `mcpb pack` step) **rejects** the `inputSchema`
field on tools, while their publish validator (used at `smithery mcp publish` step)
**requires** it. It is physically impossible to satisfy both at once. This is a
Smithery-side bug, not our failure.

Consequences we accept:
- Score stays at 60/100 (capabilities scan can't run for stdio servers anyway)
- Tools/prompts/resources don't appear in the Smithery UI listing
- **Real usage is unaffected** — `uvx orcalayer-mcp` still hands over all 5 tools
  + 4 prompts + 3 resources to any MCP client that installs it
- Resources would be hidden per MCPB spec anyway (they're declared as dynamic)

Probe scripts and a working manifest shape are archived in a local scratchpad;
if Smithery reconciles their validator with the MCPB format (or adds stdio scan
support), we can re-publish in ~5 minutes.

Search index lags — newly published servers may take 1-24h to appear in
`https://smithery.ai/servers?q=...`. Direct URL works immediately.

To republish a new version: rebuild the `.mcpb` via `npx @anthropic-ai/mcpb pack`
in a workspace that contains `manifest.json`, then `npx @smithery/cli mcp publish
./orcalayer-mcp.mcpb -n orcalayer/orcalayer-mcp`.

### 1.6 mcpservers.org (mirror)

Auto-syncs with `awesome-mcp-servers`. Will pick up our entry automatically once
PR #8763 merges. No manual submission required.

---

### 1.7 Official MCP registry (registry.modelcontextprotocol.io) — NEW 2026-08-20

| Property | Value |
|---|---|
| Status | NOT YET PUBLISHED — do this with the v0.3.0 release |
| Publish CLI | `mcp-publisher` (docs: https://modelcontextprotocol.io/registry/quickstart) |
| Namespace | **`io.github.orcalayer/orcalayer-mcp`** — the repo lives under the `orcalayer` org, NOT under `ekocam`. Verify before first publish; the name is hard to change later |
| Auth | GitHub (org ownership determines the namespace) |
| Prereq | Package must already be on PyPI (registry stores metadata only) — satisfied once 0.3.0 lands |
| Caveat | Registry is in **preview**: breaking changes and data resets possible. Publish, but do not hang critical dependencies on it; be ready to re-publish |

### 1.8 PulseMCP (www.pulsemcp.com)

Checked 2026-08-20: `orcalayer` gives "No servers found" there. **Do NOT submit
manually** (Viktor, 20.08): PulseMCP pulls from the official MCP registry on its
own, so publishing to the registry (1.7) covers it. Re-check after the registry
entry is live.

---

## 2. Repository artifacts (added to support distribution)

### 2.1 `glama.json` (root)

Tells Glama who can claim ownership and provides rich category/tag metadata.

```json
{
  "$schema": "https://glama.ai/mcp/schemas/server.json",
  "maintainers": ["ekocam"],
  "categories": ["Finance", "Blockchain", "Cryptocurrency", "Data Analytics"],
  "tags": ["polymarket", "prediction-markets", "whale-tracking", "smart-money",
           "polygon", "on-chain-analytics", "isw", "ukraine"],
  "homepage": "https://orcalayer.com",
  "methodology": "https://orcalayer.com/methodology",
  "documentation": "https://orcalayer.com/developers",
  "license": "MIT",
  "language": "Python",
  "deployment": "stdio",
  "install": {
    "uvx": "uvx orcalayer-mcp",
    "pip": "pip install orcalayer-mcp",
    "pipx": "pipx install orcalayer-mcp"
  }
}
```

Schema URL may 404 — `additionalProperties` are allowed so rich fields don't break
the claim flow.

### 2.2 `smithery.yaml` (root)

Runtime config schema for Smithery — defines the optional `ORCALAYER_API_KEY` and
the launch command. **Not used for auto-discovery** (Smithery removed that), but
helpful for users who configure the server through Smithery's UI later.

### 2.3 `.smithery/manifest.json`

Source of truth for the Smithery bundle (`.mcpb`). Manifest version `0.2`, type
`python`, command `uvx orcalayer-mcp`. Includes `user_config.api_key` (optional
sensitive), declared tools (5), and declared prompts (4) with real text shapes
copied from `server.py` to satisfy Smithery's stricter validation than vanilla
`mcpb validate`.

Rebuild the bundle from this manifest with `npx @anthropic-ai/mcpb pack` in a
workspace where `manifest.json` is at the root.

### 2.4 `.github/workflows/ci.yml`

Two jobs:
- **test** — matrix py3.10/3.11/3.12, installs project, runs `pytest tests/` or
  falls back to import check + asserts MCP server lists ≥5 tools via
  `from orcalayer_mcp import mcp; await mcp.list_tools()`.
- **lint** — `ruff check src/` (non-blocking initially, `|| true`).

CI badge visible on the repo and feeds the Glama "CI status" check.

### 2.5 README badges (top)

```markdown
[![PyPI](https://img.shields.io/pypi/v/orcalayer-mcp.svg)](https://pypi.org/project/orcalayer-mcp/)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)
[![MCP Badge](https://lobehub.com/badge/mcp/orcalayer-orcalayer-mcp)](https://lobehub.com/mcp/orcalayer-orcalayer-mcp)
[![orcalayer-mcp MCP server](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp/badges/score.svg)](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp)
[![orcalayer-mcp MCP card](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp/badges/card.svg)](https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp)
```

The LobeHub badge URL **must** be exactly this format — it's the ownership
verification mechanism, not a generic shield.

### 2.6 README sections added in v0.2.0

- **Tools** table (5 tools — pre-existing)
- **Prompts** table (4 prompts — `analyze_wallet`, `find_divergence`, `hedge_check`, `territorial_markets_review`)
- **Resources** table (3 resources — `orcalayer://methodology`, `orcalayer://glossary`, `orcalayer://api-reference`)
- Install snippets for `uvx`, `pip`, `pipx`
- Claude Desktop + Cursor config examples

---

## 3. MCP server capabilities (v0.3.0)

| Type | Name | Description | Auth |
|---|---|---|---|
| Tool | `leaderboard` | Rank smart-money whales by P&L, win rate, volume | public |
| Tool | `wallet_overview` | Wallet profile + performance summary | public |
| Tool | `wallet_positions` | Wallet's largest open positions | public |
| Tool | `markets` | Track smart money flows: search markets where smart whales are accumulating | public |
| Tool | `market_consensus` | Smart-money consensus on one market vs its price: head-count AND capital-weighted splits, divergence in pp, explicit head-count caveat | public |
| Tool | `whale_alerts` | Live feed of recent smart-whale trades | Premium API key (`ORCALAYER_API_KEY`) |
| Prompt | `analyze_wallet` | Full wallet analysis (smart money or farmer?) | public |
| Prompt | `find_divergence` | Markets where smart money disagrees with price | public |
| Prompt | `hedge_check` | Verify if profit was alpha or hedge structure | public |
| Prompt | `territorial_markets_review` | Ukraine territorial markets with ISW overlay | public |
| Resource | `orcalayer://methodology` | Full classification methodology (markdown) | public |
| Resource | `orcalayer://glossary` | Prediction markets glossary | public |
| Resource | `orcalayer://api-reference` | OrcaLayer REST API reference | public |

Server object is `mcp` (FastMCP instance), not `server` — important for any
introspection code. Import path: `from orcalayer_mcp import mcp`.

---

## 4. Infrastructure / credentials

### 4.1 `gh` CLI

- Installed locally (`C:\Program Files\GitHub CLI\gh.exe`, v2.95.0)
- Authenticated as `ekocam` (`gh auth status` confirms)
- Token stored at `%APPDATA%\GitHub CLI\hosts.yml`
- Scopes: default (`repo` + `read:org`)
- Used for: PR creation, PR comments, GitHub Releases, fork+PR flows

### 4.2 GitHub Organization

- Org: `orcalayer` (owns the three open-source repos: `orcalayer-mcp`, `orcalayer-python`, `whale-watchlist-monitor`)
- OAuth app access restriction: **REMOVED** (third-party apps can see org membership without per-app approval). This was the unlock for Glama org-owned claim flow.
- All repos public, MIT licensed
- 1 member (`ekocam`)

### 4.3 PyPI

- Package: `orcalayer-mcp`
- Latest version: `0.3.0` (2026-08-20; PyPI publish needs the project token Viktor holds — `uv publish` with `UV_PUBLISH_TOKEN`).
- SDK dependency: `orcalayer>=0.2.2` (new `market()` method, released the same day)
- Entry point: `[project.scripts] orcalayer-mcp = "orcalayer_mcp:main"` (or similar — verify in `pyproject.toml`)
- Republish flow: bump version → `uv build && uv publish` (or `python -m build && twine upload`)

### 4.4 Smithery account

- Logged in as `ekocam`
- Org namespace `orcalayer` claimed
- API access via `npx @smithery/cli` (stored after `auth login`)
- Token persists locally, no manual export

### 4.5 GitHub Release v0.2.0

- URL: `https://github.com/orcalayer/orcalayer-mcp/releases/tag/v0.2.0`
- Title: "v0.2.0 — Prompts, Resources & LobeHub integration"
- Body: includes Tools (5), Prompts (4), Resources (3), changelog, install instructions
- Auto-imported by Glama as the release changelog

---

## 5. Re-publish workflow for the next version

When you bump from `0.2.0` to `0.3.x`:

1. **Bump version** in `pyproject.toml` (+ any version constant in `src/`)
2. **Update README** if new tools/prompts/resources were added
3. **Push to main** → CI runs (test + lint matrix), GitHub Actions sets the status
4. **Cut PyPI release:** `uv build && uv publish` (or twine equivalent)
5. **Cut GitHub Release:** `gh release create v0.3.x --title "..." --notes-file ./RELEASE_NOTES_0_3_x.md`
6. **Glama** auto-rebuilds + auto-publishes (no action needed)
7. **Smithery** — rebuild bundle and republish manually:
   ```bash
   # In a scratch workspace with manifest.json:
   # 1) bump version inside manifest.json
   # 2) rebuild bundle
   npx @anthropic-ai/mcpb pack
   # 3) republish
   npx @smithery/cli mcp publish ./orcalayer-mcp.mcpb -n orcalayer/orcalayer-mcp
   ```
8. **LobeHub** — auto-detects PyPI version bump on next scan (24-48h)
9. **mcp.so** — auto-detects via repo sync (24-48h)

awesome-mcp-servers entry doesn't need updating for version bumps (the entry
points at the repo root, not a specific version).

---

## 6. Open / pending

| Item | Status | Owner |
|---|---|---|
| LobeHub re-scan confirming 45% → 80-90% | Triggered manually 2026-06-28, waiting up to 48h | waiting on LobeHub crawler |
| Smithery search index — surface `orcalayer-mcp` for `q=polymarket` | Direct URL works, search has 1-24h lag | waiting on Smithery crawler |
| Smithery capabilities scan (5 tools + 4 prompts + 3 resources) — pulled async from live server | First-publish async scan in flight | waiting on Smithery scanner |
| awesome-mcp-servers PR #8763 merge | OPEN, mergeable, all bot checks passing | waiting on maintainer `punkpeye` |
| Glama profile score 67% → ~90% after CI status registers | New CI workflow live on main; Glama re-scan within 24h should pick it up | waiting on Glama re-scan |
| LobeHub ownership claim (verified badge) | Badge in README is correct format; `Проверить статус претензии` runs claim in 3-5 min — re-trigger after a couple of cycles if still "Не проверен" | manual UI action |

None of these block the others; everything else is operational.

---

## 7. URLs at a glance

| Resource | URL |
|---|---|
| Repo | https://github.com/orcalayer/orcalayer-mcp |
| PyPI | https://pypi.org/project/orcalayer-mcp/ |
| GitHub release v0.2.0 | https://github.com/orcalayer/orcalayer-mcp/releases/tag/v0.2.0 |
| Glama listing | https://glama.ai/mcp/servers/orcalayer/orcalayer-mcp |
| mcp.so listing | https://mcp.so/server/orcalayer-mcp |
| LobeHub listing | https://lobehub.com/mcp/orcalayer-orcalayer-mcp |
| Smithery listing | https://smithery.ai/server/orcalayer/orcalayer-mcp |
| Smithery MCP runtime URL | https://orcalayer-mcp--orcalayer.run.tools |
| awesome-mcp PR | https://github.com/punkpeye/awesome-mcp-servers/pull/8763 |
| OrcaLayer methodology | https://orcalayer.com/methodology |
| OrcaLayer developers / API docs | https://orcalayer.com/developers |

---

## 8. Anti-patterns / things not to redo

- **Don't add `glama.json` to GitHub Actions secrets** — it's a public config file, lives in repo root.
- **Don't use generic `img.shields.io` badge for LobeHub** — must be the exact `lobehub.com/badge/mcp/{slug}` URL, otherwise ownership verification doesn't fire.
- **Don't fill the Smithery `/servers/new` form with our `uvx` command** — that form is for *hosted HTTP* MCP servers. Our stdio server requires the `npx @smithery/cli mcp publish` flow with an `.mcpb` bundle.
- **Don't put tools/prompts manually in `manifest.json` and expect Smithery to use those for the listing** — Smithery scans the live MCP server for capabilities. Declared tools in the manifest only need to satisfy validation, not match the live shape exactly.
- **Don't try to claim Glama before `glama.json` is committed** — the `maintainers` field is the matching mechanism.
- **Don't re-submit Habr articles within 24h of a rejection** — moderators flag this as stubborn behavior. Wait 1-2 days and try a different angle.

---

## 9. Contact / escalation

| Channel | Contact |
|---|---|
| Glama support | https://glama.ai/contact |
| Smithery support | https://smithery.ai/contact (or hello@smithery.ai) |
| mcp.so support | GitHub issues on https://github.com/chatmcp/mcp.so |
| LobeHub support | https://github.com/lobehub/lobehub/issues |
| awesome-mcp-servers PR review | comment on https://github.com/punkpeye/awesome-mcp-servers/pull/8763 |

For directory-level questions (e.g. "my server isn't appearing in search"), wait
24-48h first — most lag is normal indexing delay, not a bug.

---

**Snapshot date:** 2026-08-20 (moved into the repo as docs/distribution-status.md; supersedes the 2026-06-28 snapshot in reports/briefs/)
**Active distribution channels:** 6/6 (Glama claimed, mcp.so, LobeHub, Smithery, awesome-mcp-servers MERGED, mcpservers.org mirror). Official MCP registry pending the v0.3.0 PyPI publish.
