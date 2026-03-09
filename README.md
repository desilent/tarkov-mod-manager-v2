# ◈ RAID MOD MANAGER

Web-based mod manager for **SPT-Tarkov**, **FIKA Server** and **FIKA Headless**.
Hosted on Unraid, accessible from any browser on your network.

![Version](https://img.shields.io/badge/version-3.0.0-f0c050?style=flat-square)
![SPT](https://img.shields.io/badge/SPT-4.0-50e8c8?style=flat-square)
![License](https://img.shields.io/badge/license-CC--BY--NC--SA--4.0-8899aa?style=flat-square)

---

## ✨ Features

| Feature | Description |
|:---|:---|
| **🎮 Mod Management** | Multi-profile support · Smart install (upload or URL, auto-detects server mods vs plugins) · Enable/disable without deleting · Version detection from `package.json`, DLL metadata & BepInEx attributes |
| **🎯 Presets** | Save/load named mod configurations · Strict mode disables unlisted mods · Optional config file snapshots bundled with presets — restore both mods and configs in one click |
| **⚙️ Config Editor** | VS Code Dark+ themed editor · Syntax highlighting (`.cfg` `.json` `.yaml` `.xml` `.ini` `.toml`) · Find & Replace with regex · Auto-backup on save · Live container awareness |
| **📋 Logs** | Full-height log viewer · Container sub-tabs with live status dots · Real-time filtering · ANSI color support · Auto-refresh with inline Start/Stop/Restart controls |
| **🔌 WebSocket** | Real-time push updates for container status and log streaming · Automatic fallback to HTTP polling · Exponential backoff reconnect |
| **🐳 Containers** | Start/Stop/Restart via Docker socket · Live status polling · Mod locking when running · Status visible across all tabs |
| **🔐 Auth** | Optional password login · 72h sessions · Themed login screen · LAN-friendly (disabled by default) |

---

## 🚀 Setup: GitHub → ghcr.io → Unraid

### Step 1 — Create a GitHub repository

1. Go to **github.com → New repository**, name it `raid-mod-manager`, set **Public**
2. Push this project:

```bash
git init
git remote add origin https://github.com/desilent/raid-mod-manager.git
git add .
git commit -m "Initial commit"
git push -u origin main
```

### Step 2 — GitHub Actions builds the image automatically

On every push to `main`, the workflow builds and pushes to:
`ghcr.io/desilent/tarkov-mod-manager-v2:latest`

No secrets needed — it uses the built-in GITHUB_TOKEN.
Check progress under the **Actions** tab.

### Step 3 — Make the package public (first time only)

After the first build:
- GitHub profile → **Packages** → `raid-mod-manager` → **Package settings** → set **Public**

---

## 📦 Install on Unraid

### Option A — Unraid Template (easiest)

1. Copy `unraid-template.xml` to:
   `/boot/config/plugins/dockerMan/templates-user/raid-mod-manager.xml`
2. Unraid → Docker → Add Container → scroll to **User Templates** → select it
3. Update the mod folder paths → Apply

### Option B — Manual Add Container

| Field | Value |
|:---|:---|
| Repository | `ghcr.io/desilent/tarkov-mod-manager-v2:latest` |
| Port | `7272 → 7272` |
| Path `/data` | `/mnt/user/appdata/raid-mod-manager/data` |
| Path `/mods/fika-server` | `/mnt/user/appdata/fika-server/user/mods` |
| Path `/plugins/fika-server` | `/mnt/user/appdata/fika-server/BepInEx/plugins` |
| Path `/mods/fika-headless` | `/mnt/user/appdata/fika-headless/user/mods` |
| Path `/plugins/fika-headless` | `/mnt/user/appdata/fika-headless/BepInEx/plugins` |
| Variable `TZ` | `Europe/Berlin` |

Open: **http://YOUR_UNRAID_IP:7272**

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|:---|:---|:---|
| `TZ` | `Europe/Berlin` | Timezone |
| `PROFILE_FIKA_SERVER_LABEL` | `FIKA Server` | UI display name |
| `PROFILE_FIKA_SERVER_MODS_PATH` | `/mods/fika-server` | Server mods path (user/mods) |
| `PROFILE_FIKA_SERVER_PLUGINS_PATH` | `/plugins/fika-server` | BepInEx plugins path |
| `PROFILE_FIKA_SERVER_COLOR` | `#e8b84b` | Accent colour |
| `PROFILE_FIKA_SERVER_CONTAINER` | *(empty)* | Docker container name for start/stop |
| `PROFILE_FIKA_HEADLESS_LABEL` | `FIKA Headless` | UI display name |
| `PROFILE_FIKA_HEADLESS_MODS_PATH` | `/mods/fika-headless` | Server mods path |
| `PROFILE_FIKA_HEADLESS_PLUGINS_PATH` | `/plugins/fika-headless` | BepInEx plugins path |
| `PROFILE_FIKA_HEADLESS_COLOR` | `#4be8c0` | Accent colour |
| `PROFILE_FIKA_HEADLESS_CONTAINER` | *(empty)* | Docker container name for start/stop |
| `AUTH_PASSWORD` | *(empty)* | Set to require login; leave empty to disable |
| `AUTH_SESSION_HOURS` | `72` | Session duration before re-login required |
| `DOCKER_SOCKET` | `/var/run/docker.sock` | Path to Docker socket |

**Adding extra profiles:** any `PROFILE_<ID>_MODS_PATH` env var creates a new profile automatically.
Add `_LABEL`, `_COLOR`, `_PLUGINS_PATH`, and `_CONTAINER` to customise it.

---

## 🪟 SPT Client on Windows

**Option A (recommended):** Share `C:\SPT\BepInEx\plugins` via Windows network share,
mount it on Unraid via Unassigned Devices, map to `/plugins/spt-client`.

**Option B:** Run a second local instance on Windows:
```powershell
pip install -r requirements.txt
$env:PROFILE_SPT_CLIENT_MODS_PATH = "C:\SPT\user\mods"
$env:PROFILE_SPT_CLIENT_PLUGINS_PATH = "C:\SPT\BepInEx\plugins"
python app/main.py   # → http://localhost:7272
```

---

## 📖 How It Works

**Mod toggling** — Disabling a mod moves it to a `__disabled__/` subfolder. Re-enabling moves it back. No files are ever deleted by toggling.

**Presets (strict mode)** — Loading a preset disables any mod not in the preset. Optionally bundles config file snapshots — when loaded, all configs are restored from the snapshot (with `.bak` backups).

**Version detection** — Reads mod versions from multiple sources in priority order:
1. `package.json` (standard SPT server mods)
2. JSON config files (`config`, `config.json`, `mod.json`, `manifest.json`)
3. BepInEx `[BepInPlugin]` attribute in .NET DLLs (parses the #US heap for GUID + version)
4. .NET `AssemblyFileVersion` / `AssemblyInformationalVersion` attributes
5. PE `VS_FIXEDFILEINFO` version resource

**WebSocket** — The UI connects via WebSocket for real-time updates. Container status changes and log lines are pushed instantly instead of polled. Falls back to HTTP polling automatically if WebSocket is unavailable.

**Config editor** — Creates `.bak` backups before every save. Shows a disclaimer banner when the container is running since some changes may require a restart.

---

## 🔐 Authentication

For remote access via reverse proxy (Traefik, nginx, etc.):

```yaml
AUTH_PASSWORD: "your-secret-password"
```

- Themed login screen when password is set
- 72h sessions (configurable), stored in memory
- Restarting the container invalidates all sessions
- Leave empty or unset for LAN-only use (no login required)

---

## 🗺️ Roadmap

```
 ✅ SHIPPED                           🔧 PLANNED
 ──────────────────────────────────   ──────────────────────────────────
 ◈ Multi-profile mod management       ◇ Cross-profile mod sync
 ◈ Smart installer (upload / URL)      ◇ SPT version compatibility
 ◈ Mod presets with config snapshots   ◇ Forge / GitHub update checker
 ◈ Container management (Docker)       ◇ Mod dependency resolution
 ◈ Password authentication             ◇ Bulk mod operations
 ◈ Config file editor (VS Code)        ◇ Mod backup / rollback
 ◈ Container logs (full-height)        ◇ Drag-and-drop mod ordering
 ◈ Version detection (DLL parsing)     ◇ Multi-user support
 ◈ BepInEx metadata extraction
 ◈ WebSocket live reload
```

---

## License

[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)
