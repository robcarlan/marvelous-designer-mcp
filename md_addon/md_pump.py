"""Non-blocking MCP listener for Marvelous Designer, driven by a Win32 timer on MD's GUI thread.

The blocking listener (md_listener.py) freezes MD's window because MD's embedded Python never runs background threads.
This one never blocks: a message-only window gets a WM_TIMER every INTERVAL ms from MD's own event loop, and each tick
answers whatever requests are waiting (select() with no wait) and runs at most one queued job. So:

  * MD's GUI stays alive between calls (only a running call or job holds it).
  * Long work goes through the job queue: `job_submit` returns at once, `job_status` reports progress.
  * Modal dialogs: a dialog's own event loop keeps dispatching our timer, so a nested tick sees it. While a call or job
    is running, a modal dialog (MD's main window disabled, a new owned window up) is captured to a BMP, logged and, under
    the "dismiss" policy, closed after DIALOG_GRACE s (WM_CLOSE, then Esc, then Enter). Nested ticks also answer the safe
    methods (ping, status, dialogs, dismiss_dialogs, job_*), so a client can see what a stuck call is waiting on.

Start it with start() from MD's Python (Python Editor, a plug-in, or md_autostart.py at launch). Same wire protocol as
md_listener.py: one JSON object per line, one request per TCP connection, 127.0.0.1 only.
    Request:  {"id": str, "method": str, "params": dict}
    Response: {"id": str, "result": any}  or  {"id": str, "error": str}
"""
from __future__ import annotations

import contextlib
import ctypes
import io
import json
import os
import select
import socket
import struct
import time
import traceback
import uuid
from ctypes import wintypes

HOST = "127.0.0.1"
PORT = int(os.environ.get("MD_MCP_PORT", "7421"))
INTERVAL = int(os.environ.get("MD_MCP_INTERVAL_MS", "30"))
DIALOG_POLICY = os.environ.get("MD_MCP_DIALOGS", "dismiss")        # "dismiss" or "report"
DIALOG_GRACE = float(os.environ.get("MD_MCP_DIALOG_GRACE", "3"))
LOG_DIR = os.environ.get("MD_MCP_LOGDIR", os.path.expanduser(r"~\md_mcp"))
LOG = os.path.join(LOG_DIR, "pump.log")

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)
LRESULT, WPARAM, LPARAM = ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)
ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, LPARAM)
u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
u32.DefWindowProcW.restype = LRESULT
u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
u32.CreateWindowExW.restype = wintypes.HWND
u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                wintypes.HINSTANCE, wintypes.LPVOID]
u32.GetWindow.restype = wintypes.HWND
u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
u32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
u32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
k32.GetModuleHandleW.restype = wintypes.HMODULE
WM_TIMER, WM_CLOSE, WM_KEYDOWN, WM_KEYUP = 0x0113, 0x0010, 0x0100, 0x0101
VK_ESCAPE, VK_RETURN = 0x1B, 0x0D
HWND_MESSAGE = wintypes.HWND(-3)
TIMER_ID = 0x4D44


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


_state = {"hwnd": None, "srv": None, "started": None, "depth": 0, "busy": None, "ticks": 0, "served": 0,
          "main_hwnd": None, "stop": False}
_persistent_globals: dict = {"__name__": "__md_mcp__"}
_jobs: dict = {}
_queue: list = []
_dialogs: dict = {}      # hwnd -> record
_wndproc_ref = None      # keeps the ctypes callback alive


def log(msg: str) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- windows and dialogs

def _title(h) -> str:
    n = u32.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(h, b, n + 1)
    return b.value


def _cls(h) -> str:
    b = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(h, b, 256)
    return b.value


def _rect(h):
    r = wintypes.RECT()
    u32.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _windows() -> list:
    """Visible top-level windows of this process (on this thread's desktop)."""
    pid, out = os.getpid(), []

    def each(h, _):
        q = wintypes.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(q))
        if q.value == pid and u32.IsWindowVisible(h) and h != _state["hwnd"]:
            out.append(h)
        return True
    u32.EnumWindows(ENUMPROC(each), 0)
    return out


def _main_window():
    """MD's main window: the largest unowned visible window of this process."""
    best, area = None, 0
    for h in _windows():
        if u32.GetWindow(h, 4):     # GW_OWNER
            continue
        _, _, w, hh = _rect(h)
        if w * hh > area:
            best, area = h, w * hh
    if best and area > 400 * 300:
        _state["main_hwnd"] = best
    return _state["main_hwnd"]


