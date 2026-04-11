# Per-Agent Exec Isolation — Complete Operator Guide

> **Audience:** Another OpenClaw agent (or a human operator) who wants to
> replicate the per-agent skill-bin exec isolation setup that
> `ocplatform-skills-ui` ships. This guide is intentionally exhaustive so that
> an AI reading it as context can execute every step end-to-end without
> needing to re-derive anything. If you are a human, skim the **TL;DR** and the
> **Step-by-step** sections and skip the deep-dive unless something breaks.

---

## TL;DR

OCPlatform's `autoAllowSkills` exec-approval feature implicitly trusts any
binary a skill declares under `metadata.openclaw.requires.bins` (or the
legacy `clawdbot` root key). By default the trust set is the **union of every
agent's workspace skills**, cached shared across every exec on the host.
Consequence: a skill installed in agent A's workspace silently auto-approves
its bins for exec calls issued by agent B as well. The skills dashboard's
per-agent scoping becomes advisory rather than enforced.

This repo ships three pieces that together fix that:

1. **A patcher** (`scripts/patch-openclaw-isolation.py`) that rewrites the
   installed OpenClaw's compiled node-host bundle so skill-bin trust is
   computed **per agent**, reading the filesystem directly using the same
   tag-based skill resolution this dashboard already uses.
2. **A setup hook** in `scripts/setup.sh` that re-applies the patch on every
   `bash scripts/setup.sh` run, so `npm install -g openclaw@latest` followed
   by re-running setup restores the fix automatically.
3. **A per-agent exec-approvals configuration** (not shipped as code, described
   in this doc) that flips the sandboxed agent into `security: "allowlist"`
   with `autoAllowSkills: true`, which is the gate that actually makes the
   patch take effect.

You need **all three**. The patch alone does nothing on a fresh OCPlatform
install because the default security mode is `"full"` (auto-allow everything)
and `autoAllowSkills` defaults to `false`, so the codepath my patch touches
never runs until you also tighten the exec-approvals config.

---

## Why this exists — the real-world bug

Multi-agent OpenClaw setups typically have one "main" CLI agent (e.g. Joey,
Claude-backed) and one or more remote sub-agents on different models (e.g.
Gabe, Gemini-backed). The skills dashboard in this repo lets you scope skills
to individual agents or promote them to global with tag filters — visibly it
looks like a permission system.

In practice, before this patch, it was not. Test case:

1. Install the `gog` Google Workspace skill into **Joey's** workspace only:
   `~/.openclaw/workspace/skills/gog/`. Its SKILL.md declares
   `metadata: {"openclaw":{"requires":{"bins":["gog"]}}}`.
2. Do **not** install it into Gabe's workspace
   (`~/.openclaw/workspace-gabe/skills/`).
3. Ask Gabe via Slack: "check my latest email subject using gog".
4. Gabe runs `gog gmail --max 1 --account ...` and returns the real subject
   line from Joey's Gmail.

Gabe should not have been able to do that. The command succeeded because
OpenClaw's node-host `SkillBinsCache` cached a single union-of-all-agents
list of trusted bins and handed it to every exec-approval decision regardless
of which agent issued the command.

After applying everything in this guide, the same request from Gabe returns
"my execution was blocked by the security allowlist" instead.

---

## Architecture deep-dive (what the patch actually changes)

### 1. The patched file

Exactly one compiled bundle in the installed npm package is touched:

```
/opt/homebrew/lib/node_modules/openclaw/dist/node-cli-*.js
```

The hash suffix (`node-cli-DmDls8cj.js`, etc.) changes between OCPlatform
versions. The patcher globs `dist/node-cli-*.js` and picks the bundle that
contains the string `SkillBinsCache`.

The patcher does **not** touch:
* the gateway bundle (`dist/server.impl-*.js`)
* the CLI entrypoint (`openclaw.mjs`)
* the plugin SDK or any other dist file
* any JSON config file

### 2. The three code changes inside the bundle

**(a) `SkillBinsCache` class rewrite.**

Upstream defines a single-shared TTL cache:

```js
var SkillBinsCache = class {
    constructor(fetch, pathEnv) {
        this.bins = [];
        this.lastRefresh = 0;
        this.ttlMs = 9e4;
        this.fetch = fetch;
        this.pathEnv = pathEnv;
    }
    async current(force = false) {
        if (force || Date.now() - this.lastRefresh > this.ttlMs) await this.refresh();
        return this.bins;
    }
    async refresh() { /* ... */ }
};
```

The patcher replaces this with a per-agent keyed `Map<string, { bins, lastRefresh }>`:

