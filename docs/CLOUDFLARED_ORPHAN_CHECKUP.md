# Cloudflared Orphan Checkup

A portable diagnostic for **any** of the user's apps that expose a phone/web
UI through a Cloudflare tunnel (`cloudflared`) the app spawns itself. Hand this
file to a Claude Code session working on another app and have it run the
checks below against that app's source + the live machine.

Found and fixed in **HealthTracker** (2026-05-21). If another app shows the
same symptom, it almost certainly has the same bug.

---

## Symptom

- The phone PWA / web app "disconnects from the internet from time to time",
  shows a **blank page** or a 502.
- Gets worse after the desktop app is **closed and reopened** a few times.
- Inside an installed PWA the screen often goes fully blank (the no-op service
  worker does `respondWith(fetch(...))`; a 502 with an empty body paints
  nothing).

## What's actually wrong

The app spawns `cloudflared` as a child process and is supposed to kill it
when the app exits. If that teardown doesn't work, `cloudflared` becomes an
**orphan**: it stays connected to the Cloudflare edge and keeps the tunnel
"up", but it now points at a **dead local origin** (the app's HTTP port is
closed). Cloudflare happily routes phone traffic to it → every request → 502.
Worse, each app launch spawns *another* cloudflared, so orphans **stack** and
Cloudflare load-balances across all of them.

---

## Step 1: Live diagnosis (PowerShell, on the machine)

**A. With the desktop app fully CLOSED, look for cloudflared:**

```powershell
Get-Process cloudflared -ErrorAction SilentlyContinue | Format-Table Id,StartTime -AutoSize
```

- App closed and **this lists nothing** → good, no orphan.
- App closed and **cloudflared is still running** → **ORPHAN CONFIRMED.**

