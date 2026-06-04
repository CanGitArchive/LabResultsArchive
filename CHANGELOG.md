# Changelog

All notable engineering decisions and milestones for **Lab Results Archive**.

This is the public-release log; older internal-development history is squashed
into the initial commit because the original working tree contained real
personal medical records that don't belong in a public repo.

## v0.1.0: Initial public release

**Architecture:** single PyQt6 desktop app + tray controller that hosts the
Flask PWA **in-process** on a background thread. No subprocess for the web
server, one Python environment runs everything, one PyInstaller build target.

### Highlights

- **In-process Flask hosting.** Earlier iterations spawned `app.py` as a
  child Python process from the tray (PWA subprocess gets its own console
  window on Windows, two interpreters to manage). The rebuild merges them:
  `lab_results_archive.py` does `import app`, calls
  `app.start_pwa_server()` (which returns a `werkzeug.serving.make_server`
  instance), and runs `server.serve_forever()` on a daemon thread. The
  Qt main thread keeps owning the event loop; `server.shutdown()` +
  `server.server_close()` from the tray's Stop PWA action unblocks
  `serve_forever` and frees port 20000 cleanly so a restart works.

- **Frozen-windowed `print()` survival.** PyInstaller's `--windowed` mode
  leaves `sys.stdout` / `sys.stderr` set to `None`; any `print()` then raises
  `RuntimeError: lost sys.stdout` and crashes the app. The Flask request
  logger and the cloudflared startup banner both write freely, so
  `_ensure_std_streams()` in the tray's `main()` swaps `None` streams for an
  append-only handle on `DATA/LabResultsArchive/Jobs/lab_results_archive.log`
  before anything else runs.

- **Cloudflare tunnel with reliable child reaping.** `app.py` auto-spawns
  `cloudflared tunnel run --token <…>` when a token is configured, bound to a
  Windows Job Object with `KILL_ON_JOB_CLOSE` so a force-kill of the parent
  reaps the tunnel child too. The Job-object pattern looked correct but was
  silently broken on 64-bit Python, see the writeup in
  [docs/CLOUDFLARED_ORPHAN_CHECKUP.md](docs/CLOUDFLARED_ORPHAN_CHECKUP.md).
  Short version: ctypes was truncating the `HANDLE` to 32 bits on every call,
  `AssignProcessToJobObject` was failing with no exception, and every restart
  was leaving an orphan `cloudflared` connected to the edge but pointing at a
  dead local origin, Cloudflare load-balanced phone traffic onto the orphans
  and the PWA went "unreachable from time to time." Fix: declare proper
  `argtypes` / `restype` for every kernel32 call. Belt-and-suspenders:
  `_kill_orphan_cloudflared()` self-heals on every startup by matching the
  tunnel token *in Python* (never via a shell-injectable filter) so it can
  kill leftover children from prior runs without ever touching another app's
  unrelated `cloudflared` process.

- **Portable data layout.** `get_app_dir()` resolves to the script's folder
  in dev and to `Path(sys.executable).parent` in a PyInstaller `--onefile`
  build, so `DATA/LabResultsArchive/` and its `.env` always live next to the
  thing you launched. Move the folder; move your data with it.

- **Reference-range model.** `test_ranges.json` describes each test as a
  numeric interval, an upper limit, an expected-values qualitative set, or
  "no universal range." The GUI table colors out-of-range cells (`low /
  high`), qualitative-unexpected cells (`expected_values` mismatch), and
  leaves cells with no defined range uncolored. Both data files default to
  empty / created on first run, populate via the Edit tab.

- **Tray menu.** Open GUI · Open PWA in browser · Start/Stop PWA · ✅ Run at
  Windows startup · Exit. The "Run at startup" toggle writes/deletes the
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\LabResultsArchive`
  registry value via stdlib `winreg`, preferring `pythonw.exe` over
  `python.exe` for the autostart command so no console flashes at login.

### Known scope at v0.1

- Phone PWA is **read-only** for the lab data; writes happen via the
  desktop GUI. (The PWA was never meant to be the entry surface; it's the
  field-query / share surface.)
- Source-PDF text extraction is **manual**; the GUI's Edit tab is the
  authoritative way to add records. (Older private builds had an
  AI-assisted extraction flow that doesn't ship here.)
- Tested on Windows 11 / Python 3.12. Other Python 3.10+ should work; macOS
  and Linux mostly work but the tunnel reaping and Run-at-startup pieces
  are Windows-specific (no-ops elsewhere).
