def restart_gateway():
    try:
        subprocess.run(["openclaw", "gateway", "restart"], check=True)
    except Exception as e:
        print("Failed to restart gateway:", e)


import os
import re
import glob
import subprocess
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
import json
import shutil

app = FastAPI(title="OpenClaw Manager")
templates = Jinja2Templates(directory="templates")

OCPLATFORM_DIR = os.path.expanduser("~/.openclaw")
SKILLS_GLOB = os.path.join(OCPLATFORM_DIR, "workspace-*/skills/*")
WORKSPACE_GLOB = os.path.join(OCPLATFORM_DIR, "workspace-*")
NPM_SKILLS_DIR = "/opt/homebrew/lib/node_modules/openclaw/skills"
GLOBAL_SKILLS_DIR = os.path.join(OCPLATFORM_DIR, "skills")
CLASSIFICATIONS_DIR = os.path.join(OCPLATFORM_DIR, "classifications")
CONFIG_PATH = os.path.join(OCPLATFORM_DIR, "openclaw.json")
AGENT_CLS_PATH = os.path.join(OCPLATFORM_DIR, "agent-classifications.json")


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

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
    """Scan all workspace directories and return comprehensive agent info."""
    agents = []
    config = load_config()
    agent_list = config.get("agents", {}).get("list", [])
    cls_map = load_classifications_map()

    # Build a lookup from agent config
    agent_configs = {}
    for a in agent_list:
        aid = a.get("id", "")
        agent_configs[aid] = a

    for ws_dir in sorted(glob.glob(WORKSPACE_GLOB)):
        name = os.path.basename(ws_dir).replace("workspace-", "", 1)
        agent_cfg = agent_configs.get(name, {})

        # Read workspace files
        files = {}
        for fname in ["SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md", "TOOLS.md", "MEMORY.md", "MODEL"]:
            fpath = os.path.join(ws_dir, fname)
            if os.path.isfile(fpath):
                files[fname] = Path(fpath).read_text(encoding="utf-8")

        # Parse identity
        identity_data = {}
        if "IDENTITY.md" in files:
            for line in files["IDENTITY.md"].splitlines():
                line = line.strip()
                if line.startswith("- **") and ":**" in line:
                    key = line.split("**")[1].replace(":", "").strip().lower()
                    val = line.split(":**")[1].strip()
                    identity_data[key] = val

        # Get model
        model_raw = agent_cfg.get("model", "unknown")
        model = get_model_display(model_raw)

        # Get classification
        display_name = identity_data.get("name", name.title())
        classification = cls_map.get(display_name, cls_map.get(name, ""))

        # Get tools config
        tools = agent_cfg.get("tools", {})

        # Scan per-agent skills
        skills_dir = os.path.join(ws_dir, "skills")
        agent_skills = []
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

        agents.append({
            "name": name,
            "display_name": display_name,
            "path": ws_dir,
            "model": model,
            "model_raw": model_raw,
            "classification": classification,
            "identity": identity_data,
            "tools": tools,
            "skills": agent_skills,
            "skill_count": len(agent_skills),
            "files": {k: True for k in files},
            "soul": files.get("SOUL.md", ""),
            "identity_md": files.get("IDENTITY.md", ""),
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
    if os.path.isdir(GLOBAL_SKILLS_DIR):
        for sdir in sorted(os.listdir(GLOBAL_SKILLS_DIR)):
            spath = os.path.join(GLOBAL_SKILLS_DIR, sdir)
            if not os.path.isdir(spath):
                continue
            skill_md = os.path.join(spath, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            parsed = parse_skill_md(skill_md)
            skills.append({
                "id": f"global/{sdir}",
                "folder": sdir,
                "tier": "global",
                "tier_label": "Global (shared)",
                "source": "~/.openclaw/skills",
                "agent": None,
                "path": skill_md,
                **parsed,
            })

    # Tier 3: Per-agent
    for skill_dir in sorted(glob.glob(SKILLS_GLOB)):
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        workspace = skill_dir.split("/skills/")[0].split("workspace-")[-1]
        folder_name = os.path.basename(skill_dir)
        parsed = parse_skill_md(skill_md)
        skills.append({
            "id": f"{workspace}/{folder_name}",
            "folder": folder_name,
            "tier": "agent",
            "tier_label": f"Agent ({workspace})",
            "source": f"workspace-{workspace}",
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
    }


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
    skill_path = os.path.join(OCPLATFORM_DIR, f"workspace-{workspace}", "skills", folder, "SKILL.md")
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
        base = os.path.join(OCPLATFORM_DIR, f"workspace-{body.workspace}", "skills", body.folder)
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
        skill_dir = os.path.join(OCPLATFORM_DIR, f"workspace-{workspace}", "skills", folder)
    if not os.path.isdir(skill_dir):
        raise HTTPException(status_code=404, detail="Skill not found")
    shutil.rmtree(skill_dir)
    restart_gateway()
    return {"ok": True}


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
        src = os.path.join(OCPLATFORM_DIR, f"workspace-{body.source_agent}", "skills", body.folder)

    if body.target_agent == "__global__":
        dst = os.path.join(GLOBAL_SKILLS_DIR, body.folder)
    else:
        dst = os.path.join(OCPLATFORM_DIR, f"workspace-{body.target_agent}", "skills", body.folder)

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
        src = os.path.join(OCPLATFORM_DIR, f"workspace-{body.source_agent}", "skills", body.folder)

    if body.target_agent == "__global__":
        dst = os.path.join(GLOBAL_SKILLS_DIR, body.folder)
    else:
        dst = os.path.join(OCPLATFORM_DIR, f"workspace-{body.target_agent}", "skills", body.folder)

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
    path = os.path.join(OCPLATFORM_DIR, f"workspace-{name}", "SOUL.md")
    if not os.path.exists(os.path.dirname(path)):
        raise HTTPException(status_code=404, detail="Agent workspace not found")
    Path(path).write_text(body.content, encoding="utf-8")
    restart_gateway()
    return {"ok": True}


class IdentityUpdate(BaseModel):
    content: str


@app.put("/api/agents/{name}/identity")
async def update_identity(name: str, body: IdentityUpdate):
    path = os.path.join(OCPLATFORM_DIR, f"workspace-{name}", "IDENTITY.md")
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


# --- Skill Matrix ---

@app.get("/api/matrix")
async def skill_matrix():
    return build_skill_matrix()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
