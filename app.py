"""LabResultsArchive PWA - phone-first file searcher + blood-value lookup.

Architecture: see PHONE_WEB_APP_PLAYBOOK.md (cloned from ImageVideoWeb).

Phase 1: bare shell - tunnel up, manifest + sw.js served, install button works.
Phase 2 will add the file-indexer and /api/files. Phase 3 adds /api/bloodvalues.
"""
import atexit
import ctypes
import hashlib
import json
import os
import re
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.serving import make_server


# -----------------------------
# Frozen-aware paths
# -----------------------------
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    APP_DIR = Path(sys.executable).resolve().parent
    _meipass = getattr(sys, "_MEIPASS", None)
    _res_candidates = ([Path(_meipass)] if _meipass else []) + [APP_DIR]
    RES_DIR = next(
        (c for c in _res_candidates if (c / "templates" / "index.html").is_file()),
        _res_candidates[0],
    )
else:
    APP_DIR = Path(__file__).resolve().parent
    RES_DIR = APP_DIR


# -----------------------------
# Data folder + .env
# -----------------------------
DATA_DIR = APP_DIR / "DATA" / "LabResultsArchive"
JOBS_DIR = DATA_DIR / "Jobs"
ENV_PATH = DATA_DIR / ".env"

for d in (DATA_DIR, JOBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

load_dotenv(ENV_PATH)


def env(name: str, default: str = "") -> str:
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) else default


# -----------------------------
# Configuration
# -----------------------------
APP_NAME = "LabResultsArchive"
ICON_VER = "2"

SERVER_PORT = int(env("APP_SERVER_PORT", "20000") or "20000")
APP_PUBLIC_HOST = env("APP_PUBLIC_HOST")
APP_PUBLIC_PORT = env("APP_PUBLIC_PORT", str(SERVER_PORT))
APP_ACCESS_TOKEN = env("APP_ACCESS_TOKEN", "")
APP_PUBLIC_URL = env("APP_PUBLIC_URL", "")
CLOUDFLARE_TUNNEL_TOKEN = env("CLOUDFLARE_TUNNEL_TOKEN", "")


# -----------------------------
# File indexer + blood-value config
# -----------------------------
INDEXED_FOLDERS = [
    ("blood_exports", "Blood Exports", APP_DIR / "DATA" / "LabResultsArchive" / "exports"),
    ("blood_pdfs",    "Blood PDFs",    APP_DIR / "DATA" / "LabResultsArchive" / "original_pdfs"),
]
# Optional extra folder: set LAB_RESULTS_EXTRA_FOLDER to any absolute path
# (e.g. a sibling project's data dir) to expose it in the PWA's Files tab too.
_extra = env("LAB_RESULTS_EXTRA_FOLDER", "").strip()
if _extra and Path(_extra).is_dir():
    INDEXED_FOLDERS.append(("extra", "Extra", Path(_extra)))
ALLOWED_EXT = {".pdf", ".md", ".png", ".jpg", ".jpeg", ".bmp", ".txt"}
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "backups", "node_modules"}
BLOOD_RESULTS_PATH = APP_DIR / "DATA" / "LabResultsArchive" / "blood_test_results.json"

_INDEX: List[Dict] = []
_BLOOD_RECORDS: List[Dict] = []
_INDEX_LOCK = threading.Lock()

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _file_id(abs_path: str) -> str:
    return hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:12]


def _extract_date(name: str) -> str:
    m = DATE_RE.search(name)
    return m.group(1) if m else ""


def build_index() -> None:
    """Walk INDEXED_FOLDERS, populate _INDEX (newest first)."""
    new_index: List[Dict] = []
    for folder_id, label, base in INDEXED_FOLDERS:
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in ALLOWED_EXT:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            abs_path = str(path.resolve())
            new_index.append({
                "id": _file_id(abs_path),
                "folder_id": folder_id,
                "folder_label": label,
                "name": path.name,
                "rel_path": str(path.relative_to(base)).replace("\\", "/"),
                "abs_path": abs_path,
                "ext": path.suffix.lower(),
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "date": _extract_date(path.name),
            })
    new_index.sort(key=lambda f: (f["date"] or "0000-00-00", f["mtime"]), reverse=True)
    with _INDEX_LOCK:
        _INDEX.clear()
        _INDEX.extend(new_index)