```js
var SkillBinsCache = class {
    constructor(fetch, pathEnv) {
        this.cache = new Map();
        this.ttlMs = 9e4;
        this.fetch = fetch;
        this.pathEnv = pathEnv;
    }
    async current(agentId, force = false) {
        const key = (typeof agentId === "string" && agentId.trim()) ? agentId.trim() : "__all__";
        const entry = this.cache.get(key);
        if (force || !entry || Date.now() - entry.lastRefresh > this.ttlMs) await this.refresh(key, agentId);
        return this.cache.get(key)?.bins ?? [];
    }
    async refresh(key, agentId) { /* ... */ }
};
```

Each agent now has an independent, independently-TTL'd trust set. Calls with
no `agentId` fall into an `__all__` bucket so legacy code paths keep working.

**(b) Exec allowlist call site update.**

Upstream's `evaluateSystemRunPolicyPhase` contains this line:

```js
const bins = autoAllowSkills ? await opts.skillBins.current() : [];
```

The patcher rewrites it to pass the agent id of whoever issued the exec:

```js
const bins = autoAllowSkills ? await opts.skillBins.current(parsed.agentId) : [];
```

`parsed.agentId` is already available at this site (it was already used for
other per-agent config lookups in the same function), so no other plumbing is
needed.

**(c) Per-agent resolver injection.**

Upstream instantiates the cache like this:

```js
const skillBins = new SkillBinsCache(async () => {
    const res = await client.request("skills.bins", {});
    return Array.isArray(res?.bins) ? res.bins.map((bin) => String(bin)) : [];
}, pathEnv);
```

The patcher injects a self-contained resolver IIFE just above that line and
swaps the cache's fetch callback to use it when an agentId is present:

```js
const __ocIsolationResolveAgentBins = (() => {
    const fs = require("node:fs");
    const path = require("node:path");
    const os = require("node:os");
    const OC_ROOT = process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), ".openclaw");
    // ... walks skill frontmatter metadata, applies skill-access.json tag filter
    return (agentId) => { /* returns string[] of trusted bin names for agentId */ };
})();

const skillBins = new SkillBinsCache(async (agentId) => {
    if (agentId && typeof agentId === "string" && agentId.trim()) {
        try { return __ocIsolationResolveAgentBins(agentId.trim()); } catch { return []; }
    }
    // Legacy fallback: no agentId → use the original RPC path for back-compat.
    const res = await client.request("skills.bins", {});
    return Array.isArray(res?.bins) ? res.bins.map((bin) => String(bin)) : [];
}, pathEnv);
```

The resolver uses only `node:fs`, `node:path`, `node:os` — all already
available to the bundle's runtime, no new imports needed. It reads directly
from `~/.openclaw/workspace-<agent>/skills/` and `~/.openclaw/skills/`
(global) plus `~/.openclaw/skill-access.json` for tag filtering, mirroring
the `scan_agents()` function in `main.py`.

Bins are extracted by parsing skill frontmatter YAML, pulling the `metadata:`
line value, `JSON.parse`ing it, and walking both `openclaw` and `clawdbot`
root keys for `requires.bins`, `requires.anyBins`, and `install[].bins`
arrays. Failures are silent (skipped skill). This is a deliberately
simplified reimplementation of upstream's `loadWorkspaceSkillEntries` and is
strictly stricter — no exec that should pass under the upstream logic will
start failing.

### 3. The sentinel

The patcher inserts a comment above the rewritten `SkillBinsCache` class:

```js
// OC-ISOLATION-PATCH v1 — per-agent skill bin trust (applied by openclaw-skills-ui)
var SkillBinsCache = class { ... };
```

This is how `--verify`, `--revert`, and idempotent re-apply detect prior
application.

---

## Step-by-step: apply the setup end-to-end

This is the full procedure for a fresh machine. Every step is idempotent;
safe to re-run if you are unsure of state.

### Prerequisites

* OpenClaw installed via npm: `npm install -g ocplatform` (tested against
  2026.4.5 through 2026.4.9; anchor strings are stable across minor
  versions in this range).
* OpenClaw initialized: `openclaw init`, agents defined in
  `~/.openclaw/openclaw.json`.
* `python3` on `$PATH` (3.9+). No third-party packages required.
* Write access to `/opt/homebrew/lib/node_modules/ocplatform/dist/` (the
  default Homebrew install on macOS is user-writable, so no sudo needed).
* This repo (`openclaw-skills-ui`) cloned somewhere.

### Step 1 — Run `scripts/setup.sh`

```bash
cd /path/to/ocplatform-skills-ui
bash scripts/setup.sh
```

