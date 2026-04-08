import os
import re
import glob
import subprocess
import logging
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional, List
import json
import shutil


def restart_gateway():
    try:
        subprocess.run(["/opt/homebrew/bin/openclaw", "gateway", "restart"], check=True)
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
    # Fallback (may not exist)
    return "/opt/homebrew/lib/node_modules/openclaw/skills"


app = FastAPI(title="OpenClaw Manager")
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


logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
logger = logging.getLogger("openclaw-skills-ui")


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def resolve_workspace_dir(agent_name: str) -> str:
    """Resolve the workspace directory for an agent. 'main' uses workspace/, others use workspace-<name>/."""
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

        # Get model from config
        model_raw = agent_cfg.get("model", "unknown")
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
            "global_skills": agent_global_skills,
            "global_skill_count": len(agent_global_skills),
            "files": {k: True for k in files},
            "soul": files.get("SOUL.md", ""),
            "identity_md": files.get("IDENTITY.md", ""),
            "orphan": False,
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
        subprocess.Popen(["/opt/homebrew/bin/openclaw", "gateway", "restart"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}
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


@app.put("/api/classifications/{name}")
async def update_classification(name: str, body: ClassificationUpdate):
    path = os.path.join(CLASSIFICATIONS_DIR, f"{name}.md")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Classification not found")
    Path(path).write_text(body.content, encoding="utf-8")
    restart_gateway()
    return {"ok": True}


# --- Skill Matrix ---

@app.get("/api/matrix")
async def skill_matrix():
    return build_skill_matrix()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