def load_blood_records() -> None:
    """Load and cache records from blood_test_results.json."""
    global _BLOOD_RECORDS
    try:
        with open(BLOOD_RESULTS_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
        _BLOOD_RECORDS = doc.get("records", []) if isinstance(doc, dict) else []
    except (OSError, json.JSONDecodeError):
        _BLOOD_RECORDS = []


def _lookup(fid: str) -> Optional[Dict]:
    with _INDEX_LOCK:
        for f in _INDEX:
            if f["id"] == fid:
                return f
    return None


# -----------------------------
# Flask app + token middleware
# -----------------------------
app = Flask(
    __name__,
    template_folder=str(RES_DIR / "templates"),
    static_folder=str(RES_DIR / "static"),
)


class TokenPrefixMiddleware:
    """If APP_ACCESS_TOKEN is set, require it as the first path segment.

    Strips it so Flask routes work unchanged; everything else returns 404.
    Token empty (default) -> transparent pass-through (Cloudflare Access gates).
    """

    def __init__(self, wsgi_app, token: str):
        self.wsgi_app = wsgi_app
        self.prefix = f"/{token}" if token else ""

    def __call__(self, environ, start_response):
        if not self.prefix:
            return self.wsgi_app(environ, start_response)
        path = environ.get("PATH_INFO", "")
        if path == self.prefix or path.startswith(self.prefix + "/"):
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + self.prefix
            return self.wsgi_app(environ, start_response)
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"Not Found"]


app.wsgi_app = TokenPrefixMiddleware(app.wsgi_app, APP_ACCESS_TOKEN)


# -----------------------------
# Routes
# -----------------------------
@app.route("/")
def index():
    return render_template(
        "index.html",
        api_base=request.script_root,
        public_url=APP_PUBLIC_URL,
        icon_ver=ICON_VER,
        app_name=APP_NAME,
    )


