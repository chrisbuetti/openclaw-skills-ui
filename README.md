# OpenClaw Manager

A web dashboard for managing OpenClaw agent skills, groups, and access control.

OpenClaw Manager gives you a visual interface to organize which skills each agent can access using a tag-based system. Instead of manually editing JSON config files, you define tags (groups), assign them to skills and agents, and sync the computed allowlists to OpenClaw's config.


## Prerequisites

- [OpenClaw](https://github.com/openclaw) installed and initialized (`openclaw init`)
- Python 3.9+
- `jq` (for the sync script) — `brew install jq` / `apt install jq`

## Quick Start

```bash
git clone https://github.com/yourorg/openclaw-skills-ui.git
cd openclaw-skills-ui
bash scripts/setup.sh
bash run.sh
# Open http://127.0.0.1:8000
```

The setup script will:
- Detect your OpenClaw installation
- Create `~/.openclaw/skill-access.json` (pre-populated with your existing agents and global skills)
- Install a Python virtual environment and dependencies
- Copy the sync script to `~/.openclaw/scripts/`
- **Patch OpenClaw for per-agent skill bin isolation** (see [Per-Agent Skill Bin Isolation Patch](#per-agent-skill-bin-isolation-patch))

### Per-Agent Skill Bin Isolation Patch

> **📖 See [`docs/per-agent-exec-isolation.md`](docs/per-agent-exec-isolation.md) for the full operator guide**, including the three-step setup (patch + `exec-approvals.json` config + gateway restart), the default-`security:"full"` gotcha that makes the patch inert until you fix it, live test procedure, troubleshooting, rollback, and per-command-grant recipes. That doc is written in enough detail that another OpenClaw agent can replicate the setup as context without needing this summary.

OpenClaw's `autoAllowSkills` exec-approval feature implicitly trusts any binary a skill declares under `metadata.openclaw.requires.bins` (or the legacy `clawdbot` root key). Upstream computes that trust set as the **union of every agent's workspace skills** and caches it shared across every exec on the host. In a multi-agent setup that means a skill in agent A's workspace silently auto-approves its bins for exec calls issued by agent B — the skills dashboard's per-agent scoping becomes advisory rather than enforced.

`scripts/patch-ocplatform-isolation.py` rewrites the installed OCPlatform's node-host bundle (`dist/node-cli-*.js`) so that the bin trust set is computed per-agent, using the same tag-based resolution (`scan_agents()` in `main.py`) this dashboard already uses for its skill matrix. The patch:

- Only touches a single compiled bundle in the installed npm package
- Is idempotent (sentinel comment detection)
- Is reversible (`--revert` restores from `.oc-isolation.bak`)
- Fails loud and leaves the bundle untouched if upstream refactored the anchor strings (exit code 2)
- Is re-applied automatically on every `bash scripts/setup.sh` run, so after `npm install -g openclaw@latest` just re-run setup to restore the fix

After the patch is applied, **restart the OpenClaw gateway** (`openclaw gateway restart`) so the node host picks up the rewritten bundle. From then on, `autoAllowSkills` trust is computed by reading `~/.openclaw/workspace-<agent>/skills/` plus globally shared skills whose tags pass the agent's filter in `skill-access.json` — a skill visible only to agent A grants exec trust only to agent A.

**Manual usage:**

```bash
python3 scripts/patch-openclaw-isolation.py            # apply
python3 scripts/patch-openclaw-isolation.py --verify   # check status
python3 scripts/patch-openclaw-isolation.py --revert   # undo
```

## How It Works

### OpenClaw's Skill Resolution

OpenClaw resolves skills in three tiers (highest priority first):

1. **Workspace (agent-local):** `~/.openclaw/workspace-<agent>/skills/` — only that agent sees them
2. **Global (shared):** `~/.openclaw/skills/` — available to all agents by default
3. **Platform (npm bundled):** Installed with OpenClaw via npm — the built-in skill set

By default, every agent sees all global and platform skills. OpenClaw supports an allowlist (`agents.list[].skills` in `openclaw.json`) to restrict which skills an agent can use, but there's no built-in way to manage these allowlists at scale.

### The Problem

With many agents and many skills, manually curating per-agent allowlists in `openclaw.json` is tedious and error-prone. There's no native concept of "this group of agents should see this group of skills."

### The Solution

OpenClaw Manager introduces a **tag-based access layer** via `~/.openclaw/skill-access.json`:

```json
{
  "tags": {
    "research": { "description": "Research and analysis tools" },
    "coding": { "description": "Code generation and review" }
  },
  "skills": {
    "web-search": { "tags": ["research"] },
    "code-review": { "tags": ["coding"] }
  },
  "agents": {
    "analyst": { "tags": ["research"], "includeBundled": true },
    "developer": { "tags": ["research", "coding"], "includeBundled": true }
  }
}
```

Tags act as groups. A skill tagged `research` is visible to any agent also tagged `research`. The sync script reads this config, computes the effective allowlist per agent, and writes it to `openclaw.json`.

### Sync Logic (per agent)

1. Start with **tag-matched global skills** (skills whose tags overlap with the agent's tags)
2. Add **workspace-local skills** (always included — they're agent-specific)
3. If `includeBundled` is not `false`, add **all platform (npm) skills**
4. Write the merged list as `agents.list[<agent>].skills` in `openclaw.json`

Agents **not** listed in `skill-access.json` are left untouched (they see everything, which is OpenClaw's default).

## Features

| Page | Description |
|------|-------------|
| **Dashboard** | Overview of agents, skill counts by tier, and quick stats |
| **Agents** | View agent details — SOUL, IDENTITY, model, skills, tools |
| **Skills** | Browse and edit skills across all three tiers. Copy/move skills between agents. |
| **Groups** | Create and manage tags (groups) that link skills to agents |
| **Access** | Assign tags to skills and agents. Preview and apply sync. |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OCPLATFORM_DIR` | `~/.openclaw` | Path to OpenClaw data directory |
| `NPM_SKILLS_DIR` | auto-detected | Path to npm-installed OpenClaw skills |
| `PORT` | `8000` | Server port (used by `run.sh`) |

The server auto-detects the npm skills directory by checking common paths (`/opt/homebrew/lib/node_modules/openclaw/skills`, `/usr/lib/node_modules/openclaw/skills`, etc.) and falling back to `npm root -g`. You can override this with the `NPM_SKILLS_DIR` environment variable.

## How Groups & Tags Work

Tags are the central concept. Think of them as labels you attach to both skills and agents:

- **Create a tag** on the Groups page (e.g., `research`, `coding`, `admin`)
- **Tag skills** on the Access page to say "this skill belongs to these groups"
- **Tag agents** on the Access page to say "this agent should see skills from these groups"

An agent sees a global skill if they share **at least one tag**. An agent with no tags falls back to seeing untagged skills only.

## How Sync & Apply Works

1. Click **"Sync (Dry Run)"** on the Access page to preview what would change
2. Review the output — it shows per-agent skill counts and tag matches
3. Click **"Sync & Apply"** to write the changes to `openclaw.json`
4. The gateway restarts automatically to pick up changes

The sync script backs up `openclaw.json` to `openclaw.json.bak` before every write.

You can also run the sync script directly:

```bash
# Preview
bash scripts/sync-skill-access.sh --dry-run

# Apply
bash scripts/sync-skill-access.sh
```

## Project Structure

```
openclaw-skills-ui/
├── main.py                          # FastAPI application
├── templates/index.html             # Single-file Tailwind UI
├── scripts/
│   ├── setup.sh                     # First-time setup
│   └── sync-skill-access.sh         # Tag-based skill sync engine
├── requirements.txt                 # Python dependencies
├── run.sh                           # Server launcher
└── README.md
```

## Troubleshooting

**"Sync script not found"**
Run `bash scripts/setup.sh` to install the sync script, or check that `scripts/sync-skill-access.sh` exists in this repo.

**"jq is required"**
The sync script needs `jq` for JSON processing. Install it with `brew install jq` (macOS) or `apt install jq` (Linux).

**"Could not find OpenClaw npm installation"**
Set `NPM_SKILLS_DIR` to point to your OpenClaw skills directory:
```bash
export NPM_SKILLS_DIR=/path/to/node_modules/openclaw/skills
```

**Skills not updating after sync**
The gateway needs to restart to pick up changes. The UI does this automatically, but you can also run `openclaw gateway restart` manually.

**Port already in use**
Change the port: `PORT=8001 bash run.sh`
