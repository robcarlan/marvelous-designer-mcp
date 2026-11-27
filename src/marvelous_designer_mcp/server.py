from mcp.server.fastmcp import FastMCP

from . import bridge
from .config import MD_HOST, MD_PORT

mcp = FastMCP("marvelous-designer")


def _md_exec(code: str) -> dict:
    """Run `code` inside MD via execute_python and flatten the listener's envelope.

    The listener returns {stdout, stderr, result, error}; this returns
    {"ok": True, "result": ...} or {"ok": False, "error": ...} (plus "stdout" if any).
    """
    try:
        resp = bridge.call("execute_python", {"code": code})
    except bridge.BridgeError as e:
        return {"ok": False, "error": f"bridge: {e}"}
    if not isinstance(resp, dict):
        return {"ok": True, "result": resp}
    out: dict = {"ok": False, "error": resp["error"]} if resp.get("error") else {"ok": True, "result": resp.get("result")}
    if resp.get("stdout"):
        out["stdout"] = resp["stdout"]
    return out


@mcp.tool()
def ping() -> dict:
    """Verify the MD listener is reachable. Returns whatever the listener echoes back."""
    try:
        return {"ok": True, "host": MD_HOST, "port": MD_PORT, "result": bridge.call("ping")}
    except bridge.BridgeError as e:
        return {"ok": False, "host": MD_HOST, "port": MD_PORT, "error": str(e)}


@mcp.tool()
def execute_python(code: str) -> dict:
    """Execute arbitrary Python inside Marvelous Designer's interpreter.

    MD's API is exposed as importable modules (import_api, export_api, fabric_api,
    pattern_api, utility_api, ...), NOT as globals — so `import` what you need.
    Bind the value you want back to a name called `result`. The API is pybind11-based,
    so calling a function with wrong args raises a TypeError that lists the accepted
    signatures — handy for discovery.

    Returns: {"stdout": str, "stderr": str, "result": any, "error": str|None}.
    """
    try:
        return bridge.call("execute_python", {"code": code})
    except bridge.BridgeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def shutdown_listener() -> dict:
    """Stop the MD-side listener's blocking loop and release the Marvelous Designer GUI."""
    try:
        return {"ok": True, "result": bridge.call("shutdown")}
    except bridge.BridgeError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def scene_info() -> dict:
    """Summary of the current MD scene: project name/path, MD version, pattern & fabric counts."""
    return _md_exec(
        "import utility_api, pattern_api, fabric_api\n"
        "result = {\n"
        "    'project_name': utility_api.GetProjectName(),\n"
        "    'project_path': utility_api.GetProjectFilePath(),\n"
        "    'md_version': [utility_api.GetMajorVersion(), utility_api.GetMinorVersion(), utility_api.GetPatchVersion()],\n"
        "    'pattern_count': pattern_api.GetPatternCount(),\n"
        "    'fabric_count': fabric_api.GetFabricCount(True),\n"
        "    'fabric_styles': fabric_api.GetFabricStyleNameList(),\n"
        "}\n"
    )


@mcp.tool()
def list_patterns() -> dict:
    """List pattern pieces in the current scene: index, name, assigned fabric index."""
    return _md_exec(
        "import pattern_api\n"
        "result = [\n"
        "    {'index': i, 'name': pattern_api.GetPatternPieceName(i), 'fabric_index': pattern_api.GetPatternPieceFabricIndex(i)}\n"
        "    for i in range(pattern_api.GetPatternCount())\n"
        "]\n"
    )


@mcp.tool()
def list_fabrics() -> dict:
    """List fabrics in the current scene: index and name (plus the fabric-style name list)."""
    return _md_exec(
        "import fabric_api\n"
        "result = {\n"
        "    'fabrics': [{'index': i, 'name': fabric_api.GetFabricName(i)} for i in range(fabric_api.GetFabricCount(True))],\n"
        "    'styles': fabric_api.GetFabricStyleNameList(),\n"
        "}\n"
    )


@mcp.tool()
def assign_fabric(fabric_index: int, pattern_index: int, face: int = 2) -> dict:
    """Assign a fabric to a pattern piece via fabric_api.AssignFabricToPattern(fabric, pattern, face).

    `face` is the MD third int argument (commonly 0=front, 1=back, 2=both); default 2.
    On a signature mismatch the raw TypeError is returned so the real meaning can be found.
    """
    code = (
        "import fabric_api\n"
        f"fi, pi, fc = {int(fabric_index)}, {int(pattern_index)}, {int(face)}\n"
        "try:\n"
        "    result = {'ok': bool(fabric_api.AssignFabricToPattern(fi, pi, fc))}\n"
        "except TypeError as e:\n"
        "    result = {'ok': False, 'signature_error': str(e)}\n"
    )
    return _md_exec(code)


