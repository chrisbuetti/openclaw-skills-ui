import os
import re
import glob
import subprocess
import asyncio
import logging
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional, List
import json
import shutil


def find_openclaw_binary() -> str:
    """Auto-detect the ocplatform binary."""
    found = shutil.which("openclaw")
    if found:
        return found
    candidates = [
        "/opt/homebrew/bin/ocplatform",
        "/usr/local/bin/openclaw",
        "/usr/bin/openclaw",
        os.path.expanduser("~/.local/bin/openclaw"),
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return "openclaw"  # fallback to bare name


OCPLATFORM_BIN = find_openclaw_binary()


def restart_gateway():
    try:
        subprocess.run([OCPLATFORM_BIN, "gateway", "restart"], check=True)
    except Exception as e:
        print("Failed to restart gateway:", e)


def detect_npm_skills_dir() -> str:
    """Auto-detect the OpenClaw npm skills directory."""
    env = os.environ.get("NPM_SKILLS_DIR")
    if env and os.path.isdir(env):
        return env
    # Check common paths
    candidates = [
        "/opt/homebrew/lib/node_modules/openclaw/skills",
        "/usr/lib/node_modules/openclaw/skills",
        "/usr/local/lib/node_modules/openclaw/skills",
    ]
    for p in candidates:
        if os.path.isdir(p):
            return p
    # Try npm root -g
    try:
        result = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            p = os.path.join(result.stdout.strip(), "openclaw", "skills")
            if os.path.isdir(p):
                return p
    except Exception:
        pass
    # Fallback — try to derive from the binary location
    bin_path = find_openclaw_binary()
    if bin_path and bin_path != "openclaw":
        # e.g. /opt/homebrew/bin/openclaw -> /opt/homebrew/lib/node_modules/openclaw/skills
        bin_dir = os.path.dirname(bin_path)
        parent = os.path.dirname(bin_dir)
        candidate = os.path.join(parent, "lib", "node_modules", "openclaw", "skills")
        if os.path.isdir(candidate):
            return candidate
    return "/opt/homebrew/lib/node_modules/openclaw/skills"


app = FastAPI(title="OpenClaw Manager")
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")), name="static")
templates = Jinja2Templates(directory="templates")

# --- Configurable paths ---
OCPLATFORM_DIR = os.environ.get("OCPLATFORM_DIR", os.path.expanduser("~/.openclaw"))
SKILLS_GLOB = os.path.join(OCPLATFORM_DIR, "workspace-*/skills/*")
WORKSPACE_GLOB = os.path.join(OCPLATFORM_DIR, "workspace-*")
MAIN_WORKSPACE_DIR = os.path.join(OCPLATFORM_DIR, "workspace")
MAIN_SKILLS_GLOB = os.path.join(MAIN_WORKSPACE_DIR, "skills/*")
NPM_SKILLS_DIR = detect_npm_skills_dir()
GLOBAL_SKILLS_DIR = os.path.join(OCPLATFORM_DIR, "skills")
CLASSIFICATIONS_DIR = os.path.join(OCPLATFORM_DIR, "classifications")
CONFIG_PATH = os.path.join(OCPLATFORM_DIR, "openclaw.json")
AGENT_CLS_PATH = os.path.join(OCPLATFORM_DIR, "agent-classifications.json")
SKILL_ACCESS_PATH = os.path.join(OCPLATFORM_DIR, "skill-access.json")
SYNC_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "sync-skill-access.sh")
UI_SETTINGS_PATH = os.path.join(OCPLATFORM_DIR, "openclaw-skills-ui.json")
AGENT_PHOTOS_DIR = os.path.join(OCPLATFORM_DIR, "agent-photos")
AGENT_METADATA_PATH = os.path.join(OCPLATFORM_DIR, "agent-metadata.json")
DEFAULT_PHOTO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "default-agent.png")


logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
logger = logging.getLogger("openclaw-skills-ui")

# Ensure agent photos directory exists
os.makedirs(AGENT_PHOTOS_DIR, exist_ok=True)
os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), exist_ok=True)


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def resolve_workspace_dir(agent_name: str) -> str:
    """Resolve the workspace directory for an agent.
    
    Checks openclaw.json for an explicit workspace path first,
    then falls back to convention: 'main' uses workspace/, others use workspace-<name>/.
    """
    # Check config for explicit workspace path
    config = load_config()
    for agent_cfg in config.get("agents", {}).get("list", []):
        if agent_cfg.get("id") == agent_name:
            explicit = agent_cfg.get("workspace", "")
            if explicit and os.path.isdir(explicit):
                return explicit
            break
    # Convention fallback
    if agent_name == "main":
        return MAIN_WORKSPACE_DIR
    return os.path.join(OCPLATFORM_DIR, f"workspace-{agent_name}")


def resolve_skill_dir(agent_name: str, folder: str) -> str:
    """Resolve the skill directory for an agent's skill."""
    return os.path.join(resolve_workspace_dir(agent_name), "skills", folder)


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def load_agent_metadata() -> dict:
    """Load agent metadata (organization, apps) from agent-metadata.json."""
    if os.path.exists(AGENT_METADATA_PATH):
        try:
            with open(AGENT_METADATA_PATH) as f:
                return json.load(f).get("agents", {})
        except Exception:
            pass
    return {}


def load_classifications_map() -> dict:
    if os.path.exists(AGENT_CLS_PATH):
        with open(AGENT_CLS_PATH) as f:
            return json.load(f)
    return {}


def save_classifications_map(data: dict):
    with open(AGENT_CLS_PATH, "w") as f:
        json.dump(data, f, indent=4)


def load_skill_access() -> dict:
    if os.path.exists(SKILL_ACCESS_PATH):
        with open(SKILL_ACCESS_PATH) as f:
            data = json.load(f)
    else:
        data = {}
    # Ensure structure
    data.setdefault("tags", {})
    data.setdefault("skills", {})
    data.setdefault("agents", {})
    return data


def save_skill_access(data: dict):
    with open(SKILL_ACCESS_PATH, "w") as f:
        json.dump(data, f, indent=4)


LOGGING_LEVEL_OPTIONS = ["silent", "fatal", "error", "warn", "info", "debug", "trace"]
VERBOSE_DEFAULT_OPTIONS = ["off", "on"]


def load_ui_settings() -> dict:
    if os.path.exists(UI_SETTINGS_PATH):
        with open(UI_SETTINGS_PATH) as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("verbose_logging", False)
    return data


def save_ui_settings(data: dict):
    os.makedirs(os.path.dirname(UI_SETTINGS_PATH), exist_ok=True)
    with open(UI_SETTINGS_PATH, "w") as f:
        json.dump(data, f, indent=4)


def is_verbose_logging_enabled() -> bool:
    return bool(load_ui_settings().get("verbose_logging", False))


def log_verbose(message: str, **context):
    if not is_verbose_logging_enabled():
        return
    if context:
        logger.info("[verbose] %s | %s", message, json.dumps(context, default=str, sort_keys=True))
    else:
        logger.info("[verbose] %s", message)


