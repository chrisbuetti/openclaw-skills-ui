#!/usr/bin/env python3
"""configure-exec-approvals.py — Configure exec-approvals.json for per-agent isolation.

This script automates Step 3 from docs/per-agent-exec-isolation.md:
flipping sandboxed agents into `security: "allowlist"` with
`autoAllowSkills: true` so the per-agent skill bin isolation patch
actually takes effect.

Usage:
    # Interactive mode — pick agents to sandbox from a list:
    python3 scripts/configure-exec-approvals.py

    # Sandbox specific agents by id:
    python3 scripts/configure-exec-approvals.py --sandbox gabe

    # Sandbox all agents by default, carve out main agent:
    python3 scripts/configure-exec-approvals.py --sandbox-all --main main

    # Dry run — show what would change without writing:
    python3 scripts/configure-exec-approvals.py --sandbox gabe --dry-run

    # Revert to most recent backup:
    python3 scripts/configure-exec-approvals.py --revert

    # Custom OpenClaw directory:
    python3 scripts/configure-exec-approvals.py --oc-dir /path/to/.openclaw
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple


def find_oc_dir() -> Path:
    """Locate the OpenClaw data directory."""
    oc_dir = Path.home() / ".openclaw"
    if oc_dir.is_dir():
        return oc_dir
    raise FileNotFoundError(
        f"OpenClaw data directory not found at {oc_dir}. "
        "Use --oc-dir to specify a custom location."
    )


def load_json(path: Path) -> dict:
    """Load a JSON file, returning its parsed contents."""
    with open(path, "r") as f:
        return json.load(f)


def save_json_atomic(path: Path, data: dict) -> None:
    """Atomically write data to a JSON file.

    Writes to a temp file in the same directory then os.replace()s over
    the target — same pattern as patch-openclaw-isolation.py. Prevents
    half-written files if the process is killed mid-write.
    """
    content = json.dumps(data, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=".exec-approvals-", suffix=".tmp"
    )
    try:
        with open(fd, "w") as f:
            f.write(content)
        Path(tmp_path).replace(path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def get_agent_ids(config: dict) -> List[str]:
    """Extract agent ids from openclaw.json.

    Handles both formats:
      - agents.list: [{id: "main"}, {id: "gabe"}, ...]
      - agents.<name>: {id: "...", ...}  (legacy/flat)
    """
    agents = config.get("agents", {})
    ids: List[str] = []

    # Format 1: agents.list array (standard)
    if "list" in agents and isinstance(agents["list"], list):
        for entry in agents["list"]:
            if isinstance(entry, dict) and "id" in entry:
                ids.append(entry["id"])
        return ids

    # Format 2: flat keys (legacy / some configs)
    for key, val in agents.items():
        if key == "defaults":
            continue
        if isinstance(val, dict) and "id" in val:
            ids.append(val["id"])
        elif isinstance(val, list):
            for entry in val:
                if isinstance(entry, dict) and "id" in entry:
                    ids.append(entry["id"])
        else:
            ids.append(key)

    return ids


def interactive_select(agent_ids: List[str]) -> Tuple[List[str], Optional[str]]:
    """Interactively select agents to sandbox and optionally a main agent.

    Returns (sandbox_ids, main_id_or_none).
    """
    if not sys.stdin.isatty():
        print(
            "Error: interactive mode requires a terminal (stdin is not a TTY).\n"
            "Use --sandbox AGENT_ID or --sandbox-all --main AGENT_ID instead.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\nDiscovered agents:")
    for i, aid in enumerate(agent_ids, 1):
        print(f"  {i}. {aid}")
    print()

    # Select main agent
    print('Which agent is your PRIMARY (unsandboxed) agent?')
    print('This agent keeps security: "full" (unrestricted exec).')
    print("Enter the number, or press Enter to skip (no carve-out):")
    main_id: Optional[str] = None
    while True:
        choice = input("> ").strip()
        if not choice:
            break
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(agent_ids):
                main_id = agent_ids[idx]
                print(f"  → {main_id} will remain unsandboxed.\n")
                break
            else:
                print(f"  Invalid number. Enter 1-{len(agent_ids)} or press Enter.")
        except ValueError:
            if choice in agent_ids:
                main_id = choice
                print(f"  → {main_id} will remain unsandboxed.\n")
                break
            print("  Invalid input. Enter a number or agent id.")

    # Select agents to sandbox
    remaining = [a for a in agent_ids if a != main_id]
    if not remaining:
        print("No other agents to sandbox.")
        return [], main_id

    print("Which agents should be SANDBOXED?")
    print("Options:")
    print("  a  — all remaining agents")
    print("  1,3,5 — specific numbers (comma-separated)")
    print("  gabe,cliff — specific ids (comma-separated)")
    print()
    for i, aid in enumerate(remaining, 1):
        print(f"  {i}. {aid}")
    print()

    while True:
        choice = input("> ").strip()
        if not choice:
            print("  No agents selected. Nothing to do.")
            return [], main_id

        if choice.lower() == "a":
            print(f"  → Sandboxing all {len(remaining)} agents.")
            return remaining, main_id

        parts = [p.strip() for p in choice.split(",")]
        selected: List[str] = []
        try:
            indices = [int(p) - 1 for p in parts]
            for idx in indices:
                if 0 <= idx < len(remaining):
                    selected.append(remaining[idx])
                else:
                    print(f"  Invalid number: {idx + 1}")
                    selected = []
                    break
        except ValueError:
            for name in parts:
                if name in remaining:
                    selected.append(name)
                else:
                    print(f"  Unknown agent: {name}")
                    selected = []
                    break

        if selected:
            print(f"  → Sandboxing: {', '.join(selected)}")
            return selected, main_id

        print("  Try again. Enter 'a' for all, numbers, or agent ids.")


def find_latest_backup(oc_dir: Path) -> Optional[Path]:
    """Find the most recent .bak file for exec-approvals.json."""
    pattern = "exec-approvals.pre-isolation-*.bak"
    backups = sorted(oc_dir.glob(pattern), reverse=True)
    return backups[0] if backups else None


def revert(oc_dir: Path, dry_run: bool = False) -> None:
    """Restore exec-approvals.json from the most recent backup."""
    approvals_path = oc_dir / "exec-approvals.json"
    backup = find_latest_backup(oc_dir)

    if not backup:
        print("No backup files found (exec-approvals.pre-isolation-*.bak).", file=sys.stderr)
        sys.exit(1)

    print(f"Found backup: {backup.name}")
    print(f"  Created: {backup.stat().st_mtime}")

    if dry_run:
        print("\n--- DRY RUN — would restore from backup ---")
        data = load_json(backup)
        print(json.dumps(data, indent=2))
        print("--- end dry run ---")
        return

    shutil.copy2(backup, approvals_path)
    print(f"Restored {approvals_path} from {backup.name}")
    print("\n⚠️  Restart the OpenClaw gateway for changes to take effect:")
    print("     openclaw gateway restart")


def configure(
    oc_dir: Path,
    sandbox_ids: List[str],
    main_id: Optional[str] = None,
    sandbox_all: bool = False,
    dry_run: bool = False,
    ask_mode: str = "off",
) -> None:
    """Apply exec-approvals.json configuration.

    Args:
        oc_dir: Path to ~/.openclaw
        sandbox_ids: Agent ids to set to allowlist mode
        main_id: Agent id to keep at security: "full" (used with --sandbox-all)
        sandbox_all: If True, set defaults to allowlist and carve out main_id
        dry_run: If True, print the result without writing
        ask_mode: "off" (hard deny) or "on-miss" (prompt for approval)
    """
    approvals_path = oc_dir / "exec-approvals.json"

    # Load existing — refuse to create from scratch (socket config is required)
    if approvals_path.exists():
        approvals = load_json(approvals_path)
        print(f"Loaded existing {approvals_path}")
    else:
        print(
            f"Error: {approvals_path} not found.\n"
            "This file is created by OCPlatform on first run and contains\n"
            "required socket configuration. Start the gateway at least once\n"
            "before running this script.",
            file=sys.stderr,
        )
        sys.exit(1)

    if "agents" not in approvals:
        approvals["agents"] = {}
    if "defaults" not in approvals:
        approvals["defaults"] = {}

    changes: List[str] = []

    # Option B: sandbox all by default
    if sandbox_all:
        old_defaults = dict(approvals["defaults"])
        approvals["defaults"]["security"] = "allowlist"
        approvals["defaults"]["ask"] = ask_mode
        approvals["defaults"]["autoAllowSkills"] = True
        if approvals["defaults"] != old_defaults:
            changes.append("Set defaults to sandbox mode (allowlist + autoAllowSkills)")

        # Carve out the main agent
        if main_id:
            agent_cfg = approvals["agents"].get(main_id, {})
            if agent_cfg.get("security") != "full":
                agent_cfg["security"] = "full"
                approvals["agents"][main_id] = agent_cfg
                changes.append(f'Carved out {main_id} with security: "full"')

    # Sandbox specific agents
    for agent_id in sandbox_ids:
        agent_cfg = approvals["agents"].get(agent_id, {})
        existing_allowlist = agent_cfg.get("allowlist", [])

        needs_update = (
            agent_cfg.get("security") != "allowlist"
            or agent_cfg.get("autoAllowSkills") is not True
            or agent_cfg.get("ask") != ask_mode
        )

        if needs_update:
            agent_cfg["security"] = "allowlist"
            agent_cfg["ask"] = ask_mode
            agent_cfg["autoAllowSkills"] = True
            if "allowlist" not in agent_cfg:
                agent_cfg["allowlist"] = []
            approvals["agents"][agent_id] = agent_cfg
            preserved = len(existing_allowlist)
            changes.append(
                f"Sandboxed {agent_id} (allowlist + autoAllowSkills"
                + (f", preserved {preserved} existing allowlist entries" if preserved else "")
                + ")"
            )
        else:
            print(f"  {agent_id}: already configured for sandbox mode, skipping.")

    if not changes:
        print("\nNo changes needed — exec-approvals.json is already configured.")
        return

    # Show what we're about to do
    print(f"\nChanges to apply ({len(changes)}):")
    for c in changes:
        print(f"  • {c}")

    if dry_run:
        print("\n--- DRY RUN — would write: ---")
        print(json.dumps(approvals, indent=2))
        print("--- end dry run ---")
        return

    # Backup
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = approvals_path.with_suffix(f".pre-isolation-{timestamp}.bak")
    shutil.copy2(approvals_path, backup_path)
    print(f"\nBacked up to {backup_path}")

    # Atomic write
    save_json_atomic(approvals_path, approvals)
    print(f"Wrote {approvals_path}")

    print("\n⚠️  Restart the OpenClaw gateway for changes to take effect:")
    print("     openclaw gateway restart")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure exec-approvals.json for per-agent skill bin isolation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--oc-dir",
        type=Path,
        default=None,
        help="Path to OpenClaw data directory (default: ~/.openclaw)",
    )
    parser.add_argument(
        "--sandbox",
        nargs="+",
        metavar="AGENT_ID",
        help="Agent id(s) to put in sandbox/allowlist mode",
    )
    parser.add_argument(
        "--sandbox-all",
        action="store_true",
        help="Set defaults to sandbox mode for all agents",
    )
    parser.add_argument(
        "--main",
        metavar="AGENT_ID",
        help='Primary agent id to keep at security: "full" (used with --sandbox-all)',
    )
    parser.add_argument(
        "--ask",
        choices=["off", "on-miss"],
        default="off",
        help="Ask mode for sandboxed agents: 'off' = hard deny, 'on-miss' = prompt (default: off)",
    )
    parser.add_argument(
        "--revert",
        action="store_true",
        help="Restore exec-approvals.json from the most recent backup",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing",
    )

    args = parser.parse_args()

    # Find OC dir
    if args.oc_dir:
        oc_dir = args.oc_dir
        if not oc_dir.is_dir():
            print(f"Error: {oc_dir} is not a directory.", file=sys.stderr)
            sys.exit(1)
    else:
        oc_dir = find_oc_dir()

    # Revert mode
    if args.revert:
        if args.sandbox or args.sandbox_all:
            print("Error: --revert cannot be combined with --sandbox or --sandbox-all.", file=sys.stderr)
            sys.exit(1)
        revert(oc_dir, dry_run=args.dry_run)
        return

    # Disallow combining --sandbox and --sandbox-all (ambiguous intent)
    if args.sandbox and args.sandbox_all:
        print(
            "Error: --sandbox and --sandbox-all are mutually exclusive.\n"
            "Use --sandbox to sandbox specific agents, or --sandbox-all --main AGENT to sandbox everyone.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load openclaw.json to discover agents
    config_path = oc_dir / "openclaw.json"
    if not config_path.exists():
        print(f"Error: {config_path} not found.", file=sys.stderr)
        sys.exit(1)

    config = load_json(config_path)
    agent_ids = get_agent_ids(config)

    if not agent_ids:
        print("Error: No agents found in openclaw.json.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(agent_ids)} agents: {', '.join(agent_ids)}")

    # Determine what to do
    if args.sandbox:
        unknown = [a for a in args.sandbox if a not in agent_ids]
        if unknown:
            print(f"Warning: unknown agent id(s): {', '.join(unknown)}")
            print(f"Known agents: {', '.join(agent_ids)}")
            if sys.stdin.isatty():
                confirm = input("Continue anyway? [y/N] ").strip().lower()
                if confirm != "y":
                    sys.exit(0)
            else:
                print("Non-interactive mode — aborting on unknown agents.", file=sys.stderr)
                sys.exit(1)

        configure(
            oc_dir,
            sandbox_ids=args.sandbox,
            dry_run=args.dry_run,
            ask_mode=args.ask,
        )

    elif args.sandbox_all:
        if not args.main:
            print("Error: --sandbox-all requires --main to specify the unsandboxed agent.", file=sys.stderr)
            sys.exit(1)
        if args.main not in agent_ids:
            print(f"Warning: main agent '{args.main}' not found in openclaw.json agents.")

        sandbox_ids = [a for a in agent_ids if a != args.main]
        configure(
            oc_dir,
            sandbox_ids=sandbox_ids,
            main_id=args.main,
            sandbox_all=True,
            dry_run=args.dry_run,
            ask_mode=args.ask,
        )

    else:
        # Interactive mode
        sandbox_ids, main_id = interactive_select(agent_ids)
        if not sandbox_ids:
            print("Nothing to do.")
            sys.exit(0)

        configure(
            oc_dir,
            sandbox_ids=sandbox_ids,
            main_id=main_id,
            dry_run=args.dry_run,
            ask_mode=args.ask,
        )


if __name__ == "__main__":
    main()