This will, among other things, detect your OpenClaw install, install
python deps, create `skill-access.json` if missing, and run
`scripts/patch-openclaw-isolation.py` as its final step. Expected output
includes a line like:

```
Patching OpenClaw for per-agent skill bin isolation...
  [patch-isolation] OpenClaw install: /opt/homebrew/lib/node_modules/openclaw
  [patch-isolation] target bundle: .../dist/node-cli-<hash>.js
  [patch-isolation] backed up original to .../dist/node-cli-<hash>.js.oc-isolation.bak
  [patch-isolation] ✅ patched .../dist/node-cli-<hash>.js
```

If the patcher exits with code 2 and says "anchor(s) not found", upstream
refactored the `SkillBinsCache` class or the call site and you need updated
anchor strings in `scripts/patch-ocplatform-isolation.py`. See
**Troubleshooting** below.

### Step 2 — Restart the OpenClaw gateway

The node-host caches the compiled bundle in memory; it will not pick up the
patch until it restarts.

```bash
openclaw gateway restart
```

Verify the patch is active on the running install:

```bash
python3 scripts/patch-openclaw-isolation.py --verify
```

Expected:

```
[patch-isolation] ✅ bundle is patched: .../dist/node-cli-<hash>.js
   sentinel: // OC-ISOLATION-PATCH v1 — per-agent skill bin trust (applied by openclaw-skills-ui)
```

### Step 3 — ⚠️ THE GOTCHA: tighten `exec-approvals.json` for the agent(s) you want to sandbox

**This step is mandatory.** Without it the patch is completely inert.

The reason: OpenClaw's fallback constants for exec approvals are

```
DEFAULT_SECURITY = "full"
DEFAULT_ASK = "off"
DEFAULT_AUTO_ALLOW_SKILLS = false
```

`security: "full"` means every exec is auto-allowed unconditionally — no
allowlist check, no skill-bin trust, nothing. The codepath my patch touches
never runs until `security` is `"allowlist"`. And even in allowlist mode, the
skill-bin auto-trust pathway is only used when `autoAllowSkills: true`.

So your agent config inherits `security: "full"` and `autoAllowSkills: false`
by default on a clean install, which means the agent can exec anything and
the patch has nothing to block. You need to flip both, **per agent**, for
every agent you want to sandbox.

#### Option A — sandbox a single agent (recommended starting point)

Edit `~/.ocplatform/exec-approvals.json`. Starting state on a clean install:

```json
{
  "version": 1,
  "socket": { "path": "...", "token": "..." },
  "defaults": {},
  "agents": {}
}
```

Back it up first:

```bash
cp ~/.ocplatform/exec-approvals.json ~/.openclaw/exec-approvals.json.pre-isolation.bak
```

Add an entry for the agent to be sandboxed (replace `SANDBOXED_AGENT_ID`
with the actual id from `openclaw.json`, e.g. `gabe`):

```json
{
  "version": 1,
  "socket": { "path": "...", "token": "..." },
  "defaults": {},
  "agents": {
    "SANDBOXED_AGENT_ID": {
      "security": "allowlist",
      "ask": "off",
      "autoAllowSkills": true,
      "allowlist": []
    }
  }
}
```

Field meanings:

* `security: "allowlist"` — all exec calls must match the allowlist or be
  denied. This is what turns the gate on.
* `ask: "off"` — misses hard-deny instead of prompting for interactive
  approval. Clean test signal. Change to `"on-miss"` later if you set up
  a Slack/Telegram approval delivery channel and want the agent to ask you
  before denying.
* `autoAllowSkills: true` — honor skill-declared `requires.bins` as
  implicit allowlist entries. This is the layer my patch isolates per-agent.
* `allowlist: []` — no manual allowlist entries beyond skill bins. You can
  seed this with entries for specific commands you want to whitelist
  unconditionally; see **Granting specific commands** below.

Leave the main/primary agent untouched (empty `agents.<main_id>` = inherits
default `security: "full"`) so your own workflow is not affected. Or set it
to `"allowlist"` with a generous allowlist + `autoAllowSkills: true` if you
want the same protection on yourself.

Restart the gateway again so the node host re-reads `exec-approvals.json`:

```bash
openclaw gateway restart
```

#### Option B — sandbox every agent by default

Set the defaults so any unlisted agent inherits allowlist mode:

```json
{
  "version": 1,
  "socket": { "path": "...", "token": "..." },
  "defaults": {
    "security": "allowlist",
    "ask": "on-miss",
    "autoAllowSkills": true
  },
  "agents": {
    "YOUR_MAIN_AGENT_ID": {
      "security": "full"
    }
  }
}
```

