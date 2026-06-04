# Lab Results Archive

A single-file PyQt6 desktop app for archiving your own blood-test (and other
lab) results over time, with an installable phone app (PWA) so you can pull
up "show me my Vitamin D over the years" from anywhere. All data lives on your
own machine as plain JSON; no cloud account, no third-party storage.

<img width="1494" height="646" alt="BloodTestView" src="https://github.com/user-attachments/assets/461933eb-e0f4-485a-bb26-f6a43250cfc1" />

## Features

- **Long-term lab archive**: drop result PDFs in a folder, enter the values in
  the desktop GUI, and get a per-test history with reference-range coloring.
- **Phone access (PWA)**: installable web app for file search across your
  indexed folders and per-test lookups ("Vitamin D" → every recorded date);
  the Web Share API ships a PDF to WhatsApp in two taps.
- **System-tray controller**: hosts the Flask PWA in-process on a background
  thread (no subprocess, no second console); right-click to open the GUI,
  start/stop the phone server, or run at Windows startup.
- **Doctor-ready exports**: per-date PDF / Markdown reports plus an
  AI-analysis ZIP bundle for discussing a particular draw.
- **Optional Cloudflare tunnel**: one env var makes the PWA reachable
  HTTPS-from-anywhere, gated by your tunnel's access policy. LAN-only by
  default.

See [CHANGELOG.md](CHANGELOG.md) for the engineering log and
[docs/CLOUDFLARED_ORPHAN_CHECKUP.md](docs/CLOUDFLARED_ORPHAN_CHECKUP.md) for
the diagnostic on a subtle 64-bit `HANDLE` ctypes bug that was silently
orphaning the tunnel child process across every restart.

## Tech

Python 3.12, PyQt6 (desktop + tray), Flask + werkzeug `make_server` (PWA on a
background thread), vanilla-JS PWA, no build step, no framework. Single Python
environment runs everything.

## Install & run

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python lab_results_archive.py
```

The app creates a portable `DATA/LabResultsArchive/` folder next to the script
(or beside the `.exe` when packaged) for your results JSON, source PDFs,
exports, and backups, copy the folder to take your data with you. First-run
shows an empty archive; add records through the GUI's Edit tab.

## Phone access

The app serves an installable PWA on your LAN at `http://<your-pc-ip>:20000/`
(set the `APP_SERVER_PORT` env var to change the port). On a phone, open that
URL in Chrome/Safari → "Add to Home Screen" and you get a standalone app icon.

For **HTTPS-from-anywhere**, point a
[Cloudflare named tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
at `http://localhost:20000`, drop the token into `DATA/LabResultsArchive/.env`:

```dotenv
CLOUDFLARE_TUNNEL_TOKEN=<your-tunnel-token>
APP_PUBLIC_URL=https://lab.example.com
```

…and the app auto-spawns `cloudflared` on launch, reaps it on exit (the Job
Object pattern that drove the `docs/CLOUDFLARED_ORPHAN_CHECKUP.md` writeup),
and the tray's "Open PWA in browser" menu jumps to your public URL.

## Build a standalone `.exe` (optional, Windows)

```powershell
pyinstaller --onefile --windowed --name LabResultsArchive `
  --icon icon.ico `
  --add-data "templates;templates" `
  --add-data "static;static" `
  lab_results_archive.py
```

Keep `icon.ico` next to the resulting `.exe`; the app finds it automatically
via the same `get_resource_path()` resolver used in dev mode. When a windowed
build's `print()`s have no console to write to, the app routes them to
`DATA/LabResultsArchive/Jobs/lab_results_archive.log` instead of crashing,
useful for debugging frozen builds.
