# Self-test for md_pump outside MD: a Win32 message loop stands in for MD's Qt loop.
# Run: python -I tests/pump_selftest.py <repo>/md_addon <log folder>   (Windows; no MD needed)
import ctypes, json, os, socket, sys, threading, time, uuid
from ctypes import wintypes
sys.path.insert(0, sys.argv[1])
os.environ["MD_MCP_PORT"] = "7499"; os.environ["MD_MCP_LOGDIR"] = sys.argv[2]; os.environ["MD_MCP_DIALOG_GRACE"] = "1"
import md_pump
md_pump.start()
u32 = ctypes.windll.user32
res = {}
def call(m, p=None, to=30):
    with socket.create_connection(("127.0.0.1", 7499), timeout=to) as s:
        s.sendall((json.dumps({"id": "x", "method": m, "params": p or {}}) + "\n").encode())
        b = b""
        while b"\n" not in b: b += s.recv(65536)
    return json.loads(b)
def client():
    try:
        res["ping"] = call("ping")
        res["exec"] = call("execute_python", {"code": "x = 2\nresult = x * 21"})
        res["persist"] = call("execute_python", {"code": "result = x"})
        res["sysexit"] = call("execute_python", {"code": "raise SystemExit(3)"})["result"]["error"] is not None
        j = call("job_submit", {"code": "import time\nprint('working')\nt=time.time()\nwhile time.time()-t<2: pass\nresult='ok'", "name": "spin"})["result"]["id"]
        time.sleep(0.5)
        res["during_job_exec"] = call("execute_python", {"code": "result=1"}).get("error", "")[:40]
        res["during_job_status"] = call("job_status", {"id": j})["result"]["state"]
        while call("job_status", {"id": j})["result"]["state"] in ("queued", "running"): time.sleep(0.2)
        res["job"] = call("job_status", {"id": j})["result"]
        # a modal dialog inside a job: the watcher must close it
        j2 = call("job_submit", {"code": "import ctypes\nr = ctypes.windll.user32.MessageBoxW(None, 'blocking?', 'Save changes', 0x21)\nresult = r", "name": "modal"})["result"]["id"]
        t = time.time()
        while call("job_status", {"id": j2})["result"]["state"] in ("queued", "running") and time.time() - t < 30: time.sleep(0.3)
        res["modal"] = call("job_status", {"id": j2})["result"]
        res["quit"] = call("shutdown")
    except Exception as e:
        res["exc"] = repr(e)
    finally:
        res["end"] = True
threading.Thread(target=client, daemon=True).start()
msg = wintypes.MSG(); t = time.time()
while not res.get("end") and time.time() - t < 60:
    if u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
        u32.TranslateMessage(ctypes.byref(msg)); u32.DispatchMessageW(ctypes.byref(msg))
    else: time.sleep(0.005)
print(json.dumps(res, indent=1, default=str)[:4000])
