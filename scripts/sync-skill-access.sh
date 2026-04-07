#!/usr/bin/env bash
# sync-skill-access.sh
#
# Reads ~/.openclaw/skill-access.json and updates agents.list[].skills
# in ~/.openclaw/openclaw.json so each agent only sees skills matching
# its tags, plus workspace-local skills, plus optionally all bundled skills.
#
# Data model (skill-access.json):
# {
#   "tags": { "<tag>": { "description": "..." } },
#   "skills": { "<skill-name>": { "tags": ["tag1", "tag2"] } },
#   "agents": {
#     "<agent-id>": {
#       "tags": ["tag1", "tag2"],
#       "includeBundled": true    // default: true. Include all bundled skills.
#     }
#   }
# }
#
# Logic per agent:
#   1. Start with tag-matched global skills (from ~/.openclaw/skills/)
#   2. Add workspace-local skills (workspace-<id>/skills/*)
#   3. If includeBundled != false, add all bundled npm skills
#   4. Write as agents.list[<agent>].skills in openclaw.json
#
# Agents NOT listed in skill-access.json are left untouched (no skills filter,
# meaning they see everything — which is OpenClaw's default behavior).
#
# Usage: bash sync-skill-access.sh [--dry-run]
#
set -euo pipefail

OC_DIR="${HOME}/.openclaw"
ACCESS_FILE="${OC_DIR}/skill-access.json"
CONFIG_FILE="${OC_DIR}/openclaw.json"
BACKUP_FILE="${CONFIG_FILE}.bak"

# Auto-detect bundled skills directory
BUNDLED_DIR=""
for candidate in \
  "/opt/homebrew/lib/node_modules/openclaw/skills" \
  "/usr/lib/node_modules/openclaw/skills" \
  "/usr/local/lib/node_modules/openclaw/skills"; do
  if [[ -d "$candidate" ]]; then
    BUNDLED_DIR="$candidate"
    break
  fi
done
if [[ -z "$BUNDLED_DIR" ]]; then
  NPM_ROOT=$(npm root -g 2>/dev/null || true)
  if [[ -n "$NPM_ROOT" && -d "${NPM_ROOT}/openclaw/skills" ]]; then
    BUNDLED_DIR="${NPM_ROOT}/openclaw/skills"
  fi
fi

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

# --- Preflight ---
for f in "$ACCESS_FILE" "$CONFIG_FILE"; do
  if [[ ! -f "$f" ]]; then
    echo "Error: $f not found"
    exit 1
  fi
done

if ! command -v jq &>/dev/null; then
  echo "Error: jq is required. Install with: brew install jq"
  exit 1
fi

if ! jq empty "$ACCESS_FILE" 2>/dev/null; then
  echo "Error: $ACCESS_FILE is not valid JSON"
  exit 1
fi

