import os
import shutil
import zipfile
import tarfile
import json
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Tarkov Mod Manager", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR   = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "config.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Each profile can be seeded from environment variables so Unraid's Docker UI
# can set paths without touching any config file.
#
#   PROFILE_<ID>_LABEL  = display name   (e.g. "FIKA Server")
#   PROFILE_<ID>_PATH   = mods directory (e.g. "/mods/fika-server")
#   PROFILE_<ID>_COLOR  = hex colour     (e.g. "#e8b84b")
#
# Built-in IDs: FIKA_SERVER, FIKA_HEADLESS, SPT_CLIENT
# You can add arbitrary ones: PROFILE_MY_MOD_LABEL=... etc.

BUILTIN_PROFILES = {
    "fika-server": {
        "label": os.environ.get("PROFILE_FIKA_SERVER_LABEL",  "FIKA Server"),
        "path":  os.environ.get("PROFILE_FIKA_SERVER_PATH",   "/mods/fika-server"),
        "color": os.environ.get("PROFILE_FIKA_SERVER_COLOR",  "#e8b84b"),
    },
    "fika-headless": {
        "label": os.environ.get("PROFILE_FIKA_HEADLESS_LABEL", "FIKA Headless"),
        "path":  os.environ.get("PROFILE_FIKA_HEADLESS_PATH",  "/mods/fika-headless"),
        "color": os.environ.get("PROFILE_FIKA_HEADLESS_COLOR", "#4be8c0"),
    },
    "spt-client": {
        "label": os.environ.get("PROFILE_SPT_CLIENT_LABEL",  "SPT Client"),
        "path":  os.environ.get("PROFILE_SPT_CLIENT_PATH",   "/mods/spt-client"),
        "color": os.environ.get("PROFILE_SPT_CLIENT_COLOR",  "#e84b4b"),
    },
}

def _env_extra_profiles() -> dict:
    """Scan env for any PROFILE_<ID>_PATH vars beyond the three built-ins."""
    builtins = {"FIKA_SERVER", "FIKA_HEADLESS", "SPT_CLIENT"}
    extra = {}
    for key, val in os.environ.items():
        if key.startswith("PROFILE_") and key.endswith("_PATH"):
            raw_id = key[len("PROFILE_"):-len("_PATH")]
            if raw_id in builtins:
                continue
            pid    = raw_id.lower().replace("_", "-")
            label  = os.environ.get(f"PROFILE_{raw_id}_LABEL", pid.replace("-", " ").title())
            color  = os.environ.get(f"PROFILE_{raw_id}_COLOR", "#8b8be8")
            extra[pid] = {"label": label, "path": val, "color": color}
    return extra

def _build_default_config() -> dict:
    profiles = {**BUILTIN_PROFILES, **_env_extra_profiles()}
    return {"profiles": profiles}

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        # Merge: env vars override stored paths/labels for built-ins so
        # changing an Unraid env var is immediately reflected.
        for pid, env_profile in {**BUILTIN_PROFILES, **_env_extra_profiles()}.items():
            if pid in cfg["profiles"]:
                cfg["profiles"][pid].update(env_profile)
            else:
                cfg["profiles"][pid] = env_profile
        return cfg
    cfg = _build_default_config()
    save_config(cfg)
    return cfg

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# ── Models ────────────────────────────────────────────────────────────────────
class ModToggleRequest(BaseModel):
    profile: str
    mod_name: str
    enabled: bool

class ProfileUpdateRequest(BaseModel):
    label: str
    path: str
    color: str

class InstallUrlRequest(BaseModel):
    profile: str
    url: str

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_mod_dir(profile_id: str) -> Path:
    cfg = load_config()
    profiles = cfg.get("profiles", {})
    if profile_id not in profiles:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    p = Path(profiles[profile_id]["path"])
    p.mkdir(parents=True, exist_ok=True)
    return p

def scan_mods(mod_dir: Path) -> list[dict]:
    mods = []
    if not mod_dir.exists():
        return mods

    # Enabled mods: direct subdirs or .js/.ts files in mod_dir
    for item in sorted(mod_dir.iterdir()):
        if item.name.startswith("."):
            continue
        if item.is_dir() or item.suffix in (".js", ".ts", ".json"):
            meta = read_mod_meta(item)
            mods.append({
                "name": item.name,
                "enabled": True,
                "path": str(item),
                "size": get_size(item),
                "modified": datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
                **meta
            })

    # Disabled mods: in a __disabled__ subfolder
    disabled_dir = mod_dir / "__disabled__"
    if disabled_dir.exists():
        for item in sorted(disabled_dir.iterdir()):
            if item.name.startswith("."):
                continue
            meta = read_mod_meta(item)
            mods.append({
                "name": item.name,
                "enabled": False,
                "path": str(item),
                "size": get_size(item),
                "modified": datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
                **meta
            })

    return mods

