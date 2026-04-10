#!/usr/bin/env bash
# setup.sh — Set up OpenClaw Manager for first use.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
OC_DIR="${HOME}/.openclaw"
ACCESS_FILE="${OC_DIR}/skill-access.json"
CONFIG_FILE="${OC_DIR}/openclaw.json"
SYNC_DEST="${OC_DIR}/scripts/sync-skill-access.sh"

echo "=== OpenClaw Manager Setup ==="
echo ""

# --- Detect OpenClaw installation ---
echo "Detecting OpenClaw installation..."

NPM_SKILLS_DIR=""
for candidate in \
  "/opt/homebrew/lib/node_modules/openclaw/skills" \
  "/usr/lib/node_modules/openclaw/skills" \
  "/usr/local/lib/node_modules/openclaw/skills"; do
  if [[ -d "$candidate" ]]; then
    NPM_SKILLS_DIR="$candidate"
    break
  fi
done

if [[ -z "$NPM_SKILLS_DIR" ]]; then
  NPM_ROOT=$(npm root -g 2>/dev/null || true)
  if [[ -n "$NPM_ROOT" && -d "${NPM_ROOT}/openclaw/skills" ]]; then
    NPM_SKILLS_DIR="${NPM_ROOT}/openclaw/skills"
  fi
fi

if [[ -n "$NPM_SKILLS_DIR" ]]; then
  echo "  Found OpenClaw skills at: $NPM_SKILLS_DIR"
else
  echo "  Warning: Could not find OpenClaw npm installation."
  echo "  Make sure OpenClaw is installed: npm install -g openclaw"
  echo "  You can set NPM_SKILLS_DIR manually if installed in a non-standard location."
fi

if [[ ! -d "$OC_DIR" ]]; then
  echo "  Error: $OC_DIR not found. Is OpenClaw initialized?"
  echo "  Run 'openclaw init' first."
  exit 1
fi
echo "  OpenClaw data dir: $OC_DIR"
echo ""

# --- Normalize agent workspaces ---
echo "Normalizing agent workspaces..."
AGENTS_DIR="${OC_DIR}/agents"

# Known system/internal folders that should never be treated as agent dirs
SYSTEM_DIRS="agents browser canvas classifications completions credentials cron custom-plugins delivery-queue devices extensions flows identity logs media memory ollama-modelfiles qqbot scripts skills subagents tasks telegram workspace"

is_system_dir() {
  echo " $SYSTEM_DIRS " | grep -q " $1 "
}

if [[ -d "$AGENTS_DIR" ]]; then
  ws_renamed=0
  ws_created=0
  ws_skipped=0

  for agent_dir in "$AGENTS_DIR"/*/; do
    [[ ! -d "$agent_dir" ]] && continue
    agent=$(basename "$agent_dir")
    workspace_dir="${OC_DIR}/workspace-${agent}"
    adhoc_dir="${OC_DIR}/${agent}"

    if [[ -d "$workspace_dir" ]]; then
      ((ws_skipped++))
      continue
    fi

    # Check for ad-hoc folder (agent name without workspace- prefix)
    if [[ -d "$adhoc_dir" ]] && ! is_system_dir "$agent"; then
      echo "  🔄 $agent — renaming ${agent}/ → workspace-${agent}/"
      mv "$adhoc_dir" "$workspace_dir"
      ((ws_renamed++))
      continue
    fi

    # No folder at all — create empty workspace
    echo "  📁 $agent — creating workspace-${agent}/"
    mkdir -p "$workspace_dir"
    ((ws_created++))
  done

  echo "  Workspaces: ${ws_renamed} renamed, ${ws_created} created, ${ws_skipped} already existed"

  # Clean up explicit workspace/agentDir paths in openclaw.json
  # so agents use the standard workspace-{id} convention
  if [[ -f "$CONFIG_FILE" ]] && command -v jq &>/dev/null; then
    config_changed=false
    TMPFILE=$(mktemp)
    cp "$CONFIG_FILE" "$TMPFILE"

    AGENT_COUNT=$(jq '.agents.list | length' "$TMPFILE" 2>/dev/null || echo 0)
    for i in $(seq 0 $((AGENT_COUNT - 1))); do
      aid=$(jq -r ".agents.list[$i].id" "$TMPFILE" 2>/dev/null)
      ws=$(jq -r ".agents.list[$i].workspace // empty" "$TMPFILE" 2>/dev/null)
      adir=$(jq -r ".agents.list[$i].agentDir // empty" "$TMPFILE" 2>/dev/null)

      if [[ -n "$ws" ]]; then
        expected_ws="${OC_DIR}/workspace-${aid}"
        # For main agent, the default convention works without explicit path
        if [[ "$aid" == "main" ]] || [[ "$ws" != "$expected_ws" && -d "${OC_DIR}/workspace-${aid}" ]]; then
          echo "  🔧 $aid — removing explicit workspace path from config (convention will be used)"
          jq ".agents.list[$i] |= del(.workspace)" "$TMPFILE" > "${TMPFILE}.2" && mv "${TMPFILE}.2" "$TMPFILE"
          config_changed=true
        fi
      fi

      if [[ -n "$adir" ]]; then
        echo "  🔧 $aid — removing explicit agentDir from config"
        jq ".agents.list[$i] |= del(.agentDir)" "$TMPFILE" > "${TMPFILE}.2" && mv "${TMPFILE}.2" "$TMPFILE"
        config_changed=true
      fi
    done

    # Also remove defaults.workspace if it just points to the standard location
    default_ws=$(jq -r '.agents.defaults.workspace // empty' "$TMPFILE" 2>/dev/null)
    if [[ -n "$default_ws" && "$default_ws" == "${OC_DIR}/workspace" ]]; then
      echo "  🔧 Removing redundant defaults.workspace from config"
      jq '.agents.defaults |= del(.workspace)' "$TMPFILE" > "${TMPFILE}.2" && mv "${TMPFILE}.2" "$TMPFILE"
      config_changed=true
    fi

    if [[ "$config_changed" == "true" ]]; then
      # Backup and apply
      cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"
      mv "$TMPFILE" "$CONFIG_FILE"
      echo "  ✅ Updated openclaw.json (backup at openclaw.json.bak)"
    else
      rm -f "$TMPFILE"
      echo "  ✅ No config changes needed"
    fi
  fi
else
  echo "  No agents directory found, skipping."
fi
echo ""

# --- Create skill-access.json if missing ---
if [[ -f "$ACCESS_FILE" ]]; then
  echo "skill-access.json already exists, keeping it."
else
  echo "Creating skill-access.json..."

  # Start with empty structure
  ACCESS_JSON='{"tags":{},"skills":{},"agents":{}}'

  # Pre-populate agents from openclaw.json
  if [[ -f "$CONFIG_FILE" ]] && command -v jq &>/dev/null; then
    AGENT_IDS=$(jq -r '.agents.list[]?.id // empty' "$CONFIG_FILE" 2>/dev/null || true)
    for aid in $AGENT_IDS; do
      ACCESS_JSON=$(echo "$ACCESS_JSON" | jq --arg id "$aid" '.agents[$id] = {"tags": [], "includeBundled": true}')
    done
    AGENT_COUNT=$(echo "$ACCESS_JSON" | jq '.agents | length')
    echo "  Pre-populated $AGENT_COUNT agents from openclaw.json"
  else
    if ! command -v jq &>/dev/null; then
      echo "  Note: jq not found — skipping agent pre-population."
      echo "  Install jq for full functionality: brew install jq"
    fi
  fi

  # Pre-populate global skills
  GLOBAL_DIR="${OC_DIR}/skills"
  if [[ -d "$GLOBAL_DIR" ]]; then
    for skill_dir in "$GLOBAL_DIR"/*/; do
      if [[ -f "${skill_dir}SKILL.md" ]]; then
        skill_name=$(basename "$skill_dir")
        ACCESS_JSON=$(echo "$ACCESS_JSON" | jq --arg name "$skill_name" '.skills[$name] = {"tags": []}')
      fi
    done
    SKILL_COUNT=$(echo "$ACCESS_JSON" | jq '.skills | length')
    echo "  Pre-populated $SKILL_COUNT global skills"
  fi

  echo "$ACCESS_JSON" | jq '.' > "$ACCESS_FILE"
  echo "  Created $ACCESS_FILE"