@app.route("/manifest.json")
def manifest():
    base = request.script_root
    v = f"?v={ICON_VER}"
    return jsonify({
        "name": APP_NAME,
        "short_name": APP_NAME,
        "start_url": base + "/",
        "scope": base + "/",
        "display": "standalone",
        "background_color": "#13121a",
        "theme_color": "#13121a",
        "icons": [
            {"src": base + "/static/icon-192.png" + v, "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": base + "/static/icon-512.png" + v, "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
            {"src": base + "/static/icon-maskable-512.png" + v, "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    })


_SERVICE_WORKER_JS = """
// Minimal service worker - its only jobs are (a) to exist with a fetch
// handler so the browser treats the site as an installable PWA, and
// (b) to update instantly (no stale-asset traps). It does NOT cache.
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));
""".lstrip()


@app.route("/sw.js")
def service_worker():
    resp = app.response_class(_SERVICE_WORKER_JS, mimetype="text/javascript")
    resp.headers["Service-Worker-Allowed"] = (request.script_root or "") + "/"
    resp.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return resp


@app.after_request
def _no_cache_pwa_assets(resp):
    p = request.path
    if p in ("/manifest.json", "/sw.js") or p.startswith("/static/icon"):
        resp.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return resp


# -----------------------------
# File browser API
# -----------------------------
@app.route("/api/folders")
def api_folders():
    return jsonify({
        "items": [
            {"id": fid, "label": label, "exists": base.is_dir()}
            for fid, label, base in INDEXED_FOLDERS
        ]
    })


@app.route("/api/files")
def api_files():
    q = (request.args.get("q") or "").strip().lower()
    folder = request.args.get("folder") or ""
    with _INDEX_LOCK:
        items = list(_INDEX)
    if folder:
        items = [f for f in items if f["folder_id"] == folder]
    if q:
        items = [f for f in items if q in f["name"].lower()]
    return jsonify({
        "count": len(items),
        "items": [
            {
                "id": f["id"],
                "folder_id": f["folder_id"],
                "folder_label": f["folder_label"],
                "name": f["name"],
                "date": f["date"],
                "size": f["size"],
                "ext": f["ext"],
            }
            for f in items[:500]
        ],
    })


@app.route("/api/file/<fid>")
def api_file(fid):
    f = _lookup(fid)
    if not f:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": f["id"], "name": f["name"],
        "folder_label": f["folder_label"],
        "date": f["date"], "size": f["size"], "ext": f["ext"],
    })


@app.route("/download/<fid>")
def download_file(fid):
    f = _lookup(fid)
    if not f:
        return ("not found", 404)
    return send_file(f["abs_path"], as_attachment=True, download_name=f["name"])


@app.route("/preview/<fid>")
def preview_file(fid):
    f = _lookup(fid)
    if not f:
        return ("not found", 404)
    return send_file(f["abs_path"], as_attachment=False, download_name=f["name"])


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    build_index()
    load_blood_records()
    return jsonify({"ok": True, "files": len(_INDEX), "records": len(_BLOOD_RECORDS)})


# -----------------------------
# Blood values API
# -----------------------------
@app.route("/api/bloodtests")
def api_bloodtests():
    names = sorted({
        r.get("test_name_en", "")
        for r in _BLOOD_RECORDS
        if r.get("test_name_en")
    })
    return jsonify({"items": names})


@app.route("/api/bloodvalues")
def api_bloodvalues():
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"count": 0, "items": []})
    matches = [
        {
            "date": r.get("date", ""),
            "test_name_en": r.get("test_name_en", ""),
            "value": r.get("value", ""),
            "unit": r.get("unit", ""),
            "source_files": r.get("source_files", []),
        }
        for r in _BLOOD_RECORDS
        if q in r.get("test_name_en", "").lower()
    ]
    matches.sort(key=lambda r: r["date"])
    return jsonify({"count": len(matches), "items": matches})


# -----------------------------
# Cloudflare tunnel launcher
# -----------------------------
_TUNNEL_PROC = None
_TUNNEL_JOB = None


def _bind_child_to_parent_lifetime(proc) -> None:
    """Windows: put the child in a Job with KILL_ON_JOB_CLOSE so if THIS
    process dies - even force-killed in Task Manager - the OS also kills
    cloudflared. Prevents an orphaned tunnel that serves 502 to everyone.
    Best-effort; clean exits are still covered by _stop_tunnel/atexit."""
    if not sys.platform.startswith("win"):
        return
    global _TUNNEL_JOB
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class _BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC),
                ("IoInfo", _IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        # CRITICAL: declare argtypes/restype. Without these the 64-bit HANDLE
        # args are passed as 32-bit c_int on 64-bit Python -> the handle is
        # corrupted, AssignProcessToJobObject silently fails, and cloudflared
        # is never actually in the job (KILL_ON_JOB_CLOSE never fires).
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        hjob = k32.CreateJobObjectW(None, None)
        if not hjob:
            return
        info = _EXT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
            hjob, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info)
        ):
            return
        if not k32.AssignProcessToJobObject(hjob, int(proc._handle)):
            return
        _TUNNEL_JOB = hjob  # MUST stay open: closing it would kill the child now
    except Exception:
        pass


def _find_cloudflared() -> Optional[str]:
    import shutil
    found = shutil.which("cloudflared")
    if found:
        return found
    for c in (
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
    ):
        if Path(c).is_file():
            return c
    return None