def read_mod_meta(path: Path) -> dict:
    """Try to read package.json or mod info from the mod directory."""
    meta = {"version": None, "author": None, "description": None}
    pkg = path / "package.json" if path.is_dir() else None
    if pkg and pkg.exists():
        try:
            with open(pkg) as f:
                data = json.load(f)
            meta["version"] = data.get("version")
            meta["author"] = data.get("author")
            meta["description"] = data.get("description")
        except Exception:
            pass
    return meta

def get_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except Exception:
        pass
    return total

def extract_archive(archive_path: Path, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, "r") as z:
            z.extractall(dest)
    elif tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path, "r:*") as t:
            t.extractall(dest)
    else:
        # Just move it as-is
        shutil.move(str(archive_path), str(dest / archive_path.name))

# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    return load_config()

@app.post("/api/config/profile/{profile_id}")
def update_profile(profile_id: str, req: ProfileUpdateRequest):
    cfg = load_config()
    if profile_id not in cfg["profiles"]:
        cfg["profiles"][profile_id] = {}
    cfg["profiles"][profile_id].update({"label": req.label, "path": req.path, "color": req.color})
    save_config(cfg)
    return {"ok": True}

@app.delete("/api/config/profile/{profile_id}")
def delete_profile(profile_id: str):
    cfg = load_config()
    if profile_id in cfg["profiles"]:
        del cfg["profiles"][profile_id]
        save_config(cfg)
    return {"ok": True}

@app.post("/api/config/profile")
def create_profile(req: ProfileUpdateRequest, profile_id: Optional[str] = None):
    cfg = load_config()
    pid = profile_id or req.label.lower().replace(" ", "-")
    cfg["profiles"][pid] = {"label": req.label, "path": req.path, "color": req.color}
    save_config(cfg)
    return {"ok": True, "id": pid}

@app.get("/api/mods/{profile}")
def list_mods(profile: str):
    mod_dir = get_mod_dir(profile)
    return {"profile": profile, "mods": scan_mods(mod_dir)}

@app.post("/api/mods/toggle")
def toggle_mod(req: ModToggleRequest):
    mod_dir = get_mod_dir(req.profile)
    disabled_dir = mod_dir / "__disabled__"
    disabled_dir.mkdir(exist_ok=True)

    enabled_path = mod_dir / req.mod_name
    disabled_path = disabled_dir / req.mod_name

    if req.enabled:
        if not disabled_path.exists():
            raise HTTPException(status_code=404, detail="Mod not found in disabled folder")
        shutil.move(str(disabled_path), str(enabled_path))
    else:
        if not enabled_path.exists():
            raise HTTPException(status_code=404, detail="Mod not found in enabled folder")
        shutil.move(str(enabled_path), str(disabled_path))

    return {"ok": True, "mod": req.mod_name, "enabled": req.enabled}

@app.delete("/api/mods/{profile}/{mod_name}")
def delete_mod(profile: str, mod_name: str):
    mod_dir = get_mod_dir(profile)
    for candidate in [mod_dir / mod_name, mod_dir / "__disabled__" / mod_name]:
        if candidate.exists():
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
            return {"ok": True}
    raise HTTPException(status_code=404, detail="Mod not found")

@app.post("/api/mods/{profile}/upload")
async def upload_mod(profile: str, file: UploadFile = File(...)):
    mod_dir = get_mod_dir(profile)
    tmp = mod_dir / f"__tmp_{file.filename}"
    try:
        with open(tmp, "wb") as f:
            content = await file.read()
            f.write(content)

        suffix = Path(file.filename).suffix.lower()
        if suffix in (".zip", ".tar", ".gz", ".tgz"):
            extract_archive(tmp, mod_dir)
            tmp.unlink(missing_ok=True)
            return {"ok": True, "message": f"Extracted {file.filename}"}
        else:
            dest = mod_dir / file.filename
            shutil.move(str(tmp), str(dest))
            return {"ok": True, "message": f"Installed {file.filename}"}
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/mods/install-url")
async def install_from_url(req: InstallUrlRequest):
    import urllib.request
    mod_dir = get_mod_dir(req.profile)
    filename = req.url.split("/")[-1].split("?")[0] or "mod_download"
    tmp = mod_dir / f"__tmp_{filename}"
    try:
        urllib.request.urlretrieve(req.url, tmp)
        suffix = Path(filename).suffix.lower()
        if suffix in (".zip", ".tar", ".gz", ".tgz"):
            extract_archive(tmp, mod_dir)
            tmp.unlink(missing_ok=True)
        else:
            dest = mod_dir / filename
            shutil.move(str(tmp), str(dest))
        return {"ok": True, "message": f"Installed from URL: {filename}"}
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0.0", "data_dir": str(DATA_DIR)}

# ── Static / Frontend ─────────────────────────────────────────────────────────
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/", response_class=HTMLResponse)
def root():
    index = static_dir / "index.html"
    if index.exists():
        return index.read_text()
    return HTMLResponse("<h1>Tarkov Mod Manager</h1><p>Frontend not found.</p>")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7272, reload=False)