fi
echo ""

# --- Copy sync script ---
echo "Installing sync script..."
mkdir -p "$(dirname "$SYNC_DEST")"
cp "${SCRIPT_DIR}/sync-skill-access.sh" "$SYNC_DEST"
chmod +x "$SYNC_DEST"
echo "  Copied to $SYNC_DEST"
echo ""

# --- Install Python dependencies ---
echo "Installing Python dependencies..."
if [[ -d "${REPO_DIR}/venv" ]]; then
  echo "  Activating existing venv..."
  source "${REPO_DIR}/venv/bin/activate"
else
  echo "  Creating virtual environment..."
  python3 -m venv "${REPO_DIR}/venv"
  source "${REPO_DIR}/venv/bin/activate"
fi
pip install -q -r "${REPO_DIR}/requirements.txt"
echo "  Dependencies installed."
echo ""

# --- Patch OpenClaw for per-agent skill bin isolation ---
# By default OpenClaw auto-approves exec calls for any bin a skill declares
# in its `metadata.openclaw.requires.bins` list, and the trust set is a
# UNION across every agent workspace on the host. That means a `gog` skill
# installed in agent A's workspace silently approves `gog` execs issued by
# agent B as well. The patcher below rewrites the installed OpenClaw's
# SkillBinsCache so that auto-trust is scoped to the agent that actually
# issued the exec. See scripts/patch-openclaw-isolation.py for details.
echo "Patching OCPlatform for per-agent skill bin isolation..."
if [[ -n "$NPM_SKILLS_DIR" ]]; then
  OC_ROOT_DIR="$(dirname "$NPM_SKILLS_DIR")"
  if python3 "${SCRIPT_DIR}/patch-openclaw-isolation.py" --oc-dir "$OC_ROOT_DIR"; then
    echo "  ✅ Patch applied (or already present)."
    echo "  ℹ️  Restart the OpenClaw gateway so the node host picks it up:"
    echo "       ocplatform gateway restart"
  else
    status=$?
    if [[ $status -eq 2 ]]; then
      echo "  ⚠️  Patcher could not locate its anchor strings — OCPlatform may have been updated."
      echo "      Original bundle left untouched; ping the skills-ui maintainers for an updated patch."
    else
      echo "  ⚠️  Patcher failed with exit $status; continuing setup."
    fi
  fi
else
  echo "  Skipping — OpenClaw install not detected above."
fi
echo ""

# --- Done ---
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Start the server:"
echo "       bash run.sh"
echo "     or:"
echo "       source venv/bin/activate && uvicorn main:app --reload"
echo ""
echo "  2. Open http://127.0.0.1:8000 in your browser"
echo ""
echo "  3. Use the Groups page to create tags, then assign them to"
echo "     skills and agents on the Access page."
echo ""
echo "  4. Click 'Sync & Apply' to write the computed skill allowlists"
echo "     to openclaw.json."
echo ""
if [[ -z "$NPM_SKILLS_DIR" ]]; then
  echo "  Note: Set NPM_SKILLS_DIR if your OpenClaw npm skills are in"
  echo "  a non-standard location."
  echo ""
fi