@mcp.tool()
def import_project(path: str) -> dict:
    """Open an MD project / garment / mesh file (.zprj, .zpac, .obj, .fbx, ...) by absolute path.

    Uses the generic import_api.ImportFile (dispatches by extension); the *W variant
    handles non-ASCII Windows paths. Returns which call ran and its bool result.

    Caveat: if MD raises a modal dialog (e.g. "save current project?"), the listener
    deadlocks because the GUI thread is stuck in our accept loop — close MD to recover.
    """
    code = (
        "import import_api\n"
        f"path = {path!r}\n"
        "result = None\n"
        "for name in ('ImportFileW', 'ImportFile'):\n"
        "    fn = getattr(import_api, name, None)\n"
        "    if fn is None:\n"
        "        continue\n"
        "    try:\n"
        "        result = {'ok': bool(fn(path)), 'used': name, 'path': path}\n"
        "        break\n"
        "    except TypeError as e:\n"
        "        result = {'ok': False, 'signature_error': str(e), 'tried': name}\n"
        "if result is None:\n"
        "    result = {'ok': False, 'error': 'no ImportFile variant in import_api', 'path': path}\n"
    )
    return _md_exec(code)


@mcp.tool()
def export_project(path: str) -> dict:
    """Save the current scene as a .zprj project file at the given absolute path.

    Uses export_api.ExportZPrjW(path, False) (the bool is the MD second arg; False to
    avoid any dialog), falling back to ExportZPrj(path). Returns the path string MD
    reports, or the raw signature error if the call shape was wrong.
    """
    code = (
        "import export_api\n"
        f"path = {path!r}\n"
        "result = None\n"
        "fnW = getattr(export_api, 'ExportZPrjW', None)\n"
        "if fnW is not None:\n"
        "    try:\n"
        "        result = {'ok': True, 'used': 'ExportZPrjW', 'returned': fnW(path, False)}\n"
        "    except TypeError as e:\n"
        "        result = {'ok': False, 'signature_error': str(e), 'tried': 'ExportZPrjW'}\n"
        "if result is None or not result.get('ok'):\n"
        "    fn = getattr(export_api, 'ExportZPrj', None)\n"
        "    if fn is not None:\n"
        "        try:\n"
        "            result = {'ok': True, 'used': 'ExportZPrj', 'returned': fn(path)}\n"
        "        except TypeError as e:\n"
        "            result = {'ok': False, 'signature_error': str(e), 'tried': 'ExportZPrj'}\n"
        "if result is None:\n"
        "    result = {'ok': False, 'error': 'no ExportZPrj variant in export_api', 'path': path}\n"
    )
    return _md_exec(code)


@mcp.tool()
def simulate(steps: int = 1) -> dict:
    """Run cloth simulation via utility_api.Simulate(int).

    `steps` is the int the MD API expects (its exact meaning — frame/step count or a
    mode — is version-dependent; 1 is a reasonable default). Returns the bool MD reports.
    """
    code = (
        "import utility_api\n"
        f"steps = {int(steps)}\n"
        "try:\n"
        "    result = {'ok': True, 'returned': utility_api.Simulate(steps), 'steps': steps}\n"
        "except TypeError as e:\n"
        "    result = {'ok': False, 'signature_error': str(e)}\n"
    )
    return _md_exec(code)


@mcp.tool()
def md_api(module: str, contains: str = "") -> dict:
    """List the functions of an MD API module (import_api, export_api, fabric_api, pattern_api,
    utility_api, ...). `contains` filters names by case-insensitive substring.

    To learn a function's signature, call it via execute_python with wrong/no args — the
    TypeError lists the accepted argument types.
    """
    code = (
        "import importlib\n"
        f"mod = importlib.import_module({module!r})\n"
        f"sub = {contains.lower()!r}\n"
        "names = [n for n in dir(mod) if not n.startswith('_')]\n"
        "if sub:\n"
        "    names = [n for n in names if sub in n.lower()]\n"
        "result = sorted(names)\n"
    )
    return _md_exec(code)


# ---------------------------------------------------------------- md_pump (non-blocking listener) tools
# These need md_addon/md_pump.py running in MD (scripts/install_autostart.py + MD_MCP_AUTOSTART=1 at launch, or
# md_pump.start() from the Python Editor). The blocking md_listener.py answers only ping/execute_python/shutdown.

def _call(method: str, params: dict | None = None) -> dict:
    try:
        return {"ok": True, "result": bridge.call(method, params or {})}
    except bridge.BridgeError as e:
        return {"ok": False, "error": str(e)}
    except OSError as e:
        return {"ok": False, "error": f"listener not reachable on {MD_HOST}:{MD_PORT}: {e}"}