def _is_dialog(h, main) -> bool:
    if h == main or not u32.IsWindowEnabled(h):
        return False
    c = _cls(h).lower()
    if any(s in c for s in ("popup", "tooltip", "shadow", "dropdown", "toolbar")):
        return False
    _, _, w, hh = _rect(h)
    return w >= 80 and hh >= 40


def capture(h, path: str) -> bool:
    """PrintWindow a window into a 24-bit BMP (works on a hidden desktop, where no one can see the dialog)."""
    _, _, w, hh = _rect(h)
    if w <= 0 or hh <= 0:
        return False
    sdc = u32.GetDC(None)
    mdc = g32.CreateCompatibleDC(sdc)
    bmp = g32.CreateCompatibleBitmap(sdc, w, hh)
    old = g32.SelectObject(mdc, bmp)
    try:
        u32.PrintWindow(h, mdc, 2)      # PW_RENDERFULLCONTENT
        stride = (w * 3 + 3) & ~3
        hdr = struct.pack("<IiiHHIIiiII", 40, w, hh, 1, 24, 0, stride * hh, 0, 0, 0, 0)
        bits = ctypes.create_string_buffer(stride * hh)
        bi = ctypes.create_string_buffer(hdr, 40 + 16)
        g32.GetDIBits(mdc, bmp, 0, hh, bits, bi, 0)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"BM" + struct.pack("<IHHI", 14 + 40 + stride * hh, 0, 0, 54) + hdr + bits.raw)
        return True
    except Exception:
        log("capture failed " + traceback.format_exc())
        return False
    finally:
        g32.SelectObject(mdc, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mdc)
        u32.ReleaseDC(None, sdc)


def scan_dialogs() -> list:
    """Every dialog-like window up now (new ones are captured and logged once)."""
    main = _main_window()
    now, live = time.time(), []
    for h in _windows():
        if not _is_dialog(h, main):
            continue
        key = int(h)
        d = _dialogs.get(key)
        if d is None or d.get("gone"):
            d = {"hwnd": key, "title": _title(h), "class": _cls(h), "rect": _rect(h), "seen": now, "actions": [],
                 "during": _state["busy"], "modal": bool(main and not u32.IsWindowEnabled(main))}
            d["image"] = os.path.join(LOG_DIR, "dialogs", "%s_%d.bmp" % (time.strftime("%H%M%S"), key))
            if not capture(h, d["image"]):
                d["image"] = None
            _dialogs[key] = d
            log("dialog up: %r class %s modal %s during %s image %s" % (d["title"], d["class"], d["modal"], d["during"],
                                                                         d["image"]))
        live.append(d)
    alive = {d["hwnd"] for d in live}
    for k, d in _dialogs.items():
        if k not in alive and not d.get("gone"):
            d["gone"] = now
    return live


def dismiss(d, force=False) -> None:
    """Close one dialog, escalating each try: WM_CLOSE, then Esc, then Enter (the default button)."""
    h = wintypes.HWND(d["hwnd"])
    step = len(d["actions"])
    if step == 0:
        u32.PostMessageW(h, WM_CLOSE, 0, 0); act = "close"
    elif step == 1:
        u32.PostMessageW(h, WM_KEYDOWN, VK_ESCAPE, 0x00010001); u32.PostMessageW(h, WM_KEYUP, VK_ESCAPE, 0xC0010001)
        act = "escape"
    else:
        u32.PostMessageW(h, WM_KEYDOWN, VK_RETURN, 0x001C0001); u32.PostMessageW(h, WM_KEYUP, VK_RETURN, 0xC01C0001)
        act = "enter"
    d["actions"].append([round(time.time() - d["seen"], 1), act])
    log("dialog %r: %s" % (d["title"], act))


def _watch_dialogs() -> None:
    """While a call or job runs, dismiss dialogs that block it (policy 'dismiss'), one escalation step per grace."""
    if DIALOG_POLICY != "dismiss":
        scan_dialogs(); return
    for d in scan_dialogs():
        due = d["seen"] + DIALOG_GRACE * (len(d["actions"]) + 1)
        if time.time() >= due and len(d["actions"]) < 6:
            dismiss(d)
            j = _jobs.get(_state["busy"] or "")
            if j is not None and d["hwnd"] not in j["dialogs"]:
                j["dialogs"].append(d["hwnd"])


def _public(d) -> dict:
    return {k: v for k, v in d.items() if k != "seen"} | {"age_s": round(time.time() - d["seen"], 1)}


# ---------------------------------------------------------------- handlers

