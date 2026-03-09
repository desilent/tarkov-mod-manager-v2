import os
import shutil
import zipfile
import tarfile
import json
import logging
import socket
import asyncio
import secrets
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, Response, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Tarkov Mod Manager", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_executor = ThreadPoolExecutor(max_workers=2)

# ── Auth ──────────────────────────────────────────────────────────────────────
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "").strip()
AUTH_SESSION_HOURS = int(os.environ.get("AUTH_SESSION_HOURS", "72"))
AUTH_ENABLED = bool(AUTH_PASSWORD)
_sessions: dict[str, datetime] = {}

def _create_session() -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expiry = datetime.now() + timedelta(hours=AUTH_SESSION_HOURS)
    _sessions[token] = expiry
    now = datetime.now()
    for t in [t for t, exp in _sessions.items() if exp < now]:
        del _sessions[t]
    return token, expiry

def _validate_session(token: str | None) -> bool:
    if not token: return False
    expiry = _sessions.get(token)
    if not expiry: return False
    if datetime.now() > expiry:
        del _sessions[token]
        return False
    return True

def _check_password(password: str) -> bool:
    return secrets.compare_digest(password, AUTH_PASSWORD)

_AUTH_EXEMPT = {"/api/auth/check", "/api/auth/login", "/api/health"}

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not AUTH_ENABLED:
            return await call_next(request)
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        if path in _AUTH_EXEMPT:
            return await call_next(request)
        token = request.cookies.get("rmm_session")
        if not _validate_session(token):
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        return await call_next(request)

app.add_middleware(AuthMiddleware)

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "config.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Profile env vars now support dual paths:
#   PROFILE_<ID>_MODS_PATH     = user/mods directory
#   PROFILE_<ID>_PLUGINS_PATH  = BepInEx/plugins directory
#   PROFILE_<ID>_PATH          = legacy single path (migrated to mods_path)

BUILTIN_PROFILES = {
    "fika-server": {
        "label": os.environ.get("PROFILE_FIKA_SERVER_LABEL", "FIKA Server"),
        "mods_path": os.environ.get("PROFILE_FIKA_SERVER_MODS_PATH",
                     os.environ.get("PROFILE_FIKA_SERVER_PATH", "/mods/fika-server")),
        "plugins_path": os.environ.get("PROFILE_FIKA_SERVER_PLUGINS_PATH", "/plugins/fika-server"),
        "color": os.environ.get("PROFILE_FIKA_SERVER_COLOR", "#e8b84b"),
    },
    "fika-headless": {
        "label": os.environ.get("PROFILE_FIKA_HEADLESS_LABEL", "FIKA Headless"),
        "mods_path": os.environ.get("PROFILE_FIKA_HEADLESS_MODS_PATH",
                     os.environ.get("PROFILE_FIKA_HEADLESS_PATH", "/mods/fika-headless")),
        "plugins_path": os.environ.get("PROFILE_FIKA_HEADLESS_PLUGINS_PATH", "/plugins/fika-headless"),
        "color": os.environ.get("PROFILE_FIKA_HEADLESS_COLOR", "#4be8c0"),
    },
}

def _env_extra_profiles() -> dict:
    builtins = {"FIKA_SERVER", "FIKA_HEADLESS"}
    extra = {}
    for key, val in os.environ.items():
        if key.startswith("PROFILE_") and (key.endswith("_PATH") or key.endswith("_MODS_PATH")):
            if key.endswith("_MODS_PATH"):
                raw_id = key[len("PROFILE_"):-len("_MODS_PATH")]
            else:
                raw_id = key[len("PROFILE_"):-len("_PATH")]
            if raw_id in builtins:
                continue
            pid = raw_id.lower().replace("_", "-")
            if pid in extra:
                continue
            label = os.environ.get(f"PROFILE_{raw_id}_LABEL", pid.replace("-", " ").title())
            color = os.environ.get(f"PROFILE_{raw_id}_COLOR", "#8b8be8")
            mods_path = os.environ.get(f"PROFILE_{raw_id}_MODS_PATH",
                        os.environ.get(f"PROFILE_{raw_id}_PATH", ""))
            plugins_path = os.environ.get(f"PROFILE_{raw_id}_PLUGINS_PATH", "")
            extra[pid] = {"label": label, "mods_path": mods_path, "plugins_path": plugins_path, "color": color}
    return extra

def _migrate_profile(p: dict) -> dict:
    """Migrate old single-path profile to dual-path."""
    if "path" in p and "mods_path" not in p:
        p["mods_path"] = p.pop("path")
    if "plugins_path" not in p:
        p["plugins_path"] = ""
    return p

def _build_default_config() -> dict:
    profiles = {**BUILTIN_PROFILES, **_env_extra_profiles()}
    return {"profiles": profiles}

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        # Migrate old profiles
        for pid in cfg.get("profiles", {}):
            cfg["profiles"][pid] = _migrate_profile(cfg["profiles"][pid])
        # Merge env vars
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

# ── Docker Socket ─────────────────────────────────────────────────────────────
def _build_container_map() -> dict:
    cmap = {
        "fika-server": os.environ.get("PROFILE_FIKA_SERVER_CONTAINER", ""),
        "fika-headless": os.environ.get("PROFILE_FIKA_HEADLESS_CONTAINER", ""),
    }
    builtins = {"FIKA_SERVER", "FIKA_HEADLESS"}
    for key, val in os.environ.items():
        if key.startswith("PROFILE_") and key.endswith("_CONTAINER"):
            raw_id = key[len("PROFILE_"):-len("_CONTAINER")]
            if raw_id in builtins: continue
            cmap[raw_id.lower().replace("_", "-")] = val
    return cmap

