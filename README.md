# ◈ RAID MOD MANAGER

Web-based mod manager for SPT-Tarkov, FIKA Server and FIKA Headless.
Hosted on Unraid, accessible from any browser on your network.

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
|---|---|
| Repository | `ghcr.io/desilent/tarkov-mod-manager-v2:latest` |
| Port | `7272 → 7272` |
| Path `/data` | `/mnt/user/appdata/raid-mod-manager/data` |
| Path `/mods/fika-server` | `/mnt/user/appdata/fika-server/BepInEx/plugins` |
| Path `/mods/fika-headless` | `/mnt/user/appdata/fika-headless/BepInEx/plugins` |
| Variable `TZ` | `Europe/Berlin` |

Open: **http://YOUR_UNRAID_IP:7272**

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TZ` | `Europe/Berlin` | Timezone |
| `PROFILE_FIKA_SERVER_LABEL` | `FIKA Server` | UI display name |
| `PROFILE_FIKA_SERVER_PATH` | `/mods/fika-server` | Internal path |
| `PROFILE_FIKA_SERVER_COLOR` | `#e8b84b` | Accent colour |
| `PROFILE_FIKA_HEADLESS_LABEL` | `FIKA Headless` | UI display name |
| `PROFILE_FIKA_HEADLESS_PATH` | `/mods/fika-headless` | Internal path |
| `PROFILE_FIKA_HEADLESS_COLOR` | `#4be8c0` | Accent colour |
| `PROFILE_SPT_CLIENT_LABEL` | `SPT Client` | UI display name |
| `PROFILE_SPT_CLIENT_PATH` | `/mods/spt-client` | Internal path |
| `PROFILE_SPT_CLIENT_COLOR` | `#e84b4b` | Accent colour |

**Adding extra profiles:** any `PROFILE_<ID>_PATH` var without a matching built-in
creates a new profile automatically. Add `_LABEL` and `_COLOR` to customise it.

---

## 🪟 SPT Client on Windows

**Option A (recommended):** Share `C:\SPT\BepInEx\plugins` via Windows network share,
mount it on Unraid via Unassigned Devices, map to `/mods/spt-client`.

**Option B:** Run a second local instance on Windows:
```powershell
pip install -r requirements.txt
$env:PROFILE_SPT_CLIENT_PATH = "C:\SPT\BepInEx\plugins"
python app/main.py   # → http://localhost:7272
```

---

## How mod toggling works

Disabling a mod moves it to a `__disabled__` subfolder. Re-enabling moves it back.
No files are ever deleted by toggling.

---

## Roadmap

- [ ] Auto-update checker (compare versions vs GitHub releases)
- [ ] Mod presets / profiles (save sets of enabled mods)
- [ ] Cross-profile mod sync
- [ ] WebSocket live reload
- [ ] SPT version compatibility warnings