def _exec(code: str, g: dict, out=None, err=None):
    out, err = out or io.StringIO(), err or io.StringIO()
    error = None
    g.pop("result", None)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exec(compile(code, g.get("__file__", "<mcp>"), "exec"), g)
    except BaseException as ex:     # SystemExit from a script must not end MD
        error = "".join(traceback.format_exception(type(ex), ex, ex.__traceback__))
    result = g.get("result")
    try:
        json.dumps(result)
    except (TypeError, ValueError):
        result = repr(result)
    return result, error


def h_ping(p):
    return {"pong": True, "pump": True, "pid": os.getpid()}


def h_status(p):
    main = _main_window()
    return {"pid": os.getpid(), "port": PORT, "up_s": round(time.time() - (_state["started"] or time.time()), 1),
            "ticks": _state["ticks"], "served": _state["served"], "busy": _state["busy"],
            "main_window": _title(main) if main else None,
            "main_enabled": bool(main and u32.IsWindowEnabled(main)),
            "queued": list(_queue), "dialogs": [_public(d) for d in scan_dialogs()], "log": LOG}


def h_execute_python(p):
    out, err = io.StringIO(), io.StringIO()
    _state["busy"] = "execute_python"
    try:
        result, error = _exec(p.get("code", ""), _persistent_globals, out, err)
    finally:
        _state["busy"] = None
    return {"stdout": out.getvalue(), "stderr": err.getvalue(), "result": result, "error": error}


def h_dialogs(p):
    return [_public(d) for d in scan_dialogs()]


def h_dismiss_dialogs(p):
    live = scan_dialogs()
    for d in live:
        if p.get("hwnd") in (None, d["hwnd"]):
            dismiss(d)
    return [_public(d) for d in live]


class _Tee(io.TextIOBase):
    def __init__(self, path):
        self.buf, self.path = io.StringIO(), path

    def write(self, s):
        self.buf.write(s)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(s)
        except Exception:
            pass
        return len(s)


def h_job_submit(p):
    """{"script": path} or {"code": str}; optional "name", "env" {k: v} (set before it runs), "args" (bound as `args`)."""
    if not p.get("script") and not p.get("code"):
        raise ValueError("job needs 'script' or 'code'")
    jid = (p.get("name") or "job") + "_" + uuid.uuid4().hex[:6]
    _jobs[jid] = {"id": jid, "state": "queued", "script": p.get("script"), "code": p.get("code"), "env": p.get("env") or {},
                  "args": p.get("args"), "queued": time.time(), "dialogs": [],
                  "log": os.path.join(LOG_DIR, "jobs", jid + ".log")}
    _queue.append(jid)
    log("job queued " + jid)
    return {"id": jid, "position": len(_queue)}


def _job_view(j, tail=4000):
    v = {k: j.get(k) for k in ("id", "state", "script", "result", "error", "log", "queued", "started", "ended")}
    v["dialogs"] = [_public(_dialogs[h]) for h in j["dialogs"] if h in _dialogs]
    if j.get("out") is not None:
        v["output_tail"] = j["out"].buf.getvalue()[-tail:]
    if j.get("started"):
        v["elapsed_s"] = round((j.get("ended") or time.time()) - j["started"], 1)
    return v


def h_job_status(p):
    j = _jobs.get(p.get("id", ""))
    if j is None:
        raise KeyError("no job " + str(p.get("id")))
    return _job_view(j, int(p.get("tail", 4000)))


def h_job_list(p):
    return [{"id": j["id"], "state": j["state"]} for j in _jobs.values()]


def h_job_cancel(p):
    jid = p.get("id", "")
    if jid in _queue:
        _queue.remove(jid); _jobs[jid]["state"] = "cancelled"
        return {"cancelled": True}
    return {"cancelled": False, "state": _jobs.get(jid, {}).get("state")}


def _run_job(jid):
    j = _jobs[jid]
    os.makedirs(os.path.dirname(j["log"]), exist_ok=True)
    j["state"], j["started"], j["out"] = "running", time.time(), _Tee(j["log"])
    _state["busy"] = jid
    log("job start " + jid)
    try:
        for k, v in j["env"].items():
            os.environ[str(k)] = str(v)
        if j["script"]:
            g = {"__name__": "__md_job__", "__file__": j["script"], "args": j["args"]}
            code = open(j["script"], "r", encoding="utf-8").read()
        else:
            g = dict(_persistent_globals, args=j["args"])
            code = j["code"]
        j["result"], j["error"] = _exec(code, g, j["out"], j["out"])
        j["state"] = "error" if j["error"] else "done"
    finally:
        _state["busy"] = None
        j["ended"] = time.time()
        log("job %s %s in %.1fs" % (jid, j["state"], j["ended"] - j["started"]))


def h_quit(p):
    """End Marvelous at once (no save prompt): after the reply is sent."""
    _state["quit"] = True
    return {"quitting": True}


