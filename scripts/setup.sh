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