def _kill_orphan_cloudflared() -> int:
    """Self-heal: kill any cloudflared.exe from a prior run that carries OUR
    tunnel token. An orphan keeps the tunnel 'up' but points at a dead local
    origin, so Cloudflare load-balances phone traffic onto it -> 502s. Matched
    by token in Python so other apps' cloudflared tunnels are never touched."""
    if not sys.platform.startswith("win") or not CLOUDFLARE_TUNNEL_TOKEN:
        return 0
    CREATE_NO_WINDOW = 0x08000000
    try:
        import subprocess
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=8, creationflags=CREATE_NO_WINDOW,
        )
        raw = (res.stdout or "").strip()
        if not raw:
            return 0
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        killed = 0
        for item in data:
            if CLOUDFLARE_TUNNEL_TOKEN in (item.get("CommandLine") or ""):
                pid = int(item.get("ProcessId") or 0)
                if pid > 0:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, creationflags=CREATE_NO_WINDOW)
                    killed += 1
        if killed:
            print(f"Self-heal: killed {killed} orphaned cloudflared process(es).",
                  flush=True)
        return killed
    except Exception as exc:
        print(f"Orphan cleanup skipped ({exc}).", flush=True)
        return 0


def _start_tunnel() -> None:
    """Spawn the named cloudflared tunnel if a token is configured.
    Never raises - a tunnel problem must not stop the local app."""
    global _TUNNEL_PROC
    if not CLOUDFLARE_TUNNEL_TOKEN:
        return
    _kill_orphan_cloudflared()
    try:
        import subprocess
        cf = _find_cloudflared()
        if not cf:
            print("CLOUDFLARE_TUNNEL_TOKEN set but cloudflared not found "
                  "(winget install Cloudflare.cloudflared).", flush=True)
            return
        log = open(JOBS_DIR / "cloudflared.log", "a", encoding="utf-8", errors="replace")
        creationflags = 0x08000000 if sys.platform.startswith("win") else 0  # CREATE_NO_WINDOW
        _TUNNEL_PROC = subprocess.Popen(
            [cf, "tunnel", "--no-autoupdate", "run", "--token", CLOUDFLARE_TUNNEL_TOKEN],
            stdout=log, stderr=subprocess.STDOUT, creationflags=creationflags,
        )
        _bind_child_to_parent_lifetime(_TUNNEL_PROC)
        print("Cloudflare tunnel started"
              + (f" -> {APP_PUBLIC_URL}" if APP_PUBLIC_URL else ""), flush=True)
    except Exception as e:
        print(f"Tunnel failed to start ({e}); local app still running.", flush=True)


def _stop_tunnel() -> None:
    global _TUNNEL_PROC
    if _TUNNEL_PROC is not None:
        try:
            _TUNNEL_PROC.terminate()
        except Exception:
            pass
        _TUNNEL_PROC = None


atexit.register(_stop_tunnel)


# -----------------------------
# Run
# -----------------------------
def start_pwa_server():
    """Build the file index, start the Cloudflare tunnel, and return a running
    werkzeug server. The caller runs server.serve_forever() (blocking): in a
    background thread when lab_results_archive.py hosts the PWA in-process,
    or directly from main() for standalone use."""
    print("Indexing files...", flush=True)
    build_index()
    load_blood_records()
    print(f"Indexed {len(_INDEX)} files, {len(_BLOOD_RECORDS)} blood-value records.", flush=True)
    _start_tunnel()
    server = make_server("0.0.0.0", SERVER_PORT, app, threaded=True)
    print(f"{APP_NAME} listening on 0.0.0.0:{SERVER_PORT}"
          + (f" -> {APP_PUBLIC_URL}" if APP_PUBLIC_URL else ""), flush=True)
    return server


def stop_pwa_server(server) -> None:
    """Shut down an in-process server (from start_pwa_server) and the tunnel.
    Safe to call after serve_forever() has already returned."""
    if server is not None:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
    _stop_tunnel()


def main():
    server = start_pwa_server()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping...", flush=True)
    finally:
        stop_pwa_server(server)


if __name__ == "__main__":
    main()