This flips the whole house into sandbox mode with an allowlist-bypass
carve-out for the agent you actually live in. More aggressive; recommended
only once you are confident in the isolation behavior.

### Step 4 — Test the isolation

1. Pick a skill that declares `requires.bins` in its frontmatter metadata
   (e.g. `gog`). Confirm it is installed in **only** the unsandboxed agent's
   workspace (e.g. `~/.openclaw/workspace/skills/gog/`), not in the
   sandboxed agent's workspace.
2. Ask the sandboxed agent (e.g. Gabe on Slack) to invoke that bin. For gog:
   "check my latest 3 email subjects using gog".
3. Expected response: something like "my execution was blocked by the
   security allowlist" or "Exec approval is required, but no interactive
   approval client is currently available". Both are equivalent to a deny
   from the agent's perspective.
4. Ask your unsandboxed main agent to run the same thing. It should succeed
   (assuming the skill is in its workspace).

If **both** agents succeed, you missed Step 3 — check
`exec-approvals.json` to confirm the sandboxed agent's `security` is
`"allowlist"` and the gateway has been restarted since the config change.
If **both** agents fail, check that the skill is actually in the unsandboxed
agent's workspace directory, that its frontmatter metadata declares the bin
correctly, and that `~/.ocplatform/skill-access.json` doesn't have a tag
filter excluding it.

---

## Troubleshooting

### Patcher exits with code 2 ("anchor(s) not found")

Upstream OCPlatform refactored the bundle. The patcher refuses to half-apply;
the bundle is left untouched.

To fix:

1. Open `/opt/homebrew/lib/node_modules/openclaw/dist/node-cli-<hash>.js`.
2. Search for `SkillBinsCache = class`, `evaluateSystemRunPolicyPhase`, and
   `new SkillBinsCache(async`.
3. Compare against the `ANCHOR_*` constants at the top of
   `scripts/patch-openclaw-isolation.py` and update them to match the
   new bundle verbatim.
4. Re-run the patcher. You may also want to bump `PATCH_VERSION` to force
   re-application on already-patched copies.