def _op(fn: str, **kw) -> dict:
    """Run md_ops.<fn>(**kw) inside MD."""
    return _md_exec(f"import md_ops\nresult = md_ops.{fn}(**{kw!r})\n")


@mcp.tool()
def status() -> dict:
    """Pump status: busy call or job, the job queue, MD's main window, and any dialog up now (with a BMP capture)."""
    return _call("status")


@mcp.tool()
def dialogs() -> dict:
    """Dialog windows MD has up now: title, class, whether modal, a BMP capture path, and what was done to each."""
    return _call("dialogs")


@mcp.tool()
def dismiss_dialogs(hwnd: int | None = None) -> dict:
    """Close MD's dialogs (or one, by hwnd), one escalation step per call: WM_CLOSE, then Esc, then Enter."""
    return _call("dismiss_dialogs", {"hwnd": hwnd})


@mcp.tool()
def job_submit(script: str = "", code: str = "", name: str = "job", env: dict | None = None, args: object = None) -> dict:
    """Queue long work (a Python file by absolute path, or code) to run inside MD between requests. Returns its id at
    once; poll job_status. `env` is set in MD's environment before it runs; `args` is bound as the global `args`."""
    return _call("job_submit", {"script": script or None, "code": code or None, "name": name, "env": env or {}, "args": args})


@mcp.tool()
def job_status(id: str, tail: int = 4000) -> dict:
    """A job's state (queued, running, done, error), result, error, elapsed time, output tail and the dialogs it hit."""
    return _call("job_status", {"id": id, "tail": tail})


@mcp.tool()
def job_list() -> dict:
    """Every job this MD session has seen, with its state."""
    return _call("job_list")


@mcp.tool()
def job_cancel(id: str) -> dict:
    """Drop a queued job (a running one can't be stopped)."""
    return _call("job_cancel", {"id": id})


@mcp.tool()
def quit_md() -> dict:
    """End Marvelous Designer at once, with no save prompt."""
    return _call("quit")


@mcp.tool()
def new_project() -> dict:
    """Empty the scene (utility_api.NewProject)."""
    return _op("new_project")


@mcp.tool()
def import_avatar_fbx(path: str, scale: float = 10.0, joint_animation: bool = True, options: dict | None = None) -> dict:
    """Import an avatar with its joint animation from an FBX. scale 10 for a cm FBX (MD reads it as mm at 1.0).
    `options` overrides ImportExportOption fields by name. Then pose_frame(start) before a garment goes in."""
    return _op("import_avatar_fbx", path=path, scale=scale, joint_animation=joint_animation, options=options)


@mcp.tool()
def import_avatar_alembic(path: str, unit: float = 0.1, options: dict | None = None) -> dict:
    """Import a vertex-animated avatar from an Alembic (unit 0.1 = cm)."""
    return _op("import_avatar_alembic", path=path, unit=unit, options=options)


@mcp.tool()
def pose_frame(frame: float) -> dict:
    """Set the timeline frame and Simulate(1): the only way to re-pose the avatar after an import."""
    return _op("pose_frame", frame=frame)


@mcp.tool()
def import_garment(path: str, workdir: str = "", options: dict | None = None) -> dict:
    """Add a garment (.zpac, or the .zpac inside a .zprj) without replacing the avatar or wiping its animation."""
    return _op("import_garment", path=path, workdir=workdir or None, options=options)


@mcp.tool()
def open_project(path: str, options: dict | None = None) -> dict:
    """Open a .zprj as the whole scene: its avatar, garment and animation."""
    return _op("open_project", path=path, options=options)


@mcp.tool()
def record_animation(start: float, end: float, substeps: int = 5, chunk: int = 0) -> dict:
    """Simulate and record the cloth over frames start..end (RunAnimationRecording). Long: prefer job_submit for a
    long clip, e.g. code='import md_ops; result = md_ops.record(1, 190)'."""
    return _op("record", start=start, end=end, substeps=substeps, chunk=chunk)


@mcp.tool()
def export_alembic(path: str, garment: bool = True, avatar: bool = False, scale: float = 0.1, options: dict | None = None) -> dict:
    """Export the recorded cloth as an Alembic (scale 0.1 = cm; garment only by default)."""
    return _op("export_alembic", path=path, garment=garment, avatar=avatar, scale=scale, options=options)


@mcp.tool()
def export_obj(path: str, garment: bool = True, avatar: bool = False, scale: float = 0.1, frame: float | None = None,
               options: dict | None = None) -> dict:
    """Export the garment (and/or avatar) as an OBJ, at `frame` if given."""
    return _op("export_obj", path=path, garment=garment, avatar=avatar, scale=scale, frame=frame, options=options)