def save_config(config: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def get_logging_settings() -> dict:
    config = load_config()
    logging_cfg = config.get("logging", {})
    current_level = logging_cfg.get("level", "info")
    if current_level not in LOGGING_LEVEL_OPTIONS:
        current_level = "info"
    return {
        "level": current_level,
        "options": LOGGING_LEVEL_OPTIONS,
    }


def get_verbose_default_settings() -> dict:
    config = load_config()
    agent_defaults = config.get("agents", {}).get("defaults", {})
    current_value = agent_defaults.get("verboseDefault", "off")
    if current_value not in VERBOSE_DEFAULT_OPTIONS:
        current_value = "off"
    return {
        "value": current_value,
        "options": VERBOSE_DEFAULT_OPTIONS,
    }


def save_logging_level(level: str):
    if level not in LOGGING_LEVEL_OPTIONS:
        raise ValueError(f"Invalid logging level: {level}")
    config = load_config()
    logging_cfg = config.setdefault("logging", {})
    logging_cfg["level"] = level
    save_config(config)


def save_verbose_default(value: str):
    if value not in VERBOSE_DEFAULT_OPTIONS:
        raise ValueError(f"Invalid verboseDefault value: {value}")
    config = load_config()
    agents_cfg = config.setdefault("agents", {})
    defaults_cfg = agents_cfg.setdefault("defaults", {})
    defaults_cfg["verboseDefault"] = value
    save_config(config)


def run_sync_script(dry_run: bool = False) -> dict:
    """Run the sync-skill-access.sh script and return output."""
    if not os.path.exists(SYNC_SCRIPT_PATH):
        return {"ok": False, "error": f"Sync script not found at {SYNC_SCRIPT_PATH}"}
    cmd = ["bash", SYNC_SCRIPT_PATH]
    if dry_run:
        cmd.append("--dry-run")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Sync script timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def parse_skill_md(path: str) -> dict:
    """Parse a SKILL.md file, handling XML-tag and YAML frontmatter formats."""
    text = Path(path).read_text(encoding="utf-8")
    name = ""
    description = ""
    instructions = ""

    xml_match = re.match(
        r"\s*<skill>\s*<name>(.*?)</name>\s*<description>(.*?)</description>\s*</skill>(.*)",
        text, re.DOTALL,
    )
    if xml_match:
        name = xml_match.group(1).strip()
        description = xml_match.group(2).strip()
        instructions = xml_match.group(3).strip()
    else:
        fm_match = re.match(r"\s*---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
        if fm_match:
            frontmatter = fm_match.group(1)
            instructions = fm_match.group(2).strip()
            for line in frontmatter.splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip('"').strip("'")
        else:
            instructions = text.strip()

    return {"name": name, "description": description, "instructions": instructions}


def serialize_skill_md(name: str, description: str, instructions: str) -> str:
    return (
        f"<skill>\n"
        f"  <name>{name}</name>\n"
        f"  <description>{description}</description>\n"
        f"</skill>\n\n"
        f"{instructions}\n"
    )


def get_model_display(model) -> str:
    """Extract a display-friendly model string."""
    if isinstance(model, dict):
        primary = model.get("primary", "unknown")
        return primary
    return str(model) if model else "unknown"


# ──────────────────────────────────────────────────────────────
# Data Scanners
# ──────────────────────────────────────────────────────────────

def scan_agents() -> list[dict]:
    """Discover agents from openclaw.json config (source of truth), then enrich with workspace files."""
    agents = []
    config = load_config()
    agent_list = config.get("agents", {}).get("list", [])
    cls_map = load_classifications_map()
    access_data = load_skill_access()
    skill_tags_map = access_data.get("skills", {})
    agent_tags_map = access_data.get("agents", {})
    agent_metadata = load_agent_metadata()

    # Pre-scan global skills for tag-based access resolution
    global_skills_list = []
    if os.path.isdir(GLOBAL_SKILLS_DIR):
        for sdir in sorted(os.listdir(GLOBAL_SKILLS_DIR)):
            spath = os.path.join(GLOBAL_SKILLS_DIR, sdir)
            if not os.path.isdir(spath):
                continue
            skill_md = os.path.join(spath, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            parsed = parse_skill_md(skill_md)
            skill_tags = skill_tags_map.get(sdir, {}).get("tags", [])
            global_skills_list.append({"folder": sdir, "tags": skill_tags, **parsed})

    # Track which agent IDs are claimed by config entries
    claimed_agent_ids = set()

    # Iterate over agents.list[] from config — this is the source of truth
    for agent_cfg in agent_list:
        name = agent_cfg.get("id", "")
        if not name:
            continue

        # Resolve workspace path: explicit config > convention > missing
        ws_dir = agent_cfg.get("workspace", "")
        if not ws_dir:
            # Convention: "main" uses ~/.openclaw/workspace, others use workspace-{id}
            if name == "main":
                ws_dir = MAIN_WORKSPACE_DIR
            else:
                ws_dir = os.path.join(OCPLATFORM_DIR, f"workspace-{name}")

        has_workspace = os.path.isdir(ws_dir)
        claimed_agent_ids.add(name)

        # Read workspace files (only if dir exists)
        files = {}
        if has_workspace:
            for fname in ["SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md", "TOOLS.md", "MEMORY.md", "MODEL"]:
                fpath = os.path.join(ws_dir, fname)
                if os.path.isfile(fpath):
                    files[fname] = Path(fpath).read_text(encoding="utf-8")

        # Parse identity from IDENTITY.md
        identity_data = {}
        if "IDENTITY.md" in files:
            for line in files["IDENTITY.md"].splitlines():
                line = line.strip()
                if line.startswith("- **") and ":**" in line:
                    key = line.split("**")[1].replace(":", "").strip().lower()
                    val = line.split(":**")[1].strip()
                    identity_data[key] = val

        # Get model from config, fall back to MODEL file, then defaults
        model_raw = agent_cfg.get("model", "")
        if not model_raw and has_workspace and "MODEL" in files:
            model_raw = files["MODEL"].strip()
        if not model_raw:
            # Fall back to agents.defaults.model from config
            defaults_model = config.get("agents", {}).get("defaults", {}).get("model", "")
            if defaults_model:
                model_raw = defaults_model
        if not model_raw:
            model_raw = "unknown"
        model = get_model_display(model_raw)

        # Display name: config identity > IDENTITY.md > id titlecased
        cfg_identity = agent_cfg.get("identity", {})
        display_name = (
            cfg_identity.get("name")
            or identity_data.get("name")
            or agent_cfg.get("name")
            or name.title()
        )

        # Theme from config identity
        theme = cfg_identity.get("theme", "")

        # Classification (legacy) and tags (new)
        classification = cls_map.get(display_name, cls_map.get(name, ""))
        agent_tags = agent_tags_map.get(name, {}).get("tags", [])

        # Skills explicitly configured in openclaw.json
        config_skills = agent_cfg.get("skills", [])

        # Tools config
        tools = agent_cfg.get("tools", {})

        # Scan per-agent skills (only if workspace exists)
        agent_skills = []
        if has_workspace:
            skills_dir = os.path.join(ws_dir, "skills")
            if os.path.isdir(skills_dir):
                for sdir in sorted(os.listdir(skills_dir)):
                    spath = os.path.join(skills_dir, sdir)
                    if os.path.isdir(spath):
                        skill_md = os.path.join(spath, "SKILL.md")
                        has_md = os.path.isfile(skill_md)
                        skill_info = {"folder": sdir, "has_skill_md": has_md}
                        if has_md:
                            parsed = parse_skill_md(skill_md)
                            skill_info.update(parsed)
                        agent_skills.append(skill_info)

        # Resolve accessible global skills based on tags
        if agent_tags:
            agent_global_skills = [
                s for s in global_skills_list
                if not s["tags"] or bool(set(s["tags"]) & set(agent_tags))
            ]
        else:
            agent_global_skills = [
                s for s in global_skills_list
                if not s.get("tags")
            ]

        # Enrich with metadata (organization + apps)
        meta = agent_metadata.get(name, {})

        agents.append({
            "name": name,
            "display_name": display_name,
            "theme": theme,
            "path": ws_dir if has_workspace else "",
            "has_workspace": has_workspace,
            "model": model,
            "model_raw": model_raw,
            "classification": classification,
            "agent_tags": agent_tags,
            "identity": identity_data,
            "config_identity": cfg_identity,
            "tools": tools,
            "skills": agent_skills,
            "skill_count": len(agent_skills),
            "config_skills": config_skills,
            "total_skill_count": len(set(config_skills) | {s["folder"] for s in agent_skills}),
            "global_skills": agent_global_skills,
            "global_skill_count": len(agent_global_skills),
            "files": {k: True for k in files},
            "soul": files.get("SOUL.md", ""),
            "identity_md": files.get("IDENTITY.md", ""),
            "orphan": False,
            "organization": meta.get("organization", ""),
            "apps": meta.get("apps", []),
        })

    # Detect orphan workspace dirs (exist on disk but not in config)
    all_ws_dirs = sorted(glob.glob(WORKSPACE_GLOB))
    if os.path.isdir(MAIN_WORKSPACE_DIR):
        all_ws_dirs.append(MAIN_WORKSPACE_DIR)

    for ws_dir in all_ws_dirs:
        basename = os.path.basename(ws_dir)
        name = "main" if basename == "workspace" else basename.replace("workspace-", "", 1)
        if name in claimed_agent_ids:
            continue
        # Orphan workspace — not in config

        files = {}
        for fname in ["SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md", "TOOLS.md", "MEMORY.md", "MODEL"]:
            fpath = os.path.join(ws_dir, fname)
            if os.path.isfile(fpath):
                files[fname] = Path(fpath).read_text(encoding="utf-8")

        identity_data = {}
        if "IDENTITY.md" in files:
            for line in files["IDENTITY.md"].splitlines():
                line = line.strip()
                if line.startswith("- **") and ":**" in line:
                    key = line.split("**")[1].replace(":", "").strip().lower()
                    val = line.split(":**")[1].strip()
                    identity_data[key] = val

        display_name = identity_data.get("name", name.title())

        agents.append({
            "name": name,
            "display_name": display_name,
            "theme": "",
            "path": ws_dir,
            "has_workspace": True,
            "model": "unknown",
            "model_raw": "unknown",
            "classification": "",
            "agent_tags": [],
            "identity": identity_data,
            "config_identity": {},
            "tools": {},
            "skills": [],
            "skill_count": 0,
            "global_skills": [],
            "global_skill_count": 0,
            "files": {k: True for k in files},
            "soul": files.get("SOUL.md", ""),
            "identity_md": files.get("IDENTITY.md", ""),
            "orphan": True,
        })

    return agents


def scan_all_skills() -> list[dict]:
    """Scan skills from all 3 tiers."""
    skills = []

    # Tier 1: NPM-installed (platform)
    if os.path.isdir(NPM_SKILLS_DIR):
        for sdir in sorted(os.listdir(NPM_SKILLS_DIR)):
            spath = os.path.join(NPM_SKILLS_DIR, sdir)
            if not os.path.isdir(spath):
                continue
            skill_md = os.path.join(spath, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            parsed = parse_skill_md(skill_md)
            skills.append({
                "id": f"npm/{sdir}",
                "folder": sdir,
                "tier": "platform",
                "tier_label": "Platform (npm)",
                "source": "npm",
                "agent": None,
                "path": skill_md,
                **parsed,
            })

    # Tier 2: User global
    access_data = load_skill_access()
    skill_tags_map = access_data.get("skills", {})
    if os.path.isdir(GLOBAL_SKILLS_DIR):
        for sdir in sorted(os.listdir(GLOBAL_SKILLS_DIR)):
            spath = os.path.join(GLOBAL_SKILLS_DIR, sdir)
            if not os.path.isdir(spath):
                continue
            skill_md = os.path.join(spath, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            parsed = parse_skill_md(skill_md)
            tags = skill_tags_map.get(sdir, {}).get("tags", [])
            skills.append({
                "id": f"global/{sdir}",
                "folder": sdir,
                "tier": "global",
                "tier_label": "Global (shared)",
                "source": "~/.openclaw/skills",
                "agent": None,
                "tags": tags,
                "path": skill_md,
                **parsed,
            })

    # Tier 3: Per-agent (workspace-* and main workspace)
    all_agent_skill_dirs = sorted(glob.glob(SKILLS_GLOB)) + sorted(glob.glob(MAIN_SKILLS_GLOB))
    for skill_dir in all_agent_skill_dirs:
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        parts = skill_dir.split("/skills/")[0]
        basename = os.path.basename(parts)
        workspace = "main" if basename == "workspace" else basename.replace("workspace-", "", 1)
        folder_name = os.path.basename(skill_dir)
        parsed = parse_skill_md(skill_md)
        skills.append({
            "id": f"{workspace}/{folder_name}",
            "folder": folder_name,
            "tier": "agent",
            "tier_label": f"Agent ({workspace})",
            "source": "workspace" if workspace == "main" else f"workspace-{workspace}",
            "agent": workspace,
            "path": skill_md,
            **parsed,
        })

    return skills


def scan_classifications() -> list[dict]:
    results = []
    if os.path.isdir(CLASSIFICATIONS_DIR):
        for f in sorted(os.listdir(CLASSIFICATIONS_DIR)):
            if f.endswith(".md"):
                path = os.path.join(CLASSIFICATIONS_DIR, f)
                name = f.replace(".md", "")
                content = Path(path).read_text(encoding="utf-8")
                results.append({"name": name, "path": path, "content": content})
    return results


def build_skill_matrix() -> dict:
    """Build a matrix of agent × skill for the overview."""
    agents = scan_agents()
    all_skills = scan_all_skills()

    # Unique skill names across all tiers
    skill_names = sorted(set(s["folder"] for s in all_skills))

    matrix = {}
    for agent in agents:
        agent_skill_names = [s["folder"] for s in agent["skills"]]
        matrix[agent["name"]] = {
            "skills": agent_skill_names,
            "model": agent["model"],
            "classification": agent["classification"],
        }

    return {
        "agents": [a["name"] for a in agents],
        "skill_names": skill_names,
        "matrix": matrix,
        "npm_skills": [s["folder"] for s in all_skills if s["tier"] == "platform"],
        "global_skills": [s["folder"] for s in all_skills if s["tier"] == "global"],
    }


# ──────────────────────────────────────────────────────────────
# API Routes
# ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# --- Dashboard ---

@app.get("/api/dashboard")
async def dashboard():
    agents = scan_agents()
    skills = scan_all_skills()
    classifications = scan_classifications()
    cls_map = load_classifications_map()
    skill_access = load_skill_access()
    ui_settings = load_ui_settings()
    logging_settings = get_logging_settings()
    verbose_default_settings = get_verbose_default_settings()

    return {
        "agents": agents,
        "skill_summary": {
            "platform": len([s for s in skills if s["tier"] == "platform"]),
            "global": len([s for s in skills if s["tier"] == "global"]),
            "agent": len([s for s in skills if s["tier"] == "agent"]),
            "total": len(skills),
        },
        "classifications": classifications,
        "classifications_map": cls_map,
        "skill_access": skill_access,
        "ui_settings": ui_settings,
        "logging_settings": logging_settings,
        "verbose_default_settings": verbose_default_settings,
    }


# --- Skill Access (tag-based) ---

@app.get("/api/skill-access")
async def get_skill_access_config():
    """Return the full skill-access.json config."""
    return load_skill_access()


@app.put("/api/skill-access")
async def save_skill_access_config(request: Request):
    """Save the full skill-access.json config."""
    data = await request.json()
    save_skill_access(data)
    log_verbose("Saved skill access config")
    return {"ok": True}


@app.get("/api/settings")
async def get_ui_settings():
    return {
        "ui_settings": load_ui_settings(),
        "logging_settings": get_logging_settings(),
        "verbose_default_settings": get_verbose_default_settings(),
    }


class UISettingsUpdate(BaseModel):
    verbose_logging: bool


@app.put("/api/settings")
async def save_settings(body: UISettingsUpdate):
    data = load_ui_settings()
    data["verbose_logging"] = body.verbose_logging
    save_ui_settings(data)
    logger.setLevel(logging.INFO)
    logger.info("UI verbose logging %s", "enabled" if body.verbose_logging else "disabled")
    return {
        "ok": True,
        "settings": data,
        "logging_settings": get_logging_settings(),
        "verbose_default_settings": get_verbose_default_settings(),
    }


class VerboseDefaultUpdate(BaseModel):
    value: str


@app.put("/api/settings/verbose-default")
async def update_verbose_default(body: VerboseDefaultUpdate):
    try:
        save_verbose_default(body.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log_verbose("Updated OpenClaw verboseDefault", value=body.value)
    return {"ok": True, "verbose_default_settings": get_verbose_default_settings()}


class LoggingLevelUpdate(BaseModel):
    level: str


@app.put("/api/settings/logging-level")
async def update_logging_level(body: LoggingLevelUpdate):
    try:
        save_logging_level(body.level)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log_verbose("Updated OpenClaw logging level", level=body.level)
    return {"ok": True, "logging_settings": get_logging_settings()}


class TagCreate(BaseModel):
    name: str
    description: str = ""


@app.post("/api/skill-access/tags")
async def create_tag(body: TagCreate):
    data = load_skill_access()
    if body.name in data["tags"]:
        raise HTTPException(status_code=409, detail="Tag already exists")
    data["tags"][body.name] = {"description": body.description}
    save_skill_access(data)
    return {"ok": True}


@app.delete("/api/skill-access/tags/{name}")
async def delete_tag(name: str):
    data = load_skill_access()
    if name not in data["tags"]:
        raise HTTPException(status_code=404, detail="Tag not found")
    del data["tags"][name]
    # Remove tag from all skills and agents
    for skill in data["skills"].values():
        if name in skill.get("tags", []):
            skill["tags"].remove(name)
    for agent in data["agents"].values():
        if name in agent.get("tags", []):
            agent["tags"].remove(name)
    save_skill_access(data)
    return {"ok": True}


class SkillTagsUpdate(BaseModel):
    folder: str
    tags: List[str]


@app.put("/api/skill-access/skill-tags")
async def update_skill_tags(body: SkillTagsUpdate):
    data = load_skill_access()
    if body.tags:
        data["skills"][body.folder] = {"tags": body.tags}
    else:
        data["skills"].pop(body.folder, None)
    save_skill_access(data)
    return {"ok": True}


class AgentTagsUpdate(BaseModel):
    agent_id: str
    tags: List[str]


@app.put("/api/skill-access/agent-tags")
async def update_agent_tags(body: AgentTagsUpdate):
    data = load_skill_access()
    if body.tags:
        existing = data["agents"].get(body.agent_id, {})
        existing["tags"] = body.tags
        data["agents"][body.agent_id] = existing
    else:
        data["agents"].pop(body.agent_id, None)
    save_skill_access(data)
    return {"ok": True}


class SyncRequest(BaseModel):
    dry_run: bool = False


@app.post("/api/skill-access/sync")
async def sync_skill_access(body: SyncRequest):
    """Run the sync script to apply skill-access.json to openclaw.json."""
    result = run_sync_script(dry_run=body.dry_run)
    return result


# --- Gateway ---

@app.post("/api/gateway/restart")
async def api_restart_gateway():
    """Restart the OpenClaw gateway."""
    try:
        log_verbose("Restarting gateway from UI")
        subprocess.Popen([OCPLATFORM_BIN, "gateway", "restart"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Diagnose ---


DIAGNOSE_PROMPT_PATH = os.path.join(OCPLATFORM_DIR, "scripts", "slack-debug-prompt.md")


@app.post("/api/diagnose")
async def api_diagnose():
    """Run the OCPlatform diagnostic using Claude CLI."""
    if not os.path.isfile(DIAGNOSE_PROMPT_PATH):
        raise HTTPException(status_code=404, detail=f"Diagnostic prompt not found at {DIAGNOSE_PROMPT_PATH}")
    try:
        prompt_content = Path(DIAGNOSE_PROMPT_PATH).read_text(encoding="utf-8")
        log_verbose("Running OpenClaw diagnostic")
        proc = await asyncio.create_subprocess_exec(
            "claude", "--print", "-p", prompt_content,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {"ok": False, "error": "Diagnostic timed out after 120 seconds"}
        output = stdout.decode("utf-8", errors="replace")
        err_output = stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            return {"ok": False, "output": output, "error": err_output or f"Process exited with code {proc.returncode}"}
        return {"ok": True, "output": output}
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="Claude CLI not found. Is 'claude' installed and in PATH?")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Skills ---


@app.get("/api/skills")
async def list_skills(tier: Optional[str] = None, agent: Optional[str] = None):
    skills = scan_all_skills()
    if tier:
        skills = [s for s in skills if s["tier"] == tier]
    if agent:
        skills = [s for s in skills if s["agent"] == agent]
    return skills


@app.get("/api/skills/{tier}/{folder:path}")
async def read_skill(tier: str, folder: str):
    skills = scan_all_skills()
    skill_id = f"{tier}/{folder}"
    for s in skills:
        if s["id"] == skill_id:
            return s
    raise HTTPException(status_code=404, detail="Skill not found")


class SkillUpdate(BaseModel):
    name: str
    description: str
    instructions: str


@app.put("/api/skills/{workspace}/{folder}")
async def update_skill(workspace: str, folder: str):
    # Handle the different tier paths
    pass


@app.put("/api/skills/agent/{workspace}/{folder}")
async def update_agent_skill(workspace: str, folder: str, body: SkillUpdate):
    skill_path = os.path.join(resolve_skill_dir(workspace, folder), "SKILL.md")
    if not os.path.exists(skill_path):
        raise HTTPException(status_code=404, detail="Skill not found")
    content = serialize_skill_md(body.name, body.description, body.instructions)
    Path(skill_path).write_text(content, encoding="utf-8")
    restart_gateway()
    return {"ok": True}


@app.put("/api/skills/global/{folder}")
async def update_global_skill(folder: str, body: SkillUpdate):
    skill_path = os.path.join(GLOBAL_SKILLS_DIR, folder, "SKILL.md")
    if not os.path.exists(skill_path):
        raise HTTPException(status_code=404, detail="Skill not found")
    content = serialize_skill_md(body.name, body.description, body.instructions)
    Path(skill_path).write_text(content, encoding="utf-8")
    restart_gateway()
    return {"ok": True}


class SkillCreate(BaseModel):
    workspace: str
    folder: str
    name: str
    description: str
    instructions: str


@app.post("/api/skills")
async def create_skill(body: SkillCreate):
    if body.workspace == "__global__":
        base = os.path.join(GLOBAL_SKILLS_DIR, body.folder)
    else:
        base = resolve_skill_dir(body.workspace, body.folder)
    skill_md = os.path.join(base, "SKILL.md")
    if os.path.exists(skill_md):
        raise HTTPException(status_code=409, detail="Skill already exists")
    os.makedirs(base, exist_ok=True)
    content = serialize_skill_md(body.name, body.description, body.instructions)
    Path(skill_md).write_text(content, encoding="utf-8")
    restart_gateway()
    return {"ok": True, "id": f"{body.workspace}/{body.folder}"}


@app.post("/api/skills/upload")
async def upload_skill(request: Request):
    """Upload skills from zip files or raw folder contents.
    
    Accepts multipart form data with:
    - workspace: target workspace (default: __global__)
    - files: one or more .zip files OR raw files with webkitRelativePath paths
    """
    import tempfile
    import zipfile

    form = await request.form()
    workspace = form.get("workspace", "__global__")
    
    # Collect all uploaded files
    uploaded_files = []
    for key in form:
        if key == 'workspace':
            continue
        items = form.getlist(key)
        for item in items:
            if hasattr(item, 'filename') and item.filename:
                uploaded_files.append(item)

    if not uploaded_files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    created = []
    errors = []

    with tempfile.TemporaryDirectory() as tmp:
        # Check if these are zip files or raw folder contents
        zip_files = [f for f in uploaded_files if f.filename.endswith(".zip")]
        raw_files = [f for f in uploaded_files if not f.filename.endswith(".zip")]

        # Process zip files
        for zf_upload in zip_files:
            zip_tmp = os.path.join(tmp, f"zip_{zf_upload.filename}")
            os.makedirs(zip_tmp, exist_ok=True)
            content = await zf_upload.read()
            zip_path = os.path.join(zip_tmp, "upload.zip")
            with open(zip_path, "wb") as f:
                f.write(content)
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(zip_tmp)
            except zipfile.BadZipFile:
                errors.append(f"{zf_upload.filename}: invalid zip")
                continue

            # Find skill folders
            for root, dirs, files in os.walk(zip_tmp):
                dirs[:] = [d for d in dirs if not d.startswith("__") and not d.startswith(".")]
                if "SKILL.md" in files:
                    folder_name = os.path.basename(root)
                    if not folder_name or folder_name == os.path.basename(zip_tmp):
                        folder_name = os.path.splitext(zf_upload.filename)[0]
                    result = _install_skill_folder(root, folder_name, workspace)
                    if result["ok"]:
                        created.append(result["name"])
                    else:
                        errors.append(result["error"])

        # Process raw folder uploads (from webkitdirectory)
        if raw_files:
            raw_tmp = os.path.join(tmp, "raw")
            os.makedirs(raw_tmp, exist_ok=True)
            for rf in raw_files:
                # filename contains the relative path like "my-skill/SKILL.md"
                rel_path = rf.filename
                dest_path = os.path.join(raw_tmp, rel_path)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                content = await rf.read()
                with open(dest_path, "wb") as f:
                    f.write(content)

            # Find skill folders in the reconstructed tree
            for root, dirs, files in os.walk(raw_tmp):
                dirs[:] = [d for d in dirs if not d.startswith("__") and not d.startswith(".")]
                if "SKILL.md" in files:
                    folder_name = os.path.basename(root)
                    if not folder_name or folder_name == "raw":
                        folder_name = "uploaded-skill"
                    result = _install_skill_folder(root, folder_name, workspace)
                    if result["ok"]:
                        created.append(result["name"])
                    else:
                        errors.append(result["error"])

    if not created and errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    if created:
        restart_gateway()

    return {"ok": True, "created": created, "count": len(created), "errors": errors}


def _install_skill_folder(src_path: str, folder_name: str, workspace: str) -> dict:
    """Copy a skill folder to the target location. Returns {ok, name} or {ok, error}."""
    if workspace == "__global__":
        dest = os.path.join(GLOBAL_SKILLS_DIR, folder_name)
    else:
        dest = resolve_skill_dir(workspace, folder_name)

    if os.path.exists(dest):
        return {"ok": False, "error": f"Skill '{folder_name}' already exists at target"}

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copytree(src_path, dest)
    return {"ok": True, "name": folder_name}


@app.delete("/api/skills/{workspace}/{folder}")
async def delete_skill(workspace: str, folder: str):
    if workspace == "__global__":
        skill_dir = os.path.join(GLOBAL_SKILLS_DIR, folder)
    else:
        skill_dir = resolve_skill_dir(workspace, folder)
    if not os.path.isdir(skill_dir):
        raise HTTPException(status_code=404, detail="Skill not found")
    shutil.rmtree(skill_dir)
    restart_gateway()
    return {"ok": True}


class SkillPromote(BaseModel):
    source_agent: str  # agent name (workspace)
    folder: str
    remove_original: bool = True  # move vs copy to global


@app.post("/api/skills/promote-to-global")
async def promote_skill_to_global(body: SkillPromote):
    """Promote a per-agent skill to global (~/.openclaw/skills/).
    
    This moves (or copies) the skill folder from workspace-<agent>/skills/<folder>
    to ~/.openclaw/skills/<folder>, making it available to all agents.
    """
    src = resolve_skill_dir(body.source_agent, body.folder)
    dst = os.path.join(GLOBAL_SKILLS_DIR, body.folder)

    if not os.path.isdir(src):
        raise HTTPException(status_code=404, detail=f"Source skill not found at {src}")
    if os.path.exists(dst):
        raise HTTPException(status_code=409, detail=f"A global skill named '{body.folder}' already exists")

    os.makedirs(GLOBAL_SKILLS_DIR, exist_ok=True)
    if body.remove_original:
        shutil.move(src, dst)
    else:
        shutil.copytree(src, dst)
    restart_gateway()
    return {"ok": True, "new_id": f"global/{body.folder}"}


class SkillCopy(BaseModel):
    source_agent: str  # agent name or "__global__"
    target_agent: str  # agent name or "__global__"
    folder: str


@app.post("/api/skills/copy")
async def copy_skill(body: SkillCopy):
    """Copy a skill from one agent to another (or to/from global)."""
    if body.source_agent == "__global__":
        src = os.path.join(GLOBAL_SKILLS_DIR, body.folder)
    else:
        src = resolve_skill_dir(body.source_agent, body.folder)

    if body.target_agent == "__global__":
        dst = os.path.join(GLOBAL_SKILLS_DIR, body.folder)
    else:
        dst = resolve_skill_dir(body.target_agent, body.folder)

    if not os.path.isdir(src):
        raise HTTPException(status_code=404, detail="Source skill not found")
    if os.path.exists(dst):
        raise HTTPException(status_code=409, detail="Skill already exists at target")

    shutil.copytree(src, dst)
    restart_gateway()
    return {"ok": True}


class SkillMove(BaseModel):
    source_agent: str
    target_agent: str
    folder: str


@app.post("/api/skills/move")
async def move_skill(body: SkillMove):
    """Move a skill from one agent to another."""
    if body.source_agent == "__global__":
        src = os.path.join(GLOBAL_SKILLS_DIR, body.folder)
    else:
        src = resolve_skill_dir(body.source_agent, body.folder)

    if body.target_agent == "__global__":
        dst = os.path.join(GLOBAL_SKILLS_DIR, body.folder)
    else:
        dst = resolve_skill_dir(body.target_agent, body.folder)

    if not os.path.isdir(src):
        raise HTTPException(status_code=404, detail="Source skill not found")
    if os.path.exists(dst):
        raise HTTPException(status_code=409, detail="Skill already exists at target")

    shutil.move(src, dst)
    restart_gateway()
    return {"ok": True}


# --- Agents ---

@app.get("/api/agents")
async def list_agents():
    return scan_agents()


@app.get("/api/agents/{name}")
async def read_agent(name: str):
    agents = scan_agents()
    for a in agents:
        if a["name"] == name:
            return a
    raise HTTPException(status_code=404, detail="Agent not found")


class SoulUpdate(BaseModel):
    content: str


@app.put("/api/agents/{name}/soul")
async def update_soul(name: str, body: SoulUpdate):
    path = os.path.join(resolve_workspace_dir(name), "SOUL.md")
    if not os.path.exists(os.path.dirname(path)):
        raise HTTPException(status_code=404, detail="Agent workspace not found")
    Path(path).write_text(body.content, encoding="utf-8")
    restart_gateway()
    return {"ok": True}


class IdentityUpdate(BaseModel):
    content: str


@app.put("/api/agents/{name}/identity")
async def update_identity(name: str, body: IdentityUpdate):
    path = os.path.join(resolve_workspace_dir(name), "IDENTITY.md")
    if not os.path.exists(os.path.dirname(path)):
        raise HTTPException(status_code=404, detail="Agent workspace not found")
    Path(path).write_text(body.content, encoding="utf-8")
    restart_gateway()
    return {"ok": True}


# --- Classifications ---

@app.get("/api/classifications")
async def list_classifications():
    return scan_classifications()


@app.get("/api/classifications/map")
async def get_classifications_map():
    return load_classifications_map()


class AgentClassificationUpdate(BaseModel):
    agent_display_name: str
    classification: str  # "" to unset


@app.put("/api/classifications/assign")
async def assign_classification(body: AgentClassificationUpdate):
    cls_map = load_classifications_map()
    if body.classification:
        cls_map[body.agent_display_name] = body.classification
    else:
        cls_map.pop(body.agent_display_name, None)
    save_classifications_map(cls_map)
    restart_gateway()
    return {"ok": True}


class ClassificationUpdate(BaseModel):
    content: str


class ClassificationCreate(BaseModel):
    name: str
    content: str = ""


class ClassificationRename(BaseModel):
    new_name: str


@app.post("/api/classifications")
async def create_classification(body: ClassificationCreate):
    """Create a new classification rule file."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    # Sanitize: only allow alphanumeric, hyphens, underscores
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '-', name).strip('-').lower()
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid name")
    os.makedirs(CLASSIFICATIONS_DIR, exist_ok=True)
    path = os.path.join(CLASSIFICATIONS_DIR, f"{safe_name}.md")
    if os.path.exists(path):
        raise HTTPException(status_code=409, detail=f"Classification '{safe_name}' already exists")
    content = body.content or f"# Classification: {name}\n\nDefine rules for the **{name}** classification here.\n"
    Path(path).write_text(content, encoding="utf-8")
    restart_gateway()
    return {"ok": True, "name": safe_name}


@app.put("/api/classifications/{name}")
async def update_classification(name: str, body: ClassificationUpdate):
    path = os.path.join(CLASSIFICATIONS_DIR, f"{name}.md")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Classification not found")
    Path(path).write_text(body.content, encoding="utf-8")
    restart_gateway()
    return {"ok": True}


@app.put("/api/classifications/{name}/rename")
async def rename_classification(name: str, body: ClassificationRename):
    """Rename a classification: renames file and updates agent-classifications.json."""
    old_path = os.path.join(CLASSIFICATIONS_DIR, f"{name}.md")
    if not os.path.exists(old_path):
        raise HTTPException(status_code=404, detail="Classification not found")
    new_name = re.sub(r'[^a-zA-Z0-9_-]', '-', body.new_name.strip()).strip('-').lower()
    if not new_name:
        raise HTTPException(status_code=400, detail="Invalid new name")
    if new_name == name:
        return {"ok": True, "name": name}
    new_path = os.path.join(CLASSIFICATIONS_DIR, f"{new_name}.md")
    if os.path.exists(new_path):
        raise HTTPException(status_code=409, detail=f"Classification '{new_name}' already exists")
    os.rename(old_path, new_path)
    # Update agent-classifications.json references
    cls_map = load_classifications_map()
    updated = False
    for agent_key, cls_val in list(cls_map.items()):
        if cls_val == name:
            cls_map[agent_key] = new_name
            updated = True
    if updated:
        save_classifications_map(cls_map)
    restart_gateway()
    return {"ok": True, "name": new_name}


@app.delete("/api/classifications/{name}")
async def delete_classification(name: str):
    """Delete a classification rule file and unassign all agents from it."""
    path = os.path.join(CLASSIFICATIONS_DIR, f"{name}.md")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Classification not found")
    os.remove(path)
    # Unassign agents that were in this classification
    cls_map = load_classifications_map()
    updated = False
    for agent_key, cls_val in list(cls_map.items()):
        if cls_val == name:
            del cls_map[agent_key]
            updated = True
    if updated:
        save_classifications_map(cls_map)
    restart_gateway()
    return {"ok": True}


# --- Skill Matrix ---

@app.get("/api/matrix")
async def skill_matrix():
    return build_skill_matrix()


# --- Agent Photos ---

def _agent_photo_path(agent_id: str) -> Optional[str]:
    """Return path to agent's custom photo, or None if not set."""
    for ext in ("png", "jpg", "jpeg", "gif", "webp"):
        p = os.path.join(AGENT_PHOTOS_DIR, f"{agent_id}.{ext}")
        if os.path.isfile(p):
            return p
    return None


def _photo_media_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(ext, "application/octet-stream")


@app.get("/api/agents/{name}/photo")
async def agent_photo(name: str):
    path = _agent_photo_path(name)
    if path:
        # Cache-bust: use file mtime as etag so browser refetches on update
        mtime = str(int(os.path.getmtime(path)))
        return FileResponse(
            path,
            media_type=_photo_media_type(path),
            headers={"Cache-Control": "no-cache, must-revalidate", "ETag": f'"{mtime}"'},
        )
    # Serve default
    if os.path.isfile(DEFAULT_PHOTO_PATH):
        return FileResponse(DEFAULT_PHOTO_PATH, media_type="image/png")
    raise HTTPException(status_code=404, detail="No photo found")


@app.post("/api/agents/{name}/photo")
async def upload_agent_photo(name: str, file: UploadFile = File(...)):
    # Remove old photo if exists
    for ext in ("png", "jpg", "jpeg", "gif", "webp"):
        old = os.path.join(AGENT_PHOTOS_DIR, f"{name}.{ext}")
        if os.path.isfile(old):
            os.remove(old)

    suffix = os.path.splitext(file.filename or "photo.png")[1].lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        suffix = ".png"

    dest = os.path.join(AGENT_PHOTOS_DIR, f"{name}{suffix}")
    with open(dest, "wb") as f:
        content = await file.read()
        f.write(content)

    return {"ok": True, "photo_url": f"/api/agents/{name}/photo"}


@app.delete("/api/agents/{name}/photo")
async def delete_agent_photo(name: str):
    removed = False
    for ext in ("png", "jpg", "jpeg", "gif", "webp"):
        p = os.path.join(AGENT_PHOTOS_DIR, f"{name}.{ext}")
        if os.path.isfile(p):
            os.remove(p)
            removed = True
    return {"ok": True, "removed": removed}


# ──────────────────────────────────────────────────────────────
# CONTEXT.md Editor
# ──────────────────────────────────────────────────────────────

CONTEXT_MD_PATH = os.path.join(OCPLATFORM_DIR, "CONTEXT.md")


@app.get("/api/context")
async def get_context():
    """Read ~/.openclaw/CONTEXT.md."""
    content = ""
    if os.path.isfile(CONTEXT_MD_PATH):
        content = Path(CONTEXT_MD_PATH).read_text(encoding="utf-8")
    return {"content": content, "path": CONTEXT_MD_PATH}


class ContextUpdate(BaseModel):
    content: str


@app.put("/api/context")
async def save_context(body: ContextUpdate):
    """Save ~/.openclaw/CONTEXT.md."""
    os.makedirs(os.path.dirname(CONTEXT_MD_PATH), exist_ok=True)
    Path(CONTEXT_MD_PATH).write_text(body.content, encoding="utf-8")
    log_verbose("Saved CONTEXT.md")
    return {"ok": True, "path": CONTEXT_MD_PATH}


# ──────────────────────────────────────────────────────────────
# Cron Jobs Manager
# ──────────────────────────────────────────────────────────────

def _run_openclaw_cmd(args: list, timeout: int = 30) -> dict:
    """Run an openclaw CLI command and return parsed output."""
    try:
        result = subprocess.run(
            [OCPLATFORM_BIN] + args,
            capture_output=True, text=True, timeout=timeout
        )
        stdout = result.stdout.strip()
        # Try to parse as JSON
        if stdout:
            try:
                return {"ok": result.returncode == 0, "data": json.loads(stdout)}
            except json.JSONDecodeError:
                return {"ok": result.returncode == 0, "data": stdout, "raw": True}
        return {
            "ok": result.returncode == 0,
            "data": None,
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Command timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/crons")
async def list_crons():
    """List all cron jobs."""
    result = _run_openclaw_cmd(["cron", "list", "--json"])
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", result.get("stderr", "Failed to list cron jobs")))
    data = result.get("data", {})
    return data if isinstance(data, dict) else {"jobs": []}


@app.get("/api/crons/status")
async def cron_status():
    """Get cron scheduler status."""
    result = _run_openclaw_cmd(["cron", "status", "--json"])
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to get cron status"))
    return result.get("data", {})


@app.get("/api/crons/{cron_id}")
async def get_cron(cron_id: str):
    """Show a specific cron job."""
    result = _run_openclaw_cmd(["cron", "show", cron_id, "--json"])
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", result.get("stderr", "Failed to get cron job")))
    return result.get("data", {})


@app.post("/api/crons/{cron_id}/run")
async def run_cron(cron_id: str):
    """Trigger a cron job to run now."""
    result = _run_openclaw_cmd(["cron", "run", cron_id], timeout=10)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", result.get("stderr", "Failed to run cron job")))
    return {"ok": True, "data": result.get("data")}


@app.put("/api/crons/{cron_id}/enable")
async def enable_cron(cron_id: str):
    """Enable a cron job."""
    result = _run_openclaw_cmd(["cron", "enable", cron_id])
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", result.get("stderr", "Failed to enable cron job")))
    return {"ok": True}


@app.put("/api/crons/{cron_id}/disable")
async def disable_cron(cron_id: str):
    """Disable a cron job."""
    result = _run_openclaw_cmd(["cron", "disable", cron_id])
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", result.get("stderr", "Failed to disable cron job")))
    return {"ok": True}


@app.get("/api/crons/{cron_id}/runs")
async def get_cron_runs(cron_id: str, limit: int = 10):
    """Get run history for a cron job."""
    result = _run_openclaw_cmd(["cron", "runs", "--id", cron_id, "--limit", str(limit)])
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", result.get("stderr", "Failed to get cron runs")))
    data = result.get("data", {})
    return data if isinstance(data, dict) else {"entries": []}


# --- LaunchAgent (launchd) monitoring ---
LAUNCHD_AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
LAUNCHD_METADATA_PATH = os.path.join(OCPLATFORM_DIR, "launchd-metadata.json")


def _load_launchd_metadata() -> dict:
    """Load launchd metadata from third-party JSON (labels may contain aliased strings)."""
    try:
        with open(LAUNCHD_METADATA_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _parse_plist_schedule(plist_data: dict) -> str:
    """Convert plist schedule fields to human-readable string."""
    cal = plist_data.get("StartCalendarInterval")
    if cal is not None:
        if isinstance(cal, dict):
            days = cal.get("Weekday")
            hour = cal.get("Hour", 0)
            minute = cal.get("Minute", 0)
            day_names = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]
            day_str = "" if days is None else f" {day_names[days]} "
            return f"{hour:02d}:{minute:02d}{day_str} daily"
        elif isinstance(cal, list):
            entries = []
            for entry in cal:
                h = entry.get("Hour", 0)
                m = entry.get("Minute", 0)
                entries.append(f"{h:02d}:{m:02d}")
            return ", ".join(entries)
    interval = plist_data.get("StartInterval")
    if interval:
        if interval >= 3600:
            hours = interval / 3600
            return f"Every {hours:.0f}h" if hours == int(hours) else f"Every {hours:.1f}h"
        elif interval >= 60:
            minutes = interval / 60
            return f"Every {minutes:.0f}m" if minutes == int(minutes) else f"Every {minutes:.1f}m"
        return f"Every {interval}s"
    return "On demand"


@app.get("/api/launchd")
async def list_launchd_agents():
    """List all tracked launchd agents with live status."""
    metadata = _load_launchd_metadata()
    if not metadata:
        return {"agents": []}

    agents = []

    # Get live status from launchctl
    try:
        launchctl_out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10
        )
        live_status = {}
        if launchctl_out.returncode == 0:
            for line in launchctl_out.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 3:
                    pid, status, label = parts[0], parts[1], parts[2]
                    live_status[label] = {"pid": pid if pid != "-" else None, "exitStatus": status}
    except Exception as e:
        logging.warning(f"Failed to run launchctl list: {e}")
        live_status = {}

    for label, meta in metadata.items():
        filename = meta.get("filename", f"{label}.plist")
        plist_path = os.path.join(LAUNCHD_AGENTS_DIR, filename)
        # Also check disabled variant
        if not os.path.isfile(plist_path):
            disabled_path = plist_path + ".disabled"
            if os.path.isfile(disabled_path):
                plist_path = disabled_path

        is_disabled = plist_path.endswith(".disabled")
        is_loaded = label in live_status
        live = live_status.get(label, {})
        pid = live.get("pid")
        exit_status = live.get("exitStatus", "?")

        # Parse plist for schedule and log paths
        schedule_human = "unknown"
        program = ""
        log_paths = []
        if os.path.isfile(plist_path):
            try:
                result = subprocess.run(
                    ["plutil", "-convert", "json", "-o", "-", plist_path],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    pdata = json.loads(result.stdout)
                    schedule_human = _parse_plist_schedule(pdata)
                    args = pdata.get("ProgramArguments", [])
                    program = " ".join(args) if args else pdata.get("Program", "")
                    for lp in [pdata.get("StandardOutPath"), pdata.get("StandardErrorPath")]:
                        if lp and lp not in log_paths:
                            log_paths.append(lp)
            except Exception:
                pass

        # Get last run info from launchctl print and log file timestamps
        last_run = None
        run_count = None
        try:
            uid = os.getuid()
            lp_result = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{label}"],
                capture_output=True, text=True, timeout=5
            )
            if lp_result.returncode == 0:
                for line in lp_result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("runs = "):
                        try:
                            run_count = int(line.split("=", 1)[1].strip())
                        except ValueError:
                            pass
                    if line.startswith("last exit code = "):
                        exit_status = line.split("=", 1)[1].strip()
        except Exception:
            pass

        # Use log file mtime as "last run" proxy
        for lp in log_paths:
            if os.path.isfile(lp):
                try:
                    mtime = os.path.getmtime(lp)
                    from datetime import datetime
                    last_run = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                    break
                except Exception:
                    pass

        agents.append({
            "id": label,
            "label": label,
            "name": meta.get("name", label),
            "description": meta.get("description", ""),
            "agentId": meta.get("agentId", "main"),
            "schedule": schedule_human,
            "program": program,
            "loaded": is_loaded,
            "disabled": is_disabled,
            "pid": pid,
            "exitStatus": exit_status,
            "lastRun": last_run,
            "runCount": run_count,
            "status": "disabled" if is_disabled else ("running" if pid else ("loaded" if is_loaded else "unloaded")),
        })

    return {"agents": agents}


@app.post("/api/launchd/sync")
async def sync_launchd_metadata():
    """Regenerate launchd-metadata.json by scanning ~/Library/LaunchAgents/*.plist."""
    agents_dir = LAUNCHD_AGENTS_DIR
    metadata = {}

    # Workspace path -> agent ID mapping
    ws_map = {}
    config_path = os.path.join(OCPLATFORM_DIR, "openclaw.json")
    if os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            for a in cfg.get("agents", {}).get("list", []):
                ws_map[f"workspace-{a['id']}"] = a["id"]
        except Exception:
            pass

    for fname in sorted(os.listdir(agents_dir)):
        is_disabled = fname.endswith(".plist.disabled")
        if not fname.endswith(".plist") and not is_disabled:
            continue
        fp = os.path.join(agents_dir, fname)
        try:
            r = subprocess.run(
                ["plutil", "-convert", "json", "-o", "-", fp],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                continue
            data = json.loads(r.stdout)
            label = data.get("Label", "")

            # Filter to relevant plists (exclude Apple/system/vendor ones)
            skip_prefixes = ("com.apple.", "com.google.", "com.microsoft.", "com.docker.",
                             "org.mozilla.", "com.spotify.", "com.adobe.", "com.1password.",
                             "com.raycast.", "com.logi.", "net.telerik.")
            is_system = label.lower().startswith(skip_prefixes) or fname.lower().startswith(tuple(p for p in skip_prefixes))
            if is_system:
                continue

            # Also include if it references .openclaw paths
            args = data.get("ProgramArguments", [])
            # Accept anything that isn't a known system plist

            # Determine agent from workspace paths
            args = data.get("ProgramArguments", [])
            wd = data.get("WorkingDirectory", "")
            all_paths = " ".join(str(a) for a in args) + " " + wd
            agent_id = "main"
            for ws_key, ws_agent in ws_map.items():
                if ws_key in all_paths:
                    agent_id = ws_agent
                    break

            metadata[label] = {
                "name": label,
                "description": "",
                "agentId": agent_id,
                "filename": fname,
            }
        except Exception as e:
            logging.warning(f"Failed to parse plist {fname}: {e}")

    with open(LAUNCHD_METADATA_PATH, "w") as f:
        json.dump(metadata, f, indent=2)

    return {"ok": True, "count": len(metadata), "labels": list(metadata.keys())}


@app.get("/api/cron-descriptions")
async def get_cron_descriptions():
    """Return saved descriptions for OC cron jobs."""
    desc_path = os.path.join(OCPLATFORM_DIR, "cron-descriptions.json")
    try:
        with open(desc_path) as f:
            return json.load(f)
    except Exception:
        return {}


# --- Gemini description generation ---

def _get_gemini_key() -> str:
    """Load Gemini API key from openclaw config or .env."""
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        try:
            oc_config = json.loads(Path(os.path.expanduser("~/.openclaw/openclaw.json")).read_text())
            key = oc_config.get("env", {}).get("GEMINI_API_KEY", "")
        except Exception:
            pass
    if not key:
        try:
            env_path = os.path.expanduser("~/.openclaw/.env")
            for line in Path(env_path).read_text().splitlines():
                line = line.strip()
                if line.startswith("GEMINI_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass
    return key


@app.put("/api/automations/{job_id}/description")
async def update_automation_description(job_id: str, request: Request):
    """Manually update a description for an automation job."""
    body = await request.json()
    description = body.get("description", "").strip()

    metadata = _load_launchd_metadata()
    if job_id in metadata:
        metadata[job_id]["description"] = description
        with open(LAUNCHD_METADATA_PATH, "w") as f:
            json.dump(metadata, f, indent=2)
    else:
        desc_path = os.path.join(OCPLATFORM_DIR, "cron-descriptions.json")
        try:
            with open(desc_path) as f:
                descs = json.load(f)
        except Exception:
            descs = {}
        descs[job_id] = description
        with open(desc_path, "w") as f:
            json.dump(descs, f, indent=2)

    return {"ok": True, "description": description}


@app.post("/api/automations/{job_id}/generate-description")
async def generate_automation_description(job_id: str):
    """Use Gemini to generate a description for an automation job."""
    import urllib.request

    gemini_key = _get_gemini_key()
    if not gemini_key:
        raise HTTPException(status_code=500, detail="No Gemini API key configured")

    # Gather context about the job
    job_context = f"Job ID/Label: {job_id}\n"

    # Check if it's an OpenClaw cron
    oc_result = _run_openclaw_cmd(["cron", "show", job_id, "--json"])
    if oc_result.get("ok"):
        data = oc_result.get("data", {})
        job_context += f"Type: OpenClaw cron\n"
        job_context += f"Schedule: {json.dumps(data.get('schedule', {}), indent=2)}\n"
        job_context += f"Agent: {data.get('agentId', 'unknown')}\n"
        job_context += f"Name: {data.get('name', '')}\n"
        payload = data.get('payload', {})
        job_context += f"mcp_delegate_task message: {payload.get('message', data.get('message', ''))}\n"
        delivery = data.get('delivery', {})
        job_context += f"Delivers to: {delivery.get('channel', '')} channel {delivery.get('to', '')}\n"
    else:
        # Check if it's a launchd agent
        metadata = _load_launchd_metadata()
        meta = metadata.get(job_id, {})
        plist_path = os.path.join(LAUNCHD_AGENTS_DIR, meta.get("filename", f"{job_id}.plist"))
        if not os.path.isfile(plist_path):
            plist_path = plist_path + ".disabled"

        job_context += f"Type: LaunchAgent (launchd)\n"
        job_context += f"Name: {meta.get('name', job_id)}\n"

        if os.path.isfile(plist_path):
            try:
                r = subprocess.run(
                    ["plutil", "-convert", "json", "-o", "-", plist_path],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    pdata = json.loads(r.stdout)
                    schedule = _parse_plist_schedule(pdata)
                    args = pdata.get("ProgramArguments", [])
                    program = " ".join(str(a) for a in args) if args else pdata.get("Program", "")
                    wd = pdata.get("WorkingDirectory", "")
                    job_context += f"Schedule: {schedule}\n"
                    job_context += f"Program: {program}\n"
                    job_context += f"Working Directory: {wd}\n"

                    # Try to read the script content for more context
                    # Also follow script chains (sh -> py, etc.)
                    script_paths = []
                    for a in args:
                        a_str = str(a)
                        if a_str.endswith((".sh", ".py", ".js", ".ts")):
                            script_paths.append(a_str)
                    # Also check working directory for common entry points
                    if wd and not script_paths:
                        for candidate in ["run.sh", "main.py", "index.js", "app.py"]:
                            cp = os.path.join(wd, candidate)
                            if os.path.isfile(cp):
                                script_paths.append(cp)
                                break

                    total_script_chars = 0
                    for sp in script_paths:
                        if os.path.isfile(sp) and total_script_chars < 6000:
                            try:
                                content = Path(sp).read_text(encoding="utf-8")[:3000]
                                job_context += f"\nScript content ({sp}):\n{content}\n"
                                total_script_chars += len(content)
                                # Follow script chain: look for called scripts
                                import re as _re
                                called = _re.findall(r'["\']?([\w/._-]+\.(?:py|sh|js))["\']?', content)
                                base_dir = os.path.dirname(sp) or wd or "."
                                for called_script in called[:3]:
                                    cp = called_script if os.path.isabs(called_script) else os.path.join(base_dir, called_script)
                                    if os.path.isfile(cp) and cp not in script_paths and total_script_chars < 6000:
                                        try:
                                            c2 = Path(cp).read_text(encoding="utf-8")[:3000]
                                            job_context += f"\nCalled script ({cp}):\n{c2}\n"
                                            total_script_chars += len(c2)
                                            script_paths.append(cp)
                                        except Exception:
                                            pass
                            except Exception:
                                pass
            except Exception:
                pass

    prompt = f"""You are a sysadmin writing descriptions for automation jobs in a dashboard.

Given the following automation job details, write a detailed 2-4 sentence description that covers:
- What this job does step by step
- What services, tools, or APIs are involved
- When/how often it runs and what triggers it
- What the output or end result is

CRITICAL RULES:
- ONLY describe what you can see in the provided details. Do NOT invent or guess services, APIs, or functionality that aren't mentioned.
- If the job details are vague, write a general description based on what IS there. Don't fill gaps with made-up specifics.
- This is a personal Mac mini running OpenClaw (an AI agent platform). There is no LDAP, Jira, Ansible, or enterprise infrastructure unless explicitly mentioned.

Be specific and technical. Write in plain English, no markdown, no quotes. Aim for 150-300 characters.

{job_context}

Description:"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 400, "temperature": 0.3, "thinkingConfig": {"thinkingBudget": 0}}
    }
    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"

    try:
        req = urllib.request.Request(
            gemini_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        candidates = result.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            description = "".join(p.get("text", "") for p in parts).strip()
            # Clean up
            description = description.strip('"').strip("'").strip()
            if len(description) > 400:
                description = description[:397] + "..."
        else:
            raise HTTPException(status_code=500, detail="No response from Gemini")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini API error: {e}")

    # Save the description to the appropriate store
    # For launchd jobs, update launchd-metadata.json
    metadata = _load_launchd_metadata()
    if job_id in metadata:
        metadata[job_id]["description"] = description
        with open(LAUNCHD_METADATA_PATH, "w") as f:
            json.dump(metadata, f, indent=2)
    else:
        # For OC crons, store in a separate descriptions file
        desc_path = os.path.join(OCPLATFORM_DIR, "cron-descriptions.json")
        try:
            with open(desc_path) as f:
                descs = json.load(f)
        except Exception:
            descs = {}
        descs[job_id] = description
        with open(desc_path, "w") as f:
            json.dump(descs, f, indent=2)

    return {"ok": True, "description": description}





# --- OpenClaw Update Info ---
OCPLATFORM_REPO = "openclaw/openclaw"


@app.get("/api/openclaw/status")
async def openclaw_status():
    """Get current installed version and latest GitHub release info."""
    import re as _re

    # Get current version
    current = ""
    try:
        r = subprocess.run(["openclaw", "--version"], capture_output=True, text=True, timeout=5)
        current = r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        pass

    # Get latest release from GitHub
    latest = None
    try:
        r = subprocess.run(
            ["gh", "release", "view", "--repo", OCPLATFORM_REPO, "--json", "tagName,name,publishedAt,body"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            body = data.get("body", "")
            lines = body.split("\n")
            summary_lines = []
            for line in lines:
                if line.startswith("## Fixes") or (line.startswith("## ") and len(summary_lines) > 3):
                    break
                if line.strip():
                    summary_lines.append(line)
                if len(summary_lines) >= 8:
                    break
            summary = "\n".join(summary_lines)
            latest = {
                "version": data.get("tagName", ""),
                "name": data.get("name", ""),
                "publishedAt": data.get("publishedAt", ""),
                "summary": summary[:500],
                "body": body[:5000],
            }
    except Exception:
        pass

    # Compare versions
    is_latest = False
    current_ver = ""
    if latest and current:
        m = _re.search(r'(\d{4}\.\d+\.\d+)', current)
        current_ver = m.group(1) if m else ""
        latest_ver = latest["version"].lstrip("v")
        is_latest = current_ver == latest_ver

    return {
        "current": current,
        "currentVersion": current_ver,
        "latest": latest,
        "isLatest": is_latest,
    }


@app.get("/api/openclaw/changelog")
async def openclaw_changelog():
    """Get recent releases from GitHub."""
    try:
        r = subprocess.run(
            ["gh", "release", "list", "--repo", OCPLATFORM_REPO, "--limit", "10", "--exclude-pre-releases", "--json", "tagName,name,publishedAt,isLatest"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return {"releases": json.loads(r.stdout)}
    except Exception:
        pass
    return {"releases": []}


@app.post("/api/openclaw/summary")
async def openclaw_release_summary():
    """Generate a Gemini summary of the latest release."""
    import urllib.request

    gemini_key = _get_gemini_key()
    if not gemini_key:
        return {"summary": "No Gemini API key available.", "version": ""}

    # Get release notes
    try:
        r = subprocess.run(
            ["gh", "release", "view", "--repo", OCPLATFORM_REPO, "--json", "tagName,name,body"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(r.stdout)
        version = data.get("tagName", "").lstrip("v")
        release_body = data.get("body", "")[:3000]
    except Exception:
        return {"summary": "Failed to fetch release data.", "version": ""}

    if not release_body:
        return {"summary": "No release notes available.", "version": version}

    prompt = (
        "You are summarizing a software release for a personal dashboard. "
        "Write a concise, well-structured summary of OpenClaw version " + version + ".\n\n"
        "Based on the following release notes, write a 3-5 paragraph summary that covers:\n"
        "1. What this release is about (high-level theme)\n"
        "2. Key changes and improvements\n"
        "3. Important bug fixes\n"
        "4. Any breaking changes or things to watch out for\n\n"
        "Keep it informative but conversational. This is for the developer who runs this platform, "
        "not a press release. Use plain text, no markdown headers or bullet points.\n\n"
        "Release notes:\n" + release_body[:2500]
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.5, "thinkingConfig": {"thinkingBudget": 0}}
    }
    gemini_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" + gemini_key

    try:
        req = urllib.request.Request(
            gemini_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        candidates = result.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return {"summary": text, "version": version}
        return {"summary": "Failed to generate summary.", "version": version}
    except Exception as e:
        return {"summary": "Error: " + str(e), "version": version}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)


# ═══════════════════════════════════════════════════════════
# Browser Management
# ═══════════════════════════════════════════════════════════

BROWSER_BASE = os.path.expanduser("~/.openclaw/browser")


def _get_browser_profiles() -> list:
    """Get all active OpenClaw browser profiles with their resource usage."""
    import re
    profiles = {}

    try:
        # Get all Chrome processes with OpenClaw browser paths
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5
        )

        for line in result.stdout.splitlines():
            if ".openclaw/browser/" not in line or "grep" in line:
                continue

            parts = line.split()
            if len(parts) < 11:
                continue

            pid = int(parts[1])
            cpu = float(parts[2])
            mem_pct = float(parts[3])
            rss_kb = int(parts[5])
            started = parts[8] if len(parts) > 8 else ""

            # Extract profile name from --user-data-dir
            match = re.search(r'\.openclaw/browser/([^/]+)/user-data', line)
            if not match:
                continue

            profile = match.group(1)

            if profile not in profiles:
                profiles[profile] = {
                    "profile": profile,
                    "mainPid": None,
                    "pids": [],
                    "totalMemMB": 0,
                    "totalCpu": 0,
                    "processCount": 0,
                    "started": "",
                    "debugPort": None,
                }

            profiles[profile]["pids"].append(pid)
            profiles[profile]["totalMemMB"] += rss_kb / 1024
            profiles[profile]["totalCpu"] += cpu
            profiles[profile]["processCount"] += 1

            # Main process has --remote-debugging-port in its own args (not child helpers)
            if "Google Chrome --remote-debugging-port" in line:
                profiles[profile]["mainPid"] = pid
                profiles[profile]["started"] = started
                port_match = re.search(r'--remote-debugging-port=(\d+)', line)
                if port_match:
                    profiles[profile]["debugPort"] = int(port_match.group(1))

    except Exception as e:
        return [{"error": str(e)}]

    # Round values and sort
    result_list = []
    for p in profiles.values():
        p["totalMemMB"] = round(p["totalMemMB"])
        p["totalCpu"] = round(p["totalCpu"], 1)
        result_list.append(p)

    result_list.sort(key=lambda x: x["totalMemMB"], reverse=True)
    return result_list


@app.get("/api/browsers")
async def list_browsers():
    """List all active OpenClaw browser profiles."""
    profiles = _get_browser_profiles()
    total_mem = sum(p.get("totalMemMB", 0) for p in profiles)
    total_procs = sum(p.get("processCount", 0) for p in profiles)
    return {
        "profiles": profiles,
        "totalMemMB": total_mem,
        "totalProcesses": total_procs,
        "profileCount": len(profiles),
    }


@app.post("/api/browsers/kill/{profile}")
async def kill_browser(profile: str):
    """Kill all Chrome processes for a specific browser profile."""
    if profile == "all":
        # Kill all OpenClaw browser profiles
        profiles = _get_browser_profiles()
        killed = 0
        for p in profiles:
            main_pid = p.get("mainPid")
            if main_pid:
                try:
                    os.kill(main_pid, 15)  # SIGTERM
                    killed += 1
                except ProcessLookupError:
                    pass
        return {"killed": killed, "profiles": [p["profile"] for p in profiles]}
    else:
        # Kill specific profile
        profiles = _get_browser_profiles()
        target = next((p for p in profiles if p["profile"] == profile), None)
        if not target:
            return {"error": f"Profile '{profile}' not found or not running"}
        main_pid = target.get("mainPid")
        if main_pid:
            try:
                os.kill(main_pid, 15)  # SIGTERM the main process, children follow
                return {"killed": True, "profile": profile, "pid": main_pid}
            except ProcessLookupError:
                return {"error": f"Process {main_pid} already gone"}
        else:
            # No main PID, kill all associated pids
            killed = 0
            for pid in target.get("pids", []):
                try:
                    os.kill(pid, 15)
                    killed += 1
                except ProcessLookupError:
                    pass
            return {"killed": killed, "profile": profile}