MISSING=$(jq -r '
  [ (if .tags  == null then "tags"  else empty end),
    (if .skills == null then "skills" else empty end),
    (if .agents == null then "agents" else empty end) ] | join(", ")
' "$ACCESS_FILE")

if [[ -n "$MISSING" ]]; then
  echo "Error: skill-access.json is missing required keys: $MISSING"
  exit 1
fi

# Get bundled skill names
BUNDLED_SKILLS="[]"
if [[ -n "$BUNDLED_DIR" && -d "$BUNDLED_DIR" ]]; then
  BUNDLED_SKILLS=$(ls -1 "$BUNDLED_DIR" 2>/dev/null | jq -R . | jq -s '.' 2>/dev/null || echo "[]")
fi
BUNDLED_COUNT=$(echo "$BUNDLED_SKILLS" | jq 'length')

echo "=== Skill Access Sync ==="
echo "Bundled skills dir: ${BUNDLED_DIR:-<not found>}"
echo "Bundled skills: $BUNDLED_COUNT"
echo ""

# --- Compute tag-based skill assignments per agent ---
TAG_ASSIGNMENTS=$(jq -r '
  .skills as $skills |
  .agents as $agents |
  reduce ($agents | keys[]) as $agent_id (
    {};
    . as $result |
    $agents[$agent_id].tags as $agent_tags |
    [
      $skills | to_entries[] |
      select(
        .value.tags as $skill_tags |
        ($skill_tags | map(. as $st | $agent_tags | index($st) != null) | any)
      ) |
      .key
    ] as $matched_skills |
    $result + { ($agent_id): $matched_skills }
  )
' "$ACCESS_FILE")

# --- Build final assignments ---
MERGED_JSON="{}"
for agent_id in $(echo "$TAG_ASSIGNMENTS" | jq -r 'keys[]'); do
  # Tag-matched global skills
  TAG_SKILLS=$(echo "$TAG_ASSIGNMENTS" | jq -c --arg id "$agent_id" '.[$id]')

  # Workspace-local skills
  WS_SKILLS="[]"
  ws_dir="${OC_DIR}/workspace-${agent_id}/skills"
  if [[ -d "$ws_dir" ]]; then
    WS_SKILLS=$(ls -1 "$ws_dir" 2>/dev/null | jq -R . | jq -s '.' 2>/dev/null || echo "[]")
  fi

  # Check includeBundled (default: true)
  INCLUDE_BUNDLED=$(jq -r --arg id "$agent_id" \
    '.agents[$id].includeBundled // true' "$ACCESS_FILE")

  # Merge: tag skills + workspace skills + (optionally) bundled skills
  if [[ "$INCLUDE_BUNDLED" == "true" ]]; then
    MERGED=$(jq -n \
      --argjson tag "$TAG_SKILLS" \
      --argjson ws "$WS_SKILLS" \
      --argjson bundled "$BUNDLED_SKILLS" \
      '$tag + $ws + $bundled | unique | sort')
  else
    MERGED=$(jq -n \
      --argjson tag "$TAG_SKILLS" \
      --argjson ws "$WS_SKILLS" \
      '$tag + $ws | unique | sort')
  fi

  MERGED_JSON=$(echo "$MERGED_JSON" | jq --arg id "$agent_id" --argjson skills "$MERGED" \
    '. + { ($id): $skills }')

  # Print summary
  SKILL_COUNT=$(echo "$MERGED" | jq 'length')
  TAG_COUNT=$(echo "$TAG_SKILLS" | jq 'length')
  WS_COUNT=$(echo "$WS_SKILLS" | jq 'length')
  AGENT_TAGS=$(jq -r --arg id "$agent_id" '.agents[$id].tags | join(", ")' "$ACCESS_FILE")

  echo "Agent: $agent_id"
  echo "  Tags: $AGENT_TAGS"
  echo "  Tag-matched global skills ($TAG_COUNT): $(echo "$TAG_SKILLS" | jq -r 'join(", ")')"
  if [[ "$WS_COUNT" -gt 0 ]]; then
    echo "  Workspace skills ($WS_COUNT): $(echo "$WS_SKILLS" | jq -r 'join(", ")')"
  fi
  echo "  Include bundled: $INCLUDE_BUNDLED"
  echo "  Total skills: $SKILL_COUNT"
  echo ""
done

# --- Apply or dry-run ---
if [[ "$DRY_RUN" == true ]]; then
  echo "[DRY RUN] Would update agents.list[].skills in $CONFIG_FILE"
  echo ""
  echo "Final assignments (skill counts):"
  echo "$MERGED_JSON" | jq 'to_entries | map("  \(.key): \(.value | length) skills") | .[]' -r
  exit 0
fi

# Backup
cp "$CONFIG_FILE" "$BACKUP_FILE"
echo "Backup saved to $BACKUP_FILE"

# Update openclaw.json
UPDATED_CONFIG=$(jq --argjson assignments "$MERGED_JSON" '
  reduce ($assignments | keys[]) as $agent_id (
    .;
    ($assignments[$agent_id]) as $skills |
    (
      [ .agents.list | to_entries[] | select(.value.id == $agent_id) | .key ] | first // null
    ) as $idx |
    if $idx != null then
      .agents.list[$idx].skills = $skills
    else
      .
    end
  )
' "$CONFIG_FILE")

# Warn about agents not found in config
MISSING_AGENTS=$(jq -n --argjson assignments "$MERGED_JSON" --slurpfile config "$CONFIG_FILE" '
  ($config[0].agents.list | map(.id)) as $config_ids |
  [$assignments | keys[] | select(. as $id | $config_ids | index($id) == null)]
')
if [[ $(echo "$MISSING_AGENTS" | jq 'length') -gt 0 ]]; then
  echo "Warning: These agents are in skill-access.json but not in openclaw.json:"
  echo "   $(echo "$MISSING_AGENTS" | jq -r 'join(", ")')"
  echo ""
fi

echo "$UPDATED_CONFIG" | jq '.' > "$CONFIG_FILE"
echo "Updated $CONFIG_FILE"
echo ""
echo "Agents NOT in skill-access.json keep their current behavior (no skills filter)."
echo ""
echo "Note: Restart the gateway for changes to take effect:"
echo "   openclaw gateway restart"