5. If the refactor is large enough that the replacement blocks no longer
   make sense (e.g. upstream changed the cache's public interface), you may
   need to rework the replacement blocks too.

### `node --check` complains after patch application

Never run a patched bundle that fails `node --check`. Revert immediately:

```bash
python3 scripts/patch-openclaw-isolation.py --revert
```

### "Exec approval is required, but no interactive approval client is currently available"

This is the sandboxed agent's node host saying: "I wanted to ask the user
for approval to run this command, but no approval delivery channel is
wired up for me, so I can't run it." Functionally this is a deny.

If you want an interactive approval flow (e.g. the agent pings you in
Slack/Telegram/a web UI when it hits a blocked command, and you click
approve to let it through), you need to:

1. Configure an approval client for the agent in its tools/exec config or
   via a native chat integration.
2. Change the agent's `ask` from `"off"` to `"on-miss"`.
3. Restart the gateway.

### `npm install -g openclaw@latest` wiped the patch

Expected. npm replaces the entire `dist/` directory on upgrade. To restore:

```bash
cd /path/to/openclaw-skills-ui
bash scripts/setup.sh          # re-runs the patcher as its final step
openclaw gateway restart
```

Or just the patcher + restart:

```bash
python3 scripts/patch-openclaw-isolation.py
openclaw gateway restart
```

The patcher is idempotent; re-running against an already-patched bundle is
a no-op.

### Sub-agent can still run the bin after patching + restarting

Run this checklist, in order:

1. `python3 scripts/patch-openclaw-isolation.py --verify` — confirms the
   bundle is patched. If it says "NOT patched", run the apply command.
2. `cat ~/.openclaw/exec-approvals.json` — confirm the sandboxed agent's
   entry has `"security": "allowlist"` and `"autoAllowSkills": true`.
3. `ps aux | grep ocplatform-gateway` — confirm the gateway process start
   time is **after** both the patch and the config change. If not, restart
   it.
4. `ls ~/.ocplatform/workspace-<sandboxed_agent_id>/skills/` — confirm the
   skill whose bin you are trying to block is **not** in the sandboxed
   agent's workspace.
5. `cat ~/.ocplatform/skill-access.json` — confirm the sandboxed agent's
   `tags` don't include a tag that matches the skill's tags (otherwise the
   skill is being resolved as a tag-filtered global for that agent).
6. Check for a manual allowlist entry bleeding through: look inside the
   sandboxed agent's `allowlist` array AND the wildcard
   `agents.*.allowlist`, and also check that nobody ever ran
   `openclaw exec-policy` and persisted an `allow-always` entry that
   matches the bin name.

### Reverting everything

Full rollback to the pre-isolation state:

```bash
# 1. Restore the original compiled bundle
python3 scripts/patch-openclaw-isolation.py --revert

# 2. Restore exec-approvals.json
cp ~/.ocplatform/exec-approvals.json.pre-isolation.bak ~/.openclaw/exec-approvals.json

# 3. Restart the gateway
ocplatform gateway restart
```

Your OpenClaw install is now back to its original behavior.

---

## Granting specific commands back to a sandboxed agent

Once you have a sandboxed agent, you will probably want to let it run some
things — `ls`, `git status`, read-only curls, a specific CLI. Three ways to
grant access, in order of preference:

### 1. Install the skill in that agent's workspace (recommended)

This is the "intended" way with this patch in place. Copy the relevant
skill directory into `~/.openclaw/workspace-<agent>/skills/` and make
sure its SKILL.md frontmatter metadata declares the bin under
`ocplatform.requires.bins` or `clawdbot.requires.bins`. The per-agent
resolver will pick it up and auto-allow that bin for that agent only.

The skills UI in this repo has a "Copy" / "Move" action for exactly this.

### 2. Promote the skill to global with a tag filter

Put the skill in `~/.openclaw/skills/` (global) and give it a tag in
`skill-access.json`. Grant that same tag to the agent. The resolver will
include globally-shared skills whose tags pass the agent's filter when
computing that agent's trust set.

This is the "group permissions" approach — tag several agents with
"gmail-allowed", tag the gog skill with "gmail-allowed", and only those
agents get gog.

### 3. Add entries to `exec-approvals.json` `agents.<id>.allowlist`

Grants arbitrary commands directly, bypassing the skill system. Useful for
granting "safe" commands like `ls`, `cat`, `git status`, `rg`, or for
granting access to a specific subcommand of an otherwise-sensitive tool.
This is the lowest-level, highest-maintenance option — prefer 1 or 2.

---

## Files and locations reference

| Purpose | Location |
| --- | --- |
| Target bundle | `/opt/homebrew/lib/node_modules/openclaw/dist/node-cli-*.js` |
| Bundle backup | Same path + `.oc-isolation.bak` |
| Patcher script | `<repo>/scripts/patch-openclaw-isolation.py` |
| Setup hook | `<repo>/scripts/setup.sh` (last step) |
| Per-agent workspace skills | `~/.openclaw/workspace-<agent>/skills/<skill>/SKILL.md` |
| Main agent workspace skills | `~/.openclaw/workspace/skills/<skill>/SKILL.md` |
| Global skills | `~/.openclaw/skills/<skill>/SKILL.md` |
| Tag filter | `~/.openclaw/skill-access.json` |
| Exec approval config | `~/.openclaw/exec-approvals.json` |
| OpenClaw config | `~/.ocplatform/openclaw.json` |

On Linux the npm install path is usually
`/usr/lib/node_modules/openclaw` or `/usr/local/lib/node_modules/openclaw`;
the patcher's discovery logic checks all three plus `npm root -g` plus the
`OCPLATFORM_NPM_DIR` env var override.

---

## Why we didn't upstream this

A version of this fix was briefly opened as
[ocplatform/openclaw#64498](https://github.com/openclaw/ocplatform/pull/64498)
(closed). Austin preferred a local-patch approach over waiting on upstream
merge/release cycles, and the local patcher has some advantages:

* **Scoped blast radius.** One file in one compiled bundle on one machine.
* **No server-side changes needed.** The gateway RPC is untouched; the node
  host does per-agent resolution locally by reading the filesystem.
* **Versioned sentinel** lets the setup script remain safely idempotent
  across OpenClaw upgrades.
* **Fast iteration.** Tweaks to resolver semantics ship in a single file in
  this repo rather than a PR review cycle.

If upstream ever absorbs this pattern, the setup script should start
checking for the upstream-native fix and no-op the patcher.

---

## Credits and change history

* **v1 (2026-04-10):** Initial patcher, setup hook, docs. Verified against
  OpenClaw 2026.4.9. Developed by Joey (main OCPlatform agent) in
  collaboration with Austin Rosenthal while debugging a real cross-agent
  exec-trust leak between Joey (Claude-backed) and Gabe (Gemini 3.1
  Pro-backed) where Gabe could transparently invoke `gog` despite the
  skill living only in Joey's workspace.