SAFE = {"ping": h_ping, "status": h_status, "dialogs": h_dialogs, "dismiss_dialogs": h_dismiss_dialogs,
        "job_submit": h_job_submit, "job_status": h_job_status, "job_list": h_job_list, "job_cancel": h_job_cancel,
        "quit": h_quit}
HANDLERS = dict(SAFE, execute_python=h_execute_python)


# ---------------------------------------------------------------- the loop

def _reply(conn, obj):
    conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def _serve_one(conn, nested: bool):
    conn.settimeout(10)
    buf = bytearray()
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            return
        buf.extend(chunk)
    try:
        req = json.loads(bytes(buf).split(b"\n", 1)[0].decode("utf-8"))
    except ValueError as e:
        _reply(conn, {"id": None, "error": "bad json: %s" % e}); return
    rid, method = req.get("id"), req.get("method")
    if method == "shutdown":
        _reply(conn, {"id": rid, "result": {"bye": True}})
        _state["stop"] = True
        return
    table = SAFE if nested else HANDLERS
    fn = table.get(method)
    if fn is None:
        why = ("busy with %s; only %s answer until it ends" % (_state["busy"], ", ".join(sorted(SAFE)))
               if nested and method in HANDLERS else "unknown method: %s" % method)
        _reply(conn, {"id": rid, "error": why}); return
    try:
        resp = {"id": rid, "result": fn(req.get("params") or {})}
    except Exception:
        resp = {"id": rid, "error": traceback.format_exc()}
    _state["served"] += 1
    _reply(conn, resp)


def _serve_ready(nested: bool, limit=8):
    srv = _state["srv"]
    for _ in range(limit):
        if srv is None:
            return
        r, _, _ = select.select([srv], [], [], 0)
        if not r:
            return
        conn, _ = srv.accept()
        with conn:
            try:
                _serve_one(conn, nested)
            except Exception:
                log("serve error " + traceback.format_exc())
        if _state.get("quit"):
            log("quit requested")
            os._exit(0)
        if _state["stop"]:
            stop(); return


def _tick():
    _state["ticks"] += 1
    _state["depth"] += 1
    try:
        nested = _state["depth"] > 1 or _state["busy"] is not None
        if nested:
            _watch_dialogs()
        _serve_ready(nested)
        if not nested and _queue and _state["srv"] is not None:
            _run_job(_queue.pop(0))
    except Exception:
        log("tick error " + traceback.format_exc())
    finally:
        _state["depth"] -= 1


def _wndproc(h, msg, wp, lp):
    if msg == WM_TIMER and wp == TIMER_ID:
        _tick()
        return 0
    return u32.DefWindowProcW(h, msg, wp, lp)


def start(port: int | None = None) -> dict:
    """Bind the socket and arm the timer. Returns at once; MD stays responsive. Safe to call twice."""
    global _wndproc_ref, PORT
    if _state["srv"] is not None:
        return {"already": True, "port": PORT}
    PORT = int(port or PORT)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    srv.bind((HOST, PORT))
    srv.listen(8)
    srv.setblocking(False)
    if _state["hwnd"] is None:
        _wndproc_ref = WNDPROC(_wndproc)
        hinst = k32.GetModuleHandleW(None)
        wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), 0, _wndproc_ref, 0, 0, hinst, None, None, None, None,
                         "md_mcp_pump_%d" % os.getpid(), None)
        if not u32.RegisterClassExW(ctypes.byref(wc)):
            raise OSError("RegisterClassExW failed: %d" % ctypes.get_last_error())
        h = u32.CreateWindowExW(0, wc.lpszClassName, "md_mcp_pump", 0, 0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
        if not h:
            raise OSError("CreateWindowExW failed: %d" % ctypes.get_last_error())
        _state["hwnd"] = h
    if not u32.SetTimer(_state["hwnd"], TIMER_ID, INTERVAL, None):
        raise OSError("SetTimer failed: %d" % ctypes.get_last_error())
    _state.update(srv=srv, started=time.time(), stop=False)
    log("pump listening on %s:%d every %d ms (thread %d, pid %d)" % (HOST, PORT, INTERVAL, k32.GetCurrentThreadId(),
                                                                     os.getpid()))
    return {"port": PORT, "thread": k32.GetCurrentThreadId()}


def stop() -> None:
    """Stop listening (MD keeps running)."""
    if _state["hwnd"]:
        u32.KillTimer(_state["hwnd"], TIMER_ID)
    if _state["srv"] is not None:
        try:
            _state["srv"].close()
        except Exception:
            pass
    _state["srv"] = None
    log("pump stopped")
