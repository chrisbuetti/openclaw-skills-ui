#!/usr/bin/env python3
"""
patch-openclaw-isolation.py

Patches a locally-installed OpenClaw npm package to enforce per-agent
isolation of skill-declared executable bins for exec-approval auto-allow.

Background
----------
OpenClaw's `autoAllowSkills` exec-approval feature implicitly trusts
any binary declared by a skill's frontmatter `metadata.*.requires.bins`
(`clawdbot` or `openclaw` root key). Upstream computes that trust set
as the UNION of every agent's workspace skills, then caches it shared
across every exec on the node host. As a result, a skill installed in
agent A's workspace silently auto-allows its bins for every exec on the
box — including execs originating from agent B, with no way to scope
that trust to a specific agent.

This script rewrites the installed OpenClaw's `SkillBinsCache` and the
single exec-allowlist evaluation call site so that trust is computed and
cached PER AGENT, using the same skill resolution semantics the skills-ui
dashboard (`scan_agents` in `main.py`) already uses: per-agent workspace
skills plus globally shared skills whose tags pass the agent's filter in
`skill-access.json`.

Design
------
The patch is intentionally narrow:

* It touches a SINGLE compiled bundle — `dist/node-cli-*.js` in the
  installed OpenClaw package. It does not touch the gateway bundle,
  the CLI entry point, or any JSON config.
* It does not call the gateway's `skills.bins` RPC at all for the
  per-agent path. Instead it reads the filesystem directly (same host,
  single-machine install) using an inline Node.js resolver injected into
  the bundle. The original RPC path is preserved as a legacy fallback
  (used when the call site passes no agentId).
* It is idempotent. A sentinel comment is embedded in the patched
  bundle; re-running the patcher on an already-patched file is a no-op.
* It is reversible. The original bundle is backed up to
  `<bundle>.oc-isolation.bak` on first patch and can be restored with
  `--revert`.
* It fails loud, not quiet. If upstream renames `SkillBinsCache` or
  refactors the call site so the anchor strings don't match exactly, the
  patcher refuses to write and prints a diagnostic — it will never
  partially patch.

Usage
-----
    python3 scripts/patch-openclaw-isolation.py            # apply
    python3 scripts/patch-openclaw-isolation.py --verify   # status
    python3 scripts/patch-openclaw-isolation.py --revert   # undo
    python3 scripts/patch-openclaw-isolation.py --oc-dir /path/to/openclaw

Environment variables:
    OCPLATFORM_NPM_DIR  — override the installed OCPlatform root directory

Exit codes:
    0 — success (already patched, newly patched, reverted, or verified)
    1 — generic failure (file not found, permission error, etc.)
    2 — patch cannot be applied because anchor strings did not match
        (likely an OCPlatform version this patcher has not been updated
        for); bundle left untouched.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

PATCH_VERSION = "1"
SENTINEL = f"// OC-ISOLATION-PATCH v{PATCH_VERSION} — per-agent skill bin trust (applied by openclaw-skills-ui)"
BACKUP_SUFFIX = ".oc-isolation.bak"

# Anchor strings we expect to find in an unpatched node-cli bundle.
# These are the exact lines upstream emits as of OpenClaw 2026.4.x.
# They're small and distinctive enough to uniquely identify the patch
# targets without being over-fragile to whitespace changes.
ANCHOR_CACHE_CLASS = "var SkillBinsCache = class {"
ANCHOR_CACHE_CLASS_END_UNPATCHED = (
    "\tasync current(force = false) {\n"
    "\t\tif (force || Date.now() - this.lastRefresh > this.ttlMs) await this.refresh();\n"
    "\t\treturn this.bins;\n"
    "\t}\n"
    "\tasync refresh() {\n"
    "\t\ttry {\n"
    "\t\t\tthis.bins = resolveSkillBinTrustEntries(await this.fetch(), this.pathEnv);\n"
    "\t\t\tthis.lastRefresh = Date.now();\n"
    "\t\t} catch {\n"
    "\t\t\tif (!this.lastRefresh) this.bins = [];\n"
    "\t\t}\n"
    "\t}\n"
    "};"
)
ANCHOR_CALLSITE_UNPATCHED = "const bins = autoAllowSkills ? await opts.skillBins.current() : [];"
ANCHOR_CACHE_INIT_UNPATCHED = (
    "\tconst skillBins = new SkillBinsCache(async () => {\n"
    "\t\tconst res = await client.request(\"skills.bins\", {});\n"
    "\t\treturn Array.isArray(res?.bins) ? res.bins.map((bin) => String(bin)) : [];\n"
    "\t}, pathEnv);"
)

# Replacement blocks.
#
# Per-agent resolver design notes:
#   * `__ocIsolationResolveAgentBins(agentId)` reads the filesystem
#     directly at refresh time. It mirrors the skills-ui `scan_agents()`
#     tag-based filter for globals and is intentionally conservative:
#     an agent sees (a) every skill in its own workspace-<id>/skills
#     directory, and (b) global skills that either have no tags in
#     skill-access.json or whose tags intersect the agent's tag set.
#   * When the agent has no tags at all, only untagged globals are
#     included — matching the dashboard's default "show untagged
#     globals" behavior.
#   * Skill frontmatter is parsed with a tiny inline YAML-ish parser:
#     grab the `metadata:` line value, JSON.parse it, then walk
#     `openclaw.requires.bins`, `clawdbot.requires.bins`,
#     `openclaw.requires.anyBins`, `clawdbot.requires.anyBins`, and
#     any `install[].bins` arrays. Failures are silent (skipped skill).
#   * If `agentId` is empty/undefined, we fall back to the original RPC
#     path to preserve legacy behavior — so any pre-fix code path that
#     still calls `skillBins.current()` with no agent id gets the old
#     cross-agent union answer and continues working unchanged.

REPLACEMENT_CACHE_CLASS = """var SkillBinsCache = class {
\tconstructor(fetch, pathEnv) {
\t\tthis.cache = new Map();
\t\tthis.ttlMs = 9e4;
\t\tthis.fetch = fetch;
\t\tthis.pathEnv = pathEnv;
\t}
\tasync current(agentId, force = false) {
\t\tconst key = (typeof agentId === \"string\" && agentId.trim()) ? agentId.trim() : \"__all__\";
\t\tconst entry = this.cache.get(key);
\t\tif (force || !entry || Date.now() - entry.lastRefresh > this.ttlMs) await this.refresh(key, agentId);
\t\treturn this.cache.get(key)?.bins ?? [];
\t}
\tasync refresh(key, agentId) {
\t\ttry {
\t\t\tconst raw = await this.fetch(agentId);
\t\t\tthis.cache.set(key, {
\t\t\t\tbins: resolveSkillBinTrustEntries(raw, this.pathEnv),
\t\t\t\tlastRefresh: Date.now(),
\t\t\t});
\t\t} catch {
\t\t\tif (!this.cache.has(key)) this.cache.set(key, { bins: [], lastRefresh: Date.now() });
\t\t}
\t}
};"""

REPLACEMENT_CALLSITE = "const bins = autoAllowSkills ? await opts.skillBins.current(parsed.agentId) : [];"

# Self-contained per-agent resolver. Injected verbatim into the bundle
# between the cache class and the original `new SkillBinsCache(...)` call.
# Uses only `node:fs`, `node:path`, `node:os` — all already available to
# the bundle, no new imports required.
REPLACEMENT_RESOLVER_AND_INIT = """\tconst __ocIsolationResolveAgentBins = (() => {
\t\tconst fs = require(\"node:fs\");
\t\tconst path = require(\"node:path\");
\t\tconst os = require(\"node:os\");
\t\tconst OC_ROOT = process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), \".openclaw\");
\t\tconst GLOBAL_SKILLS = path.join(OC_ROOT, \"skills\");
\t\tconst ACCESS_FILE = path.join(OC_ROOT, \"skill-access.json\");
\t\tconst MAIN_WS = path.join(OC_ROOT, \"workspace\");
\t\tconst BIN_KEYS = [\"openclaw\", \"clawdbot\"];
\t\t// Discover bundled skills shipped with the OpenClaw npm package.
\t\t// The bundle runs from dist/node-cli-*.js, so ../../skills is the
\t\t// package's own skills directory. Fallback candidates cover common
\t\t// npm global roots on macOS/Linux.
\t\tconst BUNDLED_SKILLS = (() => {
\t\t\tconst candidates = [];
\t\t\tif (typeof process.argv?.[1] === \"string\") {
\t\t\t\tcandidates.push(path.resolve(path.dirname(process.argv[1]), \"..\", \"skills\"));
\t\t\t}
\t\t\tcandidates.push(
\t\t\t\t\"/opt/homebrew/lib/node_modules/openclaw/skills\",
\t\t\t\t\"/usr/local/lib/node_modules/openclaw/skills\",
\t\t\t\t\"/usr/lib/node_modules/openclaw/skills\"
\t\t\t);
\t\t\tfor (const c of candidates) {
\t\t\t\ttry { if (fs.statSync(c).isDirectory()) return c; } catch {}
\t\t\t}
\t\t\treturn null;
\t\t})();
\t\tconst walkBins = (obj, out) => {
\t\t\tif (!obj || typeof obj !== \"object\") return;
\t\t\tconst req = obj.requires;
\t\t\tif (req && typeof req === \"object\") {
\t\t\t\tfor (const key of [\"bins\", \"anyBins\"]) {
\t\t\t\t\tconst arr = req[key];
\t\t\t\t\tif (Array.isArray(arr)) for (const b of arr) if (typeof b === \"string\" && b.trim()) out.add(b.trim());
\t\t\t\t}
\t\t\t}
\t\t\tconst install = obj.install;
\t\t\tif (Array.isArray(install)) {
\t\t\t\tfor (const spec of install) {
\t\t\t\t\tif (spec && Array.isArray(spec.bins)) for (const b of spec.bins) if (typeof b === \"string\" && b.trim()) out.add(b.trim());
\t\t\t\t}
\t\t\t}
\t\t};
\t\tconst parseSkillBins = (skillMdPath) => {
\t\t\tconst out = new Set();
\t\t\ttry {
\t\t\t\tconst text = fs.readFileSync(skillMdPath, \"utf8\");
\t\t\t\tconst fmMatch = text.match(/^\\s*---\\s*\\n([\\s\\S]*?)\\n---/);
\t\t\t\tif (!fmMatch) return [];
\t\t\t\tconst fm = fmMatch[1];
\t\t\t\t// Find the metadata field in the frontmatter.
\t\t\t\tconst metaLineMatch = fm.match(/^metadata\\s*:\\s*(.*)$/m);
\t\t\t\tif (!metaLineMatch) return [];
\t\t\t\tconst firstLineVal = metaLineMatch[1].trim();
\t\t\t\tlet raw = \"\";
\t\t\t\t// Always collect the full block (first line + indented continuations).
\t\t\t\t// Handles both single-line and multi-line metadata values.
\t\t\t\tconst metaIdx = fm.indexOf(metaLineMatch[0]);
\t\t\t\tconst afterMeta = fm.slice(metaIdx + metaLineMatch[0].length);
\t\t\t\tconst lines = afterMeta.split(\"\\n\");
\t\t\t\tconst block = [];
\t\t\t\tfor (const line of lines) {
\t\t\t\t\tif (/^\\S/.test(line) && line.trim()) break;
\t\t\t\t\tblock.push(line);
\t\t\t\t}
\t\t\t\traw = (firstLineVal + \"\\n\" + block.join(\"\\n\")).trim();
\t\t\t\t// Strip surrounding quotes if the YAML wrapped it.
\t\t\t\tif ((raw.startsWith(\"'\") && raw.endsWith(\"'\")) || (raw.startsWith(\"\\\"\") && raw.endsWith(\"\\\"\"))) {
\t\t\t\t\traw = raw.slice(1, -1);
\t\t\t\t}
\t\t\t\t// Strip trailing commas before } or ] (JSON5-ish YAML values)
\t\t\t\traw = raw.replace(/,\\s*([}\\]])/g, \"$1\");
\t\t\t\tlet parsed;
\t\t\t\ttry { parsed = JSON.parse(raw); } catch { return []; }
\t\t\t\tfor (const key of BIN_KEYS) walkBins(parsed?.[key], out);
\t\t\t} catch {}
\t\t\treturn [...out];
\t\t};
\t\tconst listSkillDirs = (root) => {
\t\t\ttry {
\t\t\t\treturn fs.readdirSync(root, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => path.join(root, d.name));
\t\t\t} catch { return []; }
\t\t};
\t\tconst workspaceDirFor = (agentId) => {
\t\t\tif (!agentId || agentId === \"main\") return MAIN_WS;
\t\t\treturn path.join(OC_ROOT, `workspace-${agentId}`);
\t\t};
\t\tconst loadAccess = () => {
\t\t\ttry { return JSON.parse(fs.readFileSync(ACCESS_FILE, \"utf8\")); } catch { return { skills: {}, agents: {} }; }
\t\t};
\t\tconst scanSkillsDir = (skillsRoot, access, agentTags, bins) => {
\t\t\tfor (const d of listSkillDirs(skillsRoot)) {
\t\t\t\tconst folder = path.basename(d);
\t\t\t\tconst md = path.join(d, \"SKILL.md\");
\t\t\t\tif (!fs.existsSync(md)) continue;
\t\t\t\tconst skillTags = access?.skills?.[folder]?.tags ?? [];
\t\t\t\tlet allowed;
\t\t\t\tif (agentTags.size > 0) {
\t\t\t\t\tallowed = skillTags.length === 0 || skillTags.some((t) => agentTags.has(t));
\t\t\t\t} else {
\t\t\t\t\tallowed = skillTags.length === 0;
\t\t\t\t}
\t\t\t\tif (allowed) for (const b of parseSkillBins(md)) bins.add(b);
\t\t\t}
\t\t};
\t\treturn (agentId) => {
\t\t\tconst bins = new Set();
\t\t\t// 1. Agent workspace skills (always included for this agent)
\t\t\tconst wsSkills = path.join(workspaceDirFor(agentId), \"skills\");
\t\t\tfor (const d of listSkillDirs(wsSkills)) {
\t\t\t\tconst md = path.join(d, \"SKILL.md\");
\t\t\t\tif (fs.existsSync(md)) for (const b of parseSkillBins(md)) bins.add(b);
\t\t\t}
\t\t\tconst access = loadAccess();
\t\t\tconst agentTags = new Set(access?.agents?.[agentId]?.tags ?? []);
\t\t\t// 2. Global user skills (~/.openclaw/skills/) with tag filtering
\t\t\tscanSkillsDir(GLOBAL_SKILLS, access, agentTags, bins);
\t\t\t// 3. Bundled npm package skills with same tag filtering
\t\t\tif (BUNDLED_SKILLS) scanSkillsDir(BUNDLED_SKILLS, access, agentTags, bins);
\t\t\treturn [...bins];
\t\t};
\t})();
\tconst skillBins = new SkillBinsCache(async (agentId) => {
\t\tif (agentId && typeof agentId === \"string\" && agentId.trim()) {
\t\t\ttry { return __ocIsolationResolveAgentBins(agentId.trim()); } catch { return []; }
\t\t}
\t\tconst res = await client.request(\"skills.bins\", {});
\t\treturn Array.isArray(res?.bins) ? res.bins.map((bin) => String(bin)) : [];
\t}, pathEnv);"""


def log(msg: str) -> None:
    print(f"[patch-isolation] {msg}")


def resolve_oc_dir(explicit: str | None) -> Path | None:
    """Find the installed OpenClaw npm package root."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env_override = os.environ.get("OCPLATFORM_NPM_DIR")
    if env_override:
        candidates.append(env_override)
    candidates.extend(
        [
            "/opt/homebrew/lib/node_modules/ocplatform",
            "/usr/local/lib/node_modules/openclaw",
            "/usr/lib/node_modules/openclaw",
        ]
    )
    try:
        npm_root = subprocess.check_output(
            ["npm", "root", "-g"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if npm_root:
            candidates.append(os.path.join(npm_root, "openclaw"))
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    for cand in candidates:
        p = Path(cand)
        if (p / "package.json").is_file() and (p / "dist").is_dir():
            return p
    return None


def find_bundle(oc_dir: Path) -> Path | None:
    """Return the node-cli bundle path inside an OpenClaw install, or None."""
    matches = sorted(glob.glob(str(oc_dir / "dist" / "node-cli-*.js")))
    if not matches:
        return None
    # Prefer the one that actually contains `SkillBinsCache`.
    for m in matches:
        try:
            with open(m, "r", encoding="utf-8") as f:
                if "SkillBinsCache" in f.read():
                    return Path(m)
        except OSError:
            continue
    return Path(matches[0])


def is_patched(text: str) -> bool:
    return SENTINEL in text


def apply_patch(text: str) -> str:
    """Return patched text, or raise RuntimeError if an anchor is missing."""
    missing: list[str] = []
    if ANCHOR_CACHE_CLASS_END_UNPATCHED not in text:
        missing.append("SkillBinsCache class body")
    if ANCHOR_CALLSITE_UNPATCHED not in text:
        missing.append("evaluateSystemRunPolicyPhase call site")
    if ANCHOR_CACHE_INIT_UNPATCHED not in text:
        missing.append("SkillBinsCache construction")
    if missing:
        raise RuntimeError(
            "anchor(s) not found in bundle: " + ", ".join(missing) +
            " — upstream may have refactored this file; patcher needs updating."
        )

    # Replacement 1: rewrite the SkillBinsCache class body.
    full_old_class = ANCHOR_CACHE_CLASS + "\n\tconstructor(fetch, pathEnv) {\n\t\tthis.bins = [];\n\t\tthis.lastRefresh = 0;\n\t\tthis.ttlMs = 9e4;\n\t\tthis.fetch = fetch;\n\t\tthis.pathEnv = pathEnv;\n\t}\n" + ANCHOR_CACHE_CLASS_END_UNPATCHED
    if full_old_class not in text:
        # Fall back: replace just the tail block we anchored on, keyed by the class header.
        # This keeps us resilient to minor constructor reordering.
        text = text.replace(
            ANCHOR_CACHE_CLASS_END_UNPATCHED,
            ANCHOR_CACHE_CLASS_END_UNPATCHED,  # no-op; we'll rely on full replace below
            1,
        )
        # Do the full-class rewrite via a two-step: replace the class header line too.
        text = text.replace(
            "var SkillBinsCache = class {\n\tconstructor(fetch, pathEnv) {\n\t\tthis.bins = [];\n\t\tthis.lastRefresh = 0;\n\t\tthis.ttlMs = 9e4;\n\t\tthis.fetch = fetch;\n\t\tthis.pathEnv = pathEnv;\n\t}\n"
            + ANCHOR_CACHE_CLASS_END_UNPATCHED,
            REPLACEMENT_CACHE_CLASS,
            1,
        )
    else:
        text = text.replace(full_old_class, REPLACEMENT_CACHE_CLASS, 1)

    if REPLACEMENT_CACHE_CLASS not in text:
        raise RuntimeError("failed to rewrite SkillBinsCache class body")

    # Replacement 2: thread parsed.agentId into the exec allowlist eval.
    text = text.replace(ANCHOR_CALLSITE_UNPATCHED, REPLACEMENT_CALLSITE, 1)
    if REPLACEMENT_CALLSITE not in text:
        raise RuntimeError("failed to patch evaluateSystemRunPolicyPhase call site")

    # Replacement 3: swap in the per-agent resolver + new cache init.
    text = text.replace(ANCHOR_CACHE_INIT_UNPATCHED, REPLACEMENT_RESOLVER_AND_INIT, 1)
    if "__ocIsolationResolveAgentBins" not in text:
        raise RuntimeError("failed to inject per-agent resolver")

    # Drop a sentinel comment just above the SkillBinsCache class so
    # `--verify` and re-runs can detect prior application.
    text = text.replace(
        "var SkillBinsCache = class {",
        SENTINEL + "\nvar SkillBinsCache = class {",
        1,
    )
    return text


def cmd_verify(bundle: Path) -> int:
    text = bundle.read_text(encoding="utf-8")
    if is_patched(text):
        log(f"✅ bundle is patched: {bundle}")
        log(f"   sentinel: {SENTINEL}")
        return 0
    log(f"⚠️  bundle is NOT patched: {bundle}")
    return 1


def cmd_apply(bundle: Path) -> int:
    text = bundle.read_text(encoding="utf-8")
    if is_patched(text):
        log(f"✅ already patched (sentinel present): {bundle}")
        return 0
    backup = bundle.with_suffix(bundle.suffix + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(bundle, backup)
        log(f"backed up original to {backup}")
    try:
        patched = apply_patch(text)
    except RuntimeError as exc:
        log(f"❌ cannot apply patch: {exc}")
        log(f"   bundle left untouched: {bundle}")
        return 2
    # Write to a tempfile then atomically move for crash-safety.
    tmp = bundle.with_suffix(bundle.suffix + ".oc-isolation.tmp")
    tmp.write_text(patched, encoding="utf-8")
    os.replace(tmp, bundle)
    log(f"✅ patched {bundle}")
    log("   next node-host run will enforce per-agent skill bin trust")
    log("   (restart the gateway or `openclaw gateway restart` to pick it up)")
    return 0


def cmd_revert(bundle: Path) -> int:
    backup = bundle.with_suffix(bundle.suffix + BACKUP_SUFFIX)
    if not backup.exists():
        log(f"❌ no backup found at {backup} — nothing to revert")
        return 1
    shutil.copy2(backup, bundle)
    log(f"✅ reverted {bundle} from {backup}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--oc-dir", help="path to installed OpenClaw npm package root")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--verify", action="store_true", help="report patch status and exit")
    group.add_argument("--revert", action="store_true", help="restore the pre-patch bundle from backup")
    args = parser.parse_args()

    oc_dir = resolve_oc_dir(args.oc_dir)
    if oc_dir is None:
        log("❌ could not locate installed OpenClaw package")
        log("   tried: --oc-dir, $OCPLATFORM_NPM_DIR, homebrew/local/usr node_modules, `npm root -g`")
        return 1
    log(f"OpenClaw install: {oc_dir}")

    bundle = find_bundle(oc_dir)
    if bundle is None:
        log(f"❌ no dist/node-cli-*.js bundle found under {oc_dir}")
        return 1
    log(f"target bundle: {bundle}")

    if args.verify:
        return cmd_verify(bundle)
    if args.revert:
        return cmd_revert(bundle)
    return cmd_apply(bundle)


if __name__ == "__main__":
    sys.exit(main())