**B. With the app RUNNING, there should be exactly ONE cloudflared per app.**
List them with command lines (each app's tunnel has a unique `--token`):

```powershell
Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" |
  Select-Object ProcessId,CreationDate,CommandLine | Format-List
```

- Two+ entries with the **same token** = stacked orphans from earlier runs.

**C. Confirm the local origin is the thing that's dead.** While the phone
shows blank, check the app's port (replace `<PORT>`):

```powershell
Get-NetTCPConnection -LocalPort <PORT> -State Listen -ErrorAction SilentlyContinue
```

- cloudflared running **but** nothing listening on `<PORT>` = orphan serving
  502s. That is the bug.

To kill a confirmed orphan immediately: `Stop-Process -Id <PID> -Force`.

---

## Step 2: Find the bug in the app's source

Search the app's Python source for how it spawns / tears down cloudflared:

```
grep -nE "cloudflared|JobObject|AssignProcessToJobObject|KILL_ON_JOB_CLOSE|atexit" app.py
```

Three failure modes, worst to "least bad":

### Bug A: Job object with no ctypes `argtypes`/`restype`  ← the HealthTracker bug

If you see `CreateJobObjectW` / `AssignProcessToJobObject` /
`SetInformationJobObject` called via `ctypes` **without** preceding
`.argtypes = [...]` / `.restype = ...` declarations, that's the bug.

Why it fails: on 64-bit Python a Windows `HANDLE` is 64-bit, but an
undeclared ctypes function defaults its int args/return to **32-bit**
`c_int`. The handle is silently **truncated** → `AssignProcessToJobObject`
gets a garbage handle → returns `0` (failure) **with no exception**. The
child is never actually in the job, so `KILL_ON_JOB_CLOSE` never kills it.
The code *looks* correct and even "succeeds".

Tell-tale: `kernel32.AssignProcessToJobObject(job, int(proc._handle))` with
no argtypes nearby, and **no return-value check**.

### Bug B: no Job object at all, only `atexit` / `proc.terminate()`

`atexit` handlers and `try/finally` do **not** run when the process is
**force-killed** (Task Manager, `taskkill /F`) or crashes. Any hard exit
then orphans cloudflared. A Job object is the only thing that survives a
force-kill.

### Bug C: tunnel started but never tracked

`subprocess.Popen([... "cloudflared" ...])` with the handle thrown away,
nothing can ever stop it. Always orphans.

---

## Step 3: The fix (drop-in, Windows, stdlib only)

Two parts: (1) a Job object that **actually works**, (2) a startup
self-heal that cleans up orphans from prior runs.

### 3.1 Working Job object (`KILL_ON_JOB_CLOSE`)

```python
def job_kill_on_close(proc):
    """Put `proc` in a Windows Job with KILL_ON_JOB_CLOSE so it dies with us,
    even on a force-kill/crash. Return the job HANDLE, keep it alive for the
    whole app lifetime (let it be garbage-collected and the child dies early).
    """
    import sys
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
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

        class IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IOC),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # THE CRITICAL PART: without these declarations the HANDLE args are
        # truncated to 32 bits on 64-bit Python and the calls silently fail.
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL

        job = k32.CreateJobObjectW(None, None)
        if not job:
            print(f"[tunnel] CreateJobObject failed (err {ctypes.get_last_error()})", flush=True)
            return None
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            print(f"[tunnel] SetInformationJobObject failed (err {ctypes.get_last_error()})", flush=True)
            return None
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):
            print(f"[tunnel] AssignProcessToJobObject failed (err {ctypes.get_last_error()})", flush=True)
            return None
        return job
    except Exception as exc:
        print(f"[tunnel] job-object setup error: {exc}", flush=True)
        return None
```

Use it right after spawning, and **store the handle so it isn't GC'd**:

```python
proc = subprocess.Popen([cloudflared, "tunnel", "--no-autoupdate", "run",
                         "--token", token],
                        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                        creationflags=0x08000000)   # CREATE_NO_WINDOW
app_state.cf_proc = proc
app_state.cf_job  = job_kill_on_close(proc)        # keep handle alive!
```

Also keep `atexit`/explicit `proc.terminate()` for clean exits, belt and
suspenders. The Job object is what covers force-kill/crash.

### 3.2 Startup self-heal: kill orphans from prior runs

Even with 3.1 correct, clean up anything a *previous* (buggy) build left
behind. Match by **this app's token**, compared in Python, so other apps'
cloudflared tunnels (different tokens) are never touched:

```python
def kill_orphan_cloudflared(token):
    """taskkill any cloudflared.exe whose command line carries OUR token."""
    import sys, json, subprocess
    if not sys.platform.startswith("win") or not token:
        return 0
    CNW = 0x08000000  # CREATE_NO_WINDOW
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=6, creationflags=CNW)
        raw = (res.stdout or "").strip()
        if not raw:
            return 0
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        killed = 0
        for item in data:
            if token in (item.get("CommandLine") or ""):     # match in Python
                pid = int(item.get("ProcessId") or 0)
                if pid > 0:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, creationflags=CNW)
                    killed += 1
        return killed
    except Exception:
        return 0
```

Call it **before** spawning the new cloudflared. Never interpolate the token
into a shell command, read it from the app's config/`.env` and compare with
Python's `in`, as above.

---

## Step 4: Verify the fix (the force-kill test)

A graceful quit can't prove anything (atexit would clean up anyway). Prove
the **Job object** works by force-killing the app:

1. Start the app; confirm exactly one cloudflared is running (Step 1B).
2. Force-kill the app process, `taskkill /F /PID <app_pid>` (this bypasses
   `atexit`, so only the Job object can save you).
3. Within a few seconds, `Get-Process cloudflared` → **gone**.

If cloudflared survives a force-kill, the Job object is still not working,
re-check the `argtypes`/`restype` declarations and that the job HANDLE is
stored somewhere long-lived (not garbage-collected).

---

## Checklist summary

- [ ] App closed → no cloudflared left running.
- [ ] App running → exactly one cloudflared, one per app, correct token.
- [ ] Source: ctypes Job-object calls declare `argtypes`/`restype` and check
      return values.
- [ ] A startup self-heal kills orphans by **this app's** token.
- [ ] Force-kill test passes: kill the app hard → cloudflared dies in seconds.
- [ ] (If relevant) the cloudflared Public Hostname is **HTTP**, not HTTPS
      (HTTPS → 502 `tls: first record...`; different bug, see playbook §5).