CONTAINER_MAP = _build_container_map()
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")

def _docker_request(method: str, path: str, body: dict | None = None) -> dict:
    sock_path = DOCKER_SOCKET
    if not os.path.exists(sock_path):
        raise HTTPException(status_code=503, detail="Docker socket not available.")
    payload = json.dumps(body) if body else ""
    req = (f"{method} {path} HTTP/1.0\r\nHost: localhost\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n{payload}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock_path)
        s.sendall(req.encode())
        response = b""
        while True:
            chunk = s.recv(4096)
            if not chunk: break
            response += chunk
    raw = response.decode(errors="replace")
    body_raw = raw.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in raw else raw
    try:
        return json.loads(body_raw)
    except Exception:
        lines = body_raw.strip().splitlines()
        if lines:
            try: return json.loads("\n".join(lines[1:]))
            except Exception: pass
        return {}

def _docker_request_raw(method: str, path: str, max_bytes: int = 512_000) -> bytes:
    sock_path = DOCKER_SOCKET
    if not os.path.exists(sock_path):
        raise HTTPException(status_code=503, detail="Docker socket not available.")
    req = f"{method} {path} HTTP/1.0\r\nHost: localhost\r\n\r\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(sock_path)
        s.sendall(req.encode())
        response = b""
        try:
            while len(response) < max_bytes:
                chunk = s.recv(8192)
                if not chunk: break
                response += chunk
        except socket.timeout: pass
    sep = b"\r\n\r\n"
    return response.split(sep, 1)[1] if sep in response else response

def _strip_docker_log_headers(raw: bytes) -> str:
    lines = []
    pos = 0
    while pos + 8 <= len(raw):
        size = int.from_bytes(raw[pos+4:pos+8], 'big')
        pos += 8
        payload = raw[pos:pos+size] if pos + size <= len(raw) else raw[pos:]
        pos += size
        try: lines.append(payload.decode('utf-8', errors='replace').rstrip('\n'))
        except Exception: pass
    if not lines and raw:
        return raw.decode('utf-8', errors='replace')
    return '\n'.join(lines)

def get_container_status(container_name: str) -> dict:
    if not container_name:
        return {"name": None, "state": "unknown", "status": "not configured", "available": False}
    try:
        data = _docker_request("GET", f"/containers/{container_name}/json")
        if isinstance(data, dict) and "State" in data:
            st = data["State"]
            return {"name": container_name, "state": "running" if st.get("Running") else "stopped",
                    "status": st.get("Status", "unknown"), "available": True}
        return {"name": container_name, "state": "unknown", "status": "not found", "available": False}
    except HTTPException: raise
    except Exception as e:
        return {"name": container_name, "state": "unknown", "status": str(e), "available": False}

def is_container_running(profile_id: str) -> bool:
    container = CONTAINER_MAP.get(profile_id, "")
    if not container: return False
    return get_container_status(container).get("state") == "running"

# ── Models ────────────────────────────────────────────────────────────────────
class ModToggleRequest(BaseModel):
    profile: str
    mod_name: str
    enabled: bool
    folder: str = "mods"  # "mods" or "plugins"

class ProfileUpdateRequest(BaseModel):
    label: str
    mods_path: str
    plugins_path: str = ""
    color: str

class InstallUrlRequest(BaseModel):
    url: str

class PresetSaveRequest(BaseModel):
    label: str

class AuthLoginRequest(BaseModel):
    password: str

class ConfigFileWriteRequest(BaseModel):
    path: str
    content: str

# ── Helpers ───────────────────────────────────────────────────────────────────
def _get_profile(profile_id: str) -> dict:
    cfg = load_config()
    profiles = cfg.get("profiles", {})
    if profile_id not in profiles:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")
    return profiles[profile_id]

def get_mod_dir(profile_id: str, folder: str = "mods") -> Path:
    """Get the mods_path or plugins_path for a profile."""
    p = _get_profile(profile_id)
    key = "mods_path" if folder == "mods" else "plugins_path"
    path_str = p.get(key, "")
    if not path_str:
        raise HTTPException(status_code=400, detail=f"No {folder} path configured for '{profile_id}'")
    path = Path(path_str)
    path.mkdir(parents=True, exist_ok=True)
    return path

def scan_mods_in_dir(mod_dir: Path, folder_type: str) -> list[dict]:
    """Scan a single directory for mods (enabled + disabled)."""
    mods = []
    if not mod_dir.exists():
        return mods
    for item in sorted(mod_dir.iterdir()):
        if item.name.startswith(".") or (item.name.startswith("__") and item.name.endswith("__")):
            continue
        if item.is_dir() or item.suffix in (".js", ".ts", ".json", ".dll"):
            meta = read_mod_meta(item)
            mods.append({
                "name": item.name, "enabled": True, "folder": folder_type,
                "path": str(item), "size": get_size(item),
                "modified": datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
                **meta
            })
    disabled_dir = mod_dir / "__disabled__"
    if disabled_dir.exists():
        for item in sorted(disabled_dir.iterdir()):
            if item.name.startswith(".") or (item.name.startswith("__") and item.name.endswith("__")):
                continue
            meta = read_mod_meta(item)
            mods.append({
                "name": item.name, "enabled": False, "folder": folder_type,
                "path": str(item), "size": get_size(item),
                "modified": datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
                **meta
            })
    return mods

def scan_all_mods(profile_id: str) -> list[dict]:
    """Scan both mods_path and plugins_path for a profile."""
    p = _get_profile(profile_id)
    all_mods = []
    if p.get("mods_path"):
        d = Path(p["mods_path"])
        d.mkdir(parents=True, exist_ok=True)
        all_mods.extend(scan_mods_in_dir(d, "mods"))
    if p.get("plugins_path"):
        d = Path(p["plugins_path"])
        d.mkdir(parents=True, exist_ok=True)
        all_mods.extend(scan_mods_in_dir(d, "plugins"))
    return all_mods

def read_mod_meta(path: Path) -> dict:
    meta = {"version": None, "author": None, "description": None}
    if not path.is_dir():
        # Single file (e.g. a .dll in plugins folder) — try DLL version extraction
        if path.suffix == ".dll":
            return _read_dll_version(path)
        return meta

    # Strategy 1: package.json in root (standard SPT server mod)
    pkg = path / "package.json"
    if pkg.exists():
        meta = _parse_package_json(pkg, meta)
        if meta["version"]:
            return meta

    # Strategy 2: package.json in src/<modname>/ or src/ (common nested pattern)
    src_dir = path / "src"
    if src_dir.exists() and src_dir.is_dir():
        for child in src_dir.iterdir():
            if child.is_dir():
                nested_pkg = child / "package.json"
                if nested_pkg.exists():
                    meta = _parse_package_json(nested_pkg, meta)
                    if meta["version"]:
                        return meta
        nested_pkg = src_dir / "package.json"
        if nested_pkg.exists():
            meta = _parse_package_json(nested_pkg, meta)
            if meta["version"]:
                return meta

    # Strategy 3: Find any package.json up to 3 levels deep
    try:
        for pkg_file in path.rglob("package.json"):
            if "node_modules" in str(pkg_file):
                continue
            rel = pkg_file.relative_to(path)
            if len(rel.parts) <= 3:
                meta = _parse_package_json(pkg_file, meta)
                if meta["version"]:
                    return meta
    except Exception:
        pass

    # Strategy 4: JSON config files (with and without extension)
    for config_name in ["config.json", "mod.json", "manifest.json", "config", "info", "meta"]:
        cfg = path / config_name
        if cfg.exists() and cfg.is_file():
            meta = _try_parse_json_meta(cfg, meta)
            if meta["version"]:
                return meta

    # Strategy 5: Any .json file in root that might contain version info
    try:
        for json_file in sorted(path.iterdir()):
            if json_file.is_file() and json_file.suffix == ".json" and json_file.name != "blacklists":
                meta = _try_parse_json_meta(json_file, meta)
                if meta["version"]:
                    return meta
    except Exception:
        pass

    # Strategy 6: Extract version from .dll assembly metadata (BepInEx plugins)
    try:
        for dll_file in sorted(path.iterdir()):
            if dll_file.is_file() and dll_file.suffix == ".dll":
                dll_meta = _read_dll_version(dll_file)
                if dll_meta.get("version"):
                    meta["version"] = meta["version"] or dll_meta["version"]
                    meta["author"] = meta["author"] or dll_meta.get("author")
                    meta["description"] = meta["description"] or dll_meta.get("description")
                    return meta
    except Exception:
        pass

    return meta

def _parse_package_json(pkg_path: Path, meta: dict) -> dict:
    """Parse a package.json and extract version/author/description."""
    try:
        with open(pkg_path) as f:
            data = json.load(f)
        meta["version"] = meta["version"] or data.get("version")
        meta["author"] = meta["author"] or data.get("author")
        meta["description"] = meta["description"] or data.get("description")
        if not meta["version"] and "akiVersion" in data:
            meta["version"] = data.get("akiVersion")
        if not meta["version"] and "sptVersion" in data:
            meta["version"] = data.get("sptVersion")
        if isinstance(meta["author"], dict):
            meta["author"] = meta["author"].get("name", str(meta["author"]))
    except Exception:
        pass
    return meta

def _try_parse_json_meta(file_path: Path, meta: dict) -> dict:
    """Try to parse a file as JSON and extract version/author/description."""
    try:
        raw = file_path.read_text(encoding="utf-8", errors="replace").strip()
        if not raw or raw[0] not in ('{', '['):
            return meta
        data = json.loads(raw)
        if isinstance(data, dict):
            # Check common version field names
            for vkey in ["version", "modVersion", "Version", "mod_version", "pluginVersion"]:
                if data.get(vkey) and not meta["version"]:
                    meta["version"] = str(data[vkey])
                    break
            for akey in ["author", "authorName", "Author", "authors"]:
                if data.get(akey) and not meta["author"]:
                    val = data[akey]
                    meta["author"] = val if isinstance(val, str) else (val[0] if isinstance(val, list) and val else str(val))
                    break
            for dkey in ["description", "Description", "desc"]:
                if data.get(dkey) and not meta["description"]:
                    meta["description"] = str(data[dkey])[:200]
                    break
    except Exception:
        pass
    return meta

def _read_dll_version(dll_path: Path) -> dict:
    """Extract version info from a .NET/BepInEx plugin DLL."""
    result = {"version": None, "author": None, "description": None}
    try:
        data = dll_path.read_bytes()
        import re

        # Decode as UTF-8 for string scanning
        text = data.decode("utf-8", errors="replace")

        # Strategy A: Find BepInPlugin attribute strings
        # BepInEx plugins contain their GUID, name, and version as consecutive strings
        # Pattern: "com.author.modname" followed nearby by a semver like "1.2.3"
        # Look for BepInPlugin-style GUIDs (com.something.something)
        guid_matches = re.finditer(r'(com\.[a-zA-Z0-9_]+\.[a-zA-Z0-9_.]+)', text)
        for gm in guid_matches:
            # Search in the ~200 bytes after the GUID for a version string
            search_start = gm.end()
            search_region = text[search_start:search_start + 200]
            ver_match = re.search(r'(\d+\.\d+\.\d+(?:\.\d+)?)', search_region)
            if ver_match:
                v = ver_match.group(1)
                # Filter out CLR/framework versions
                if v in ("4.0.30319", "2.0.50727", "0.0.0.0", "0.0.0", "1.0.0.0"):
                    continue
                result["version"] = v
                # Try to extract author from GUID (com.AUTHOR.modname)
                guid_parts = gm.group(1).split(".")
                if len(guid_parts) >= 2 and not result["author"]:
                    result["author"] = guid_parts[1]
                return result

        # Strategy B: Look for AssemblyFileVersion or AssemblyInformationalVersion
        # These are stored as UTF-16LE strings in .NET assemblies
        text_u16 = data.decode("utf-16-le", errors="replace")
        for attr_text in [text_u16, text]:
            # AssemblyFileVersion is more reliable than AssemblyVersion
            for pattern in [
                r'AssemblyFileVersion[^\d]{0,20}(\d+\.\d+\.\d+(?:\.\d+)?)',
                r'AssemblyInformationalVersion[^\d]{0,20}(\d+\.\d+\.\d+[^\x00\ufffd]*)',
                r'FileVersion[^\d]{0,20}(\d+\.\d+\.\d+(?:\.\d+)?)',
            ]:
                m = re.search(pattern, attr_text)
                if m:
                    v = m.group(1).strip().split('\x00')[0].strip()
                    if v and v not in ("4.0.30319", "2.0.50727", "0.0.0.0", "0.0.0", "1.0.0.0"):
                        result["version"] = v
                        return result

        # Strategy C: Scan raw bytes for the PE version resource
        # The VS_FIXEDFILEINFO structure contains file version as 4 uint16s
        # Signature: 0xFEEF04BD
        sig = b'\xbd\x04\xef\xfe'
        pos = data.find(sig)
        if pos >= 0 and pos + 52 <= len(data):
            import struct
            # File version is at offset +8 from signature: MS(hi,lo), LS(hi,lo)
            ms = struct.unpack_from('<I', data, pos + 8)[0]
            ls = struct.unpack_from('<I', data, pos + 12)[0]
            major = (ms >> 16) & 0xFFFF
            minor = ms & 0xFFFF
            build = (ls >> 16) & 0xFFFF
            patch = ls & 0xFFFF
            v = f"{major}.{minor}.{build}"
            if patch > 0:
                v += f".{patch}"
            if v not in ("0.0.0", "0.0.0.0", "4.0.30319"):
                result["version"] = v

    except Exception:
        pass
    return result

def get_size(path: Path) -> int:
    if path.is_file(): return path.stat().st_size
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file(): total += f.stat().st_size
    except Exception: pass
    return total

ARCHIVE_SUFFIXES = {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".tar.gz", ".tar.bz2", ".tar.xz"}

def _is_archive(filename: str) -> bool:
    """Check if filename looks like an archive."""
    name = filename.lower()
    return any(name.endswith(s) for s in ARCHIVE_SUFFIXES)

def extract_archive_to_staging(archive_path: Path) -> Path:
    """Extract archive to a temp staging dir and return the staging path.
    Supports zip, tar.*, 7z, and RAR via p7zip-full."""
    staging = archive_path.parent / "__staging__"
    staging.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, "r") as z:
            z.extractall(staging)
    elif tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path, "r:*") as t:
            t.extractall(staging)
    else:
        # Fallback to 7z command — handles .7z, .rar, and anything else
        import subprocess
        result = subprocess.run(
            ["7z", "x", str(archive_path), f"-o{staging}", "-y", "-bso0", "-bsp0"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            # Not an archive at all — just move the file
            logger.warning(f"7z extraction failed: {result.stderr[:200]}")
            dest = staging / archive_path.name
            shutil.move(str(archive_path), str(dest))
    return staging

def detect_spt_structure(staging: Path) -> dict:
    """Scan extracted content for SPT mod structure.
    Returns {"mods": [paths], "plugins": [paths], "unknown": [paths]}
    Detects: user/mods/*, BepInEx/plugins/*, or flat content."""
    result = {"mods": [], "plugins": [], "unknown": []}

    # Walk the staging to find known SPT paths
    def find_spt_dirs(root: Path):
        """Recursively look for user/mods and BepInEx/plugins directories."""
        mods_dir = None
        plugins_dir = None
        for p in root.rglob("*"):
            rel = str(p.relative_to(root)).replace("\\", "/")
            # Match user/mods/<something> or just mods/<something> at top level
            if "/user/mods" in rel or rel.startswith("user/mods"):
                candidate = p
                while candidate.parent.name != "mods":
                    candidate = candidate.parent
                mods_dir = candidate.parent  # the "mods" directory
            if "/BepInEx/plugins" in rel or rel.startswith("BepInEx/plugins"):
                candidate = p
                while candidate.parent.name != "plugins":
                    candidate = candidate.parent
                plugins_dir = candidate.parent  # the "plugins" directory
        return mods_dir, plugins_dir

    # Clean __MACOSX
    macosx = staging / "__MACOSX"
    if macosx.exists():
        shutil.rmtree(macosx)

    # Check for structured SPT mod (user/mods and/or BepInEx/plugins)
    top_items = [i for i in staging.iterdir() if not i.name.startswith(".")]

    # Unwrap single top-level folder
    if len(top_items) == 1 and top_items[0].is_dir():
        inner = top_items[0]
    else:
        inner = staging

    # Look for user/mods/*
    for candidate in [inner / "user" / "mods", inner / "mods"]:
        if candidate.exists() and candidate.is_dir():
            for item in candidate.iterdir():
                if not item.name.startswith("."):
                    result["mods"].append(item)

    # Look for BepInEx/plugins/*
    for candidate in [inner / "BepInEx" / "plugins", inner / "plugins"]:
        if candidate.exists() and candidate.is_dir():
            for item in candidate.iterdir():
                if not item.name.startswith("."):
                    result["plugins"].append(item)

    # If nothing detected, treat all top-level items as unknown
    if not result["mods"] and not result["plugins"]:
        items = [i for i in inner.iterdir() if not i.name.startswith(".")
                 and not (i.name.startswith("__") and i.name.endswith("__"))]
        # Heuristic: if items have package.json → server mod; if .dll → plugin
        for item in items:
            if item.is_dir() and (item / "package.json").exists():
                result["mods"].append(item)
            elif item.suffix == ".dll" or (item.is_dir() and any(item.rglob("*.dll"))):
                result["plugins"].append(item)
            else:
                result["unknown"].append(item)

    return result

def install_detected_mods(detected: dict, profile_id: str, config: dict) -> dict:
    """Install detected mods to the specified profile's matching paths.
    Returns summary of what was installed where."""
    summary = {"installations": [], "skipped": []}
    profiles = config.get("profiles", {})
    profile = profiles.get(profile_id)
    if not profile:
        return summary

    for folder_type in ["mods", "plugins"]:
        items = detected.get(folder_type, [])
        if not items:
            continue
        path_key = "mods_path" if folder_type == "mods" else "plugins_path"
        dest_str = profile.get(path_key, "")
        if not dest_str:
            # No path configured for this folder type — skip
            for item in items:
                summary["skipped"].append(item.name)
            continue
        dest = Path(dest_str)
        dest.mkdir(parents=True, exist_ok=True)

        for item in items:
            target = dest / item.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.copytree(str(item), str(target)) if item.is_dir() else shutil.copy2(str(item), str(target))
            summary["installations"].append({
                "mod": item.name, "profile": profile_id,
                "folder": folder_type, "label": profile.get("label", profile_id)
            })

    # Unknown items: skip
    for item in detected.get("unknown", []):
        summary["skipped"].append(item.name)

    return summary

def _sync_download(url: str, dest: Path):
    import urllib.request
    urllib.request.urlretrieve(url, dest)

# ── Presets ───────────────────────────────────────────────────────────────────
PRESETS_FILE = DATA_DIR / "presets.json"

def load_presets() -> dict:
    if PRESETS_FILE.exists():
        try:
            with open(PRESETS_FILE) as f: return json.load(f)
        except Exception: pass
    return {}

def save_presets(data: dict):
    with open(PRESETS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def apply_preset(profile_id: str, preset: dict):
    """Apply a preset (strict mode) to both mods and plugins folders."""
    p = _get_profile(profile_id)
    mod_states = preset.get("mods", {})
    moved = {"enabled": [], "disabled": []}

    for folder_type in ["mods", "plugins"]:
        path_key = f"{folder_type}_path"
        path_str = p.get(path_key, "")
        if not path_str: continue
        mod_dir = Path(path_str)
        if not mod_dir.exists(): continue
        disabled_dir = mod_dir / "__disabled__"
        disabled_dir.mkdir(exist_ok=True)

        all_mods = {}
        for item in sorted(mod_dir.iterdir()):
            if item.name.startswith(".") or (item.name.startswith("__") and item.name.endswith("__")): continue
            if item.is_dir() or item.suffix in (".js", ".ts", ".json", ".dll"):
                all_mods[item.name] = ("enabled", item)
        if disabled_dir.exists():
            for item in sorted(disabled_dir.iterdir()):
                if item.name.startswith(".") or (item.name.startswith("__") and item.name.endswith("__")): continue
                all_mods[item.name] = ("disabled", item)

        for mod_name, (current_state, path) in all_mods.items():
            should_enable = mod_states.get(mod_name, False)
            if should_enable and current_state == "disabled":
                shutil.move(str(path), str(mod_dir / mod_name))
                moved["enabled"].append(mod_name)
            elif not should_enable and current_state == "enabled":
                shutil.move(str(path), str(disabled_dir / mod_name))
                moved["disabled"].append(mod_name)

    return moved

# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    return load_config()

@app.post("/api/config/profile/{profile_id}")
def update_profile(profile_id: str, req: ProfileUpdateRequest):
    cfg = load_config()
    if profile_id not in cfg["profiles"]:
        cfg["profiles"][profile_id] = {}
    cfg["profiles"][profile_id].update({
        "label": req.label, "mods_path": req.mods_path,
        "plugins_path": req.plugins_path, "color": req.color
    })
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
    cfg["profiles"][pid] = {
        "label": req.label, "mods_path": req.mods_path,
        "plugins_path": req.plugins_path, "color": req.color
    }
    save_config(cfg)
    return {"ok": True, "id": pid}

@app.get("/api/mods/{profile}")
def list_mods(profile: str):
    return {"profile": profile, "mods": scan_all_mods(profile)}

@app.post("/api/mods/toggle")
def toggle_mod(req: ModToggleRequest):
    if is_container_running(req.profile):
        raise HTTPException(status_code=409, detail="Container is running — stop it before changing mods")
    mod_dir = get_mod_dir(req.profile, req.folder)
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

@app.delete("/api/mods/{profile}/{folder}/{mod_name}")
def delete_mod(profile: str, folder: str, mod_name: str):
    if is_container_running(profile):
        raise HTTPException(status_code=409, detail="Container is running — stop it before deleting mods")
    mod_dir = get_mod_dir(profile, folder)
    for candidate in [mod_dir / mod_name, mod_dir / "__disabled__" / mod_name]:
        if candidate.exists():
            shutil.rmtree(candidate) if candidate.is_dir() else candidate.unlink()
            return {"ok": True}
    raise HTTPException(status_code=404, detail="Mod not found")

@app.post("/api/mods/smart-install/{profile}/upload")
async def smart_install_upload(profile: str, file: UploadFile = File(...)):
    """Smart install: extract, detect structure, install to the selected profile."""
    cfg = load_config()
    if profile not in cfg.get("profiles", {}):
        raise HTTPException(status_code=404, detail=f"Profile '{profile}' not found")
    if is_container_running(profile):
        raise HTTPException(status_code=409,
            detail=f"Container for {cfg['profiles'][profile].get('label', profile)} is running — stop it first")

    tmp_dir = DATA_DIR / "__tmp_install__"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = tmp_dir / file.filename
    try:
        with open(tmp_file, "wb") as f:
            content = await file.read()
            f.write(content)

        suffix = Path(file.filename).suffix.lower()
        if _is_archive(file.filename):
            staging = extract_archive_to_staging(tmp_file)
            tmp_file.unlink(missing_ok=True)
        else:
            # Single file — put in staging
            staging = tmp_dir / "__staging__"
            staging.mkdir(exist_ok=True)
            shutil.move(str(tmp_file), str(staging / file.filename))

        detected = detect_spt_structure(staging)
        summary = install_detected_mods(detected, profile, cfg)
        return {"ok": True, "filename": file.filename, "detected": {
            "mods": [p.name for p in detected["mods"]],
            "plugins": [p.name for p in detected["plugins"]],
            "unknown": [p.name for p in detected["unknown"]],
        }, "summary": summary}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

@app.post("/api/mods/smart-install/{profile}/url")
async def smart_install_url(profile: str, req: InstallUrlRequest):
    """Smart install from URL to the selected profile."""
    cfg = load_config()
    if profile not in cfg.get("profiles", {}):
        raise HTTPException(status_code=404, detail=f"Profile '{profile}' not found")
    if is_container_running(profile):
        raise HTTPException(status_code=409,
            detail=f"Container for {cfg['profiles'][profile].get('label', profile)} is running — stop it first")

    tmp_dir = DATA_DIR / "__tmp_install__"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    filename = req.url.split("/")[-1].split("?")[0] or "mod_download"
    tmp_file = tmp_dir / filename
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_executor, _sync_download, req.url, tmp_file)

        suffix = Path(filename).suffix.lower()
        if _is_archive(filename):
            staging = extract_archive_to_staging(tmp_file)
            tmp_file.unlink(missing_ok=True)
        else:
            staging = tmp_dir / "__staging__"
            staging.mkdir(exist_ok=True)
            shutil.move(str(tmp_file), str(staging / filename))

        detected = detect_spt_structure(staging)
        summary = install_detected_mods(detected, profile, cfg)
        return {"ok": True, "filename": filename, "detected": {
            "mods": [p.name for p in detected["mods"]],
            "plugins": [p.name for p in detected["plugins"]],
            "unknown": [p.name for p in detected["unknown"]],
        }, "summary": summary}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

# Keep legacy endpoints as aliases
@app.post("/api/mods/{profile}/upload")
async def upload_mod_legacy(profile: str, file: UploadFile = File(...)):
    """Legacy single-profile upload — redirects to smart install."""
    return await smart_install_upload(profile, file)

@app.post("/api/mods/{profile}/install-url")
async def install_url_legacy(profile: str, req: InstallUrlRequest):
    return await smart_install_url(profile, req)

# ── Containers ────────────────────────────────────────────────────────────────

@app.get("/api/containers")
def list_container_statuses():
    result = {}
    for profile_id, container_name in CONTAINER_MAP.items():
        result[profile_id] = get_container_status(container_name)
    return result

@app.get("/api/containers/{profile}")
def container_status(profile: str):
    return get_container_status(CONTAINER_MAP.get(profile, ""))

@app.post("/api/containers/{profile}/stop")
def stop_container(profile: str):
    container = CONTAINER_MAP.get(profile, "")
    if not container:
        raise HTTPException(status_code=400, detail="No container configured")
    try:
        _docker_request("POST", f"/containers/{container}/stop")
        return {"ok": True, "action": "stop", "container": container}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/containers/{profile}/start")
def start_container(profile: str):
    container = CONTAINER_MAP.get(profile, "")
    if not container:
        raise HTTPException(status_code=400, detail="No container configured")
    try:
        _docker_request("POST", f"/containers/{container}/start")
        return {"ok": True, "action": "start", "container": container}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/containers/{profile}/logs")
def container_logs(profile: str, lines: int = 150, since: int = 0):
    container = CONTAINER_MAP.get(profile, "")
    if not container:
        raise HTTPException(status_code=400, detail="No container configured")
    try:
        params = f"stdout=1&stderr=1&tail={lines}&timestamps=1"
        if since > 0: params += f"&since={since}"
        raw = _docker_request_raw("GET", f"/containers/{container}/logs?{params}")
        text = _strip_docker_log_headers(raw)
        return {"ok": True, "logs": text, "container": container}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Presets ────────────────────────────────────────────────────────────────────

@app.get("/api/presets/{profile}")
def list_presets(profile: str):
    return {"profile": profile, "presets": load_presets().get(profile, {})}

@app.post("/api/presets/{profile}")
def save_preset(profile: str, req: PresetSaveRequest):
    if is_container_running(profile):
        raise HTTPException(status_code=409, detail="Container is running — stop it first")
    mods = scan_all_mods(profile)
    mod_states = {m["name"]: m["enabled"] for m in mods}
    preset_id = req.label.lower().replace(" ", "-")
    presets = load_presets()
    if profile not in presets: presets[profile] = {}
    presets[profile][preset_id] = {
        "label": req.label, "mods": mod_states,
        "mod_count": len(mod_states),
        "enabled_count": sum(1 for v in mod_states.values() if v),
        "created": datetime.now().isoformat(),
        "updated": datetime.now().isoformat(),
    }
    save_presets(presets)
    return {"ok": True, "id": preset_id, "preset": presets[profile][preset_id]}

@app.put("/api/presets/{profile}/{preset_id}")
def update_preset(profile: str, preset_id: str):
    if is_container_running(profile):
        raise HTTPException(status_code=409, detail="Container is running — stop it first")
    presets = load_presets()
    if profile not in presets or preset_id not in presets[profile]:
        raise HTTPException(status_code=404, detail="Preset not found")
    mods = scan_all_mods(profile)
    mod_states = {m["name"]: m["enabled"] for m in mods}
    presets[profile][preset_id].update({
        "mods": mod_states, "mod_count": len(mod_states),
        "enabled_count": sum(1 for v in mod_states.values() if v),
        "updated": datetime.now().isoformat(),
    })
    save_presets(presets)
    return {"ok": True, "preset": presets[profile][preset_id]}

@app.post("/api/presets/{profile}/{preset_id}/load")
def load_preset(profile: str, preset_id: str):
    if is_container_running(profile):
        raise HTTPException(status_code=409, detail="Container is running — stop it first")
    presets = load_presets()
    if profile not in presets or preset_id not in presets[profile]:
        raise HTTPException(status_code=404, detail="Preset not found")
    preset = presets[profile][preset_id]
    moved = apply_preset(profile, preset)
    return {"ok": True, "preset": preset["label"],
            "enabled": moved["enabled"], "disabled": moved["disabled"]}

@app.delete("/api/presets/{profile}/{preset_id}")
def delete_preset(profile: str, preset_id: str):
    presets = load_presets()
    if profile in presets and preset_id in presets[profile]:
        del presets[profile][preset_id]
        save_presets(presets)
    return {"ok": True}

# ── Config File Editor ─────────────────────────────────────────────────────────

CONFIG_EDITABLE_EXTENSIONS = {".cfg", ".json", ".yaml", ".yml", ".ini", ".txt", ".xml", ".toml"}
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2MB

def _safe_resolve(base: Path, rel: str) -> Path:
    """Resolve a path safely within a base directory."""
    resolved = (base / rel).resolve()
    if not str(resolved).startswith(str(base.resolve())):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    return resolved

def _scan_config_files_in_dir(root: Path, prefix: str = "") -> list[dict]:
    """Recursively scan a directory for editable config files."""
    files = []
    if not root.exists():
        return files
    try:
        for item in sorted(root.iterdir()):
            if item.name.startswith(".") or item.name.startswith("__"):
                continue
            rel = f"{prefix}/{item.name}" if prefix else item.name
            if item.is_dir():
                files.extend(_scan_config_files_in_dir(item, rel))
            elif item.suffix.lower() in CONFIG_EDITABLE_EXTENSIONS:
                try:
                    size = item.stat().st_size
                    if size <= MAX_FILE_SIZE:
                        files.append({
                            "path": rel,
                            "name": item.name,
                            "size": size,
                            "ext": item.suffix.lower(),
                            "modified": datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
                        })
                except OSError:
                    pass
    except PermissionError:
        pass
    return files

@app.get("/api/config-files/{profile}")
def list_config_files(profile: str):
    """Scan for editable config files across all known paths for a profile."""
    p = _get_profile(profile)
    sources = []

    # 1. BepInEx/config — derived from plugins_path (sibling of plugins dir)
    plugins_path = p.get("plugins_path", "")
    if plugins_path:
        plugins_dir = Path(plugins_path)
        # plugins_path is typically .../BepInEx/plugins/<profile>
        # We want .../BepInEx/config
        bepinex_dir = None
        # Walk up to find BepInEx directory
        for parent in [plugins_dir] + list(plugins_dir.parents):
            if parent.name == "BepInEx":
                bepinex_dir = parent
                break
            # Also check if parent contains BepInEx
            candidate = parent / "BepInEx"
            if candidate.exists() and candidate.is_dir():
                bepinex_dir = candidate
                break
        if bepinex_dir:
            config_dir = bepinex_dir / "config"
            if config_dir.exists():
                files = _scan_config_files_in_dir(config_dir)
                if files:
                    sources.append({
                        "id": "bepinex-config",
                        "label": "BepInEx/config",
                        "base_path": str(config_dir),
                        "files": files,
                    })

        # 2. BepInEx/plugins subdirs — scan for config files inside plugin folders
        if plugins_dir.exists():
            files = _scan_config_files_in_dir(plugins_dir)
            if files:
                sources.append({
                    "id": "plugins",
                    "label": "BepInEx/plugins",
                    "base_path": str(plugins_dir),
                    "files": files,
                })

    # 3. user/mods/*/config/ directories
    mods_path = p.get("mods_path", "")
    if mods_path:
        mods_dir = Path(mods_path)
        if mods_dir.exists():
            mod_configs = []
            for mod_dir in sorted(mods_dir.iterdir()):
                if not mod_dir.is_dir() or mod_dir.name.startswith(".") or mod_dir.name.startswith("__"):
                    continue
                # Scan the mod directory for config files (config/ subdir + root .json/.cfg)
                config_subdir = mod_dir / "config"
                if config_subdir.exists():
                    for f in _scan_config_files_in_dir(config_subdir):
                        mod_configs.append({
                            **f,
                            "path": f"{mod_dir.name}/config/{f['path']}",
                        })
                # Also scan root of mod for .json and .cfg files
                for item in sorted(mod_dir.iterdir()):
                    if item.is_file() and item.suffix.lower() in CONFIG_EDITABLE_EXTENSIONS:
                        try:
                            size = item.stat().st_size
                            if size <= MAX_FILE_SIZE:
                                mod_configs.append({
                                    "path": f"{mod_dir.name}/{item.name}",
                                    "name": item.name,
                                    "size": size,
                                    "ext": item.suffix.lower(),
                                    "modified": datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
                                })
                        except OSError:
                            pass
            if mod_configs:
                sources.append({
                    "id": "mods",
                    "label": "user/mods",
                    "base_path": str(mods_dir),
                    "files": mod_configs,
                })

    return {"profile": profile, "sources": sources}

@app.get("/api/config-files/{profile}/read")
def read_config_file(profile: str, source: str, path: str):
    """Read a single config file's contents."""
    p = _get_profile(profile)
    sources_map = _build_sources_map(profile, p)
    if source not in sources_map:
        raise HTTPException(status_code=404, detail=f"Source '{source}' not found")
    base = Path(sources_map[source])
    file_path = _safe_resolve(base, path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")
    if file_path.stat().st_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large")
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Read error: {e}")
    return {"path": path, "source": source, "content": content, "size": len(content)}

@app.post("/api/config-files/{profile}/write")
def write_config_file(profile: str, req: ConfigFileWriteRequest):
    """Write updated content to a config file. Expects source as query param."""
    p = _get_profile(profile)
    # Extract source from path prefix or query
    # We pass source in the path field as "source::relative/path"
    if "::" not in req.path:
        raise HTTPException(status_code=400, detail="Path must be source::relative/path")
    source, rel_path = req.path.split("::", 1)
    sources_map = _build_sources_map(profile, p)
    if source not in sources_map:
        raise HTTPException(status_code=404, detail=f"Source '{source}' not found")
    base = Path(sources_map[source])
    file_path = _safe_resolve(base, rel_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")
    # Create backup
    backup = file_path.with_suffix(file_path.suffix + ".bak")
    try:
        shutil.copy2(str(file_path), str(backup))
    except Exception:
        pass  # Non-fatal
    try:
        file_path.write_text(req.content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Write error: {e}")
    return {"ok": True, "path": rel_path, "source": source, "size": len(req.content)}

def _build_sources_map(profile: str, p: dict) -> dict:
    """Build a map of source_id -> base_path for a profile."""
    sources = {}
    plugins_path = p.get("plugins_path", "")
    if plugins_path:
        plugins_dir = Path(plugins_path)
        bepinex_dir = None
        for parent in [plugins_dir] + list(plugins_dir.parents):
            if parent.name == "BepInEx":
                bepinex_dir = parent
                break
            candidate = parent / "BepInEx"
            if candidate.exists() and candidate.is_dir():
                bepinex_dir = candidate
                break
        if bepinex_dir:
            config_dir = bepinex_dir / "config"
            if config_dir.exists():
                sources["bepinex-config"] = str(config_dir)
        if plugins_dir.exists():
            sources["plugins"] = str(plugins_dir)
    mods_path = p.get("mods_path", "")
    if mods_path:
        sources["mods"] = mods_path
    return sources

# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.get("/api/auth/check")
def auth_check(request: Request):
    if not AUTH_ENABLED:
        return {"auth_enabled": False, "authenticated": True}
    token = request.cookies.get("rmm_session")
    return {"auth_enabled": True, "authenticated": _validate_session(token)}

@app.post("/api/auth/login")
def auth_login(req: AuthLoginRequest, response: Response):
    if not AUTH_ENABLED:
        return {"ok": True}
    if not _check_password(req.password):
        raise HTTPException(status_code=401, detail="Invalid password")
    token, _ = _create_session()
    response.set_cookie(key="rmm_session", value=token, httponly=True,
                        samesite="lax", secure=False,
                        max_age=AUTH_SESSION_HOURS * 3600, path="/")
    return {"ok": True}

@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    token = request.cookies.get("rmm_session")
    if token and token in _sessions: del _sessions[token]
    response.delete_cookie("rmm_session", path="/")
    return {"ok": True}

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "3.0.0", "data_dir": str(DATA_DIR)}

# ── Static / Frontend ─────────────────────────────────────────────────────────
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/", response_class=HTMLResponse)
def root():
    index = static_dir / "index.html"
    if index.exists(): return index.read_text()
    return HTMLResponse("<h1>Tarkov Mod Manager</h1><p>Frontend not found.</p>")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7272, reload=False)
