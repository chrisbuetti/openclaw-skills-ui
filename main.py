
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
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

app = FastAPI(title="OpenClaw Skills Manager")
templates = Jinja2Templates(directory="templates")

SKILLS_GLOB = os.path.expanduser("~/.openclaw/workspace-*/skills/*")
WORKSPACE_GLOB = os.path.expanduser("~/.openclaw/workspace-*")



import json
def get_agent_models():
    models = {}
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                data = json.load(f)
                for agent in data.get("agents", {}).get("list", []):
                    ws = agent.get("workspace", "")
                    if ws:
                        name = os.path.basename(ws).replace("workspace-", "", 1)
                        models[name] = agent.get("model", "unknown")
        except:
            pass
    return models

def scan_agents() -> list[dict]:
    """Scan all workspace directories and return agent info with SOUL.md content."""
    agents = []
    models = get_agent_models()
    for ws_dir in sorted(glob.glob(WORKSPACE_GLOB)):
        name = os.path.basename(ws_dir).replace("workspace-", "", 1)
        soul_path = os.path.join(ws_dir, "SOUL.md")
        has_soul = os.path.isfile(soul_path)
        soul_content = ""
        if has_soul:
            soul_content = Path(soul_path).read_text(encoding="utf-8")
        agents.append({
            "name": name,
            "path": soul_path,
            "has_soul": has_soul,
            "soul": soul_content,
            "model": models.get(name, "unknown model"),
        })
    return agents


def get_agent(name: str) -> dict:
    """Get a single agent by workspace name."""
    for a in scan_agents():
        if a["name"] == name:
            return a
    raise HTTPException(status_code=404, detail="Agent not found")


class SoulUpdate(BaseModel):
    soul: str


def parse_skill_md(path: str) -> dict:
    """Parse a SKILL.md file, handling both XML-tag and YAML frontmatter formats."""
    text = Path(path).read_text(encoding="utf-8")

    name = ""
    description = ""
    instructions = ""

    # Try XML-style: <skill><name>...</name><description>...</description></skill>
    xml_match = re.match(
        r"\s*<skill>\s*<name>(.*?)</name>\s*<description>(.*?)</description>\s*</skill>(.*)",
        text,
        re.DOTALL,
    )
    if xml_match:
        name = xml_match.group(1).strip()
        description = xml_match.group(2).strip()
        instructions = xml_match.group(3).strip()
    else:
        # Try YAML frontmatter: ---\nkey: value\n---
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
            # No metadata found — treat entire file as instructions
            instructions = text.strip()

    return {"name": name, "description": description, "instructions": instructions}


def serialize_skill_md(name: str, description: str, instructions: str) -> str:
    """Serialize skill data back to XML-tag SKILL.md format."""
    return (
        f"<skill>\n"
        f"  <name>{name}</name>\n"
        f"  <description>{description}</description>\n"
        f"</skill>\n\n"
        f"{instructions}\n"
    )


def scan_skills() -> list[dict]:
    """Scan all workspace skill directories."""
    skills = []
    for skill_dir in sorted(glob.glob(SKILLS_GLOB)):
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        workspace = skill_dir.split("/skills/")[0].split("workspace-")[-1]
        folder_name = os.path.basename(skill_dir)
        parsed = parse_skill_md(skill_md)
        skills.append(
            {
                "id": f"{workspace}/{folder_name}",
                "workspace": workspace,
                "folder": folder_name,
                "path": skill_md,
                **parsed,
            }
        )
    return skills


def get_skill(skill_id: str) -> dict:
    """Get a single skill by its workspace/folder id."""
    for s in scan_skills():
        if s["id"] == skill_id:
            return s
    raise HTTPException(status_code=404, detail="Skill not found")


# --- Routes ---


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/skills")
async def list_skills():
    return scan_skills()


@app.get("/api/skills/{workspace}/{folder}")
async def read_skill(workspace: str, folder: str):
    return get_skill(f"{workspace}/{folder}")


class SkillUpdate(BaseModel):
    name: str
    description: str
    instructions: str


@app.put("/api/skills/{workspace}/{folder}")
async def update_skill(workspace: str, folder: str, body: SkillUpdate):
    skill = get_skill(f"{workspace}/{folder}")
    content = serialize_skill_md(body.name, body.description, body.instructions)
    Path(skill["path"]).write_text(content, encoding="utf-8")
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
    base = os.path.expanduser(f"~/.openclaw/workspace-{body.workspace}/skills/{body.folder}")
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
    skill = get_skill(f"{workspace}/{folder}")
    skill_dir = os.path.dirname(skill["path"])
    import shutil
    shutil.rmtree(skill_dir)
    restart_gateway()
    return {"ok": True}


@app.get("/api/agents")
async def list_agents():
    return scan_agents()


@app.get("/api/agents/{name}")
async def read_agent(name: str):
    return get_agent(name)


@app.put("/api/agents/{name}")
async def update_agent(name: str, body: SoulUpdate):
    agent = get_agent(name)
    Path(agent["path"]).write_text(body.soul, encoding="utf-8")
    restart_gateway()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
