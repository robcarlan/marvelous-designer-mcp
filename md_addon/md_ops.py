"""Typed Marvelous Designer operations for the MCP tools, run inside MD (import md_ops from execute_python or a job).

Each function returns a JSON-able dict. Option structs (ImportExportOption, ImportZPRJOption, ImportAlembicOption) live
in the ApiTypes module; `opts` overrides their fields by name and refuses a field that doesn't exist, listing the real
ones, so a typo never silently does nothing.

Found the hard way (studio COO-580 / the October lab, MD 2026.0):
  * ImportFBX reads a Blender FBX in cm as mm at scale 1.0: pass scale 10 for a cm FBX.
  * ImportFBX's animation flags are all off by default: bCreateAvatarJointAnimation=True to get the joint animation.
  * After ImportFBX the avatar is in the wrong pose until Simulate runs: pose_frame() sets the frame and Simulate(1).
  * Appending a .zprj loads its .anm and wipes the avatar's animation: import_garment() uses the .zpac stored inside it.
  * RunAnimationRecording records alone. SetAnimationRecording(True) first makes it record nothing.
  * ExportAlembic at scale 1.0 writes mm; 0.1 gives cm.
"""
import os
import time
import zipfile


def _types():
    import ApiTypes
    return ApiTypes


def opts(cls_name, over=None):
    o = getattr(_types(), cls_name)()
    fields = [d for d in dir(o) if not d.startswith("_")]
    for k, v in (over or {}).items():
        if k not in fields:
            raise KeyError("%s has no field %s; fields: %s" % (cls_name, k, ", ".join(fields)))
        setattr(o, k, v)
    return o


def describe(o):
    out = {}
    for d in dir(o):
        if d.startswith("_"):
            continue
        try:
            v = getattr(o, d)
        except Exception as ex:
            v = "<%r>" % ex
        if not callable(v):
            out[d] = v if isinstance(v, (int, float, str, bool, type(None))) else repr(v)
    return out


def avatars():
    import export_api
    return list(export_api.GetAvatarNameList())


def new_project():
    import utility_api
    utility_api.NewProject()
    return {"ok": True}


def import_avatar_fbx(path, scale=10.0, joint_animation=True, options=None):
    """An avatar (and its joint animation) from an FBX. scale 10 = a cm FBX."""
    import import_api
    before = avatars()
    o = opts("ImportExportOption", dict({"scale": float(scale), "bCreateAvatarJointAnimation": bool(joint_animation)},
                                        **(options or {})))
    r = import_api.ImportFBX(path, o)
    after = avatars()
    return {"ok": bool(r) or len(after) > len(before), "returned": repr(r), "avatars": after}


def import_avatar_alembic(path, unit=0.1, options=None):
    """A vertex-animated avatar from an Alembic (unit 0.1 = cm)."""
    import import_api
    o = opts("ImportAlembicOption", dict({"aUnit": float(unit), "boolAutoCreateFittingSuit": False,
                                          "boolAddArrangementPoints": True}, **(options or {})))
    r = import_api.ImportAlembic(path, o)
    return {"ok": bool(r), "avatars": avatars()}


def pose_frame(frame):
    """Put the avatar on a timeline frame. Only Simulate re-poses it after an import."""
    import utility_api
    utility_api.SetCurrentAnimationFrame(float(frame))
    utility_api.Simulate(1)
    return {"frame": utility_api.GetCurrentAnimationFrame()}


def import_garment(path, workdir=None, options=None):
    """A garment (.zpac, or the .zpac inside a .zprj) added to the scene without replacing the avatar or its animation."""
    import import_api
    keep = avatars()
    if path.lower().endswith(".zprj"):
        with zipfile.ZipFile(path) as z:
            inner = [n for n in z.namelist() if n.lower().endswith(".zpac")]
            if len(inner) != 1:
                raise RuntimeError("%s holds %d .zpac files, expected 1" % (path, len(inner)))
            workdir = workdir or os.path.join(os.path.dirname(path), "_md_work")
            os.makedirs(workdir, exist_ok=True)
            zp = os.path.join(workdir, os.path.basename(inner[0]))
            with open(zp, "wb") as f:
                f.write(z.read(inner[0]))
        path = zp
    r = import_api.ImportZpac(path, opts("ImportExportOption", options))
    now = avatars()
    return {"ok": bool(r), "garment": path, "avatars": now, "lost_avatars": [a for a in keep if a not in now]}


def open_project(path, options=None):
    """Open a .zprj as the whole scene (its avatar, garment and animation)."""
    import import_api
    o = opts("ImportZPRJOption", dict({"bAppend": False, "bLoadAvatar": True, "bLoadGarment": True,
                                       "bLoadSceneAndProps": False}, **(options or {})))
    return {"ok": bool(import_api.ImportZprj(path, o)), "avatars": avatars()}


def record(start, end, substeps=5, chunk=0):
    """Simulate and record the cloth over the avatar's animation, frames start..end."""
    import utility_api
    utility_api.SetSimulationNumberOfSimulation(int(substeps))
    t, a, s, e = time.time(), float(start), float(start), float(end)
    while a < e:
        b = e if chunk <= 0 else min(e, a + chunk)
        utility_api.RunAnimationRecording(a, b)
        a = b
    return {"frames": [s, e], "seconds": round(time.time() - t, 1)}


def export_alembic(path, garment=True, avatar=False, scale=0.1, options=None):
    """The recorded cloth as an Alembic (scale 0.1 = cm)."""
    import export_api
    os.makedirs(os.path.dirname(path), exist_ok=True)
    o = opts("ImportExportOption", dict({"bExportGarment": bool(garment), "bExportAvatar": bool(avatar),
                                         "scale": float(scale)}, **(options or {})))
    r = export_api.ExportAlembic(path, o)
    return {"ok": os.path.isfile(path), "returned": repr(r), "path": path,
            "bytes": os.path.getsize(path) if os.path.isfile(path) else 0}


def export_obj(path, garment=True, avatar=False, scale=0.1, frame=None, options=None):
    """The garment (and/or avatar) as an OBJ at the current frame, or at `frame` (set first)."""
    import export_api, utility_api
    if frame is not None:
        utility_api.SetCurrentAnimationFrame(float(frame))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    o = opts("ImportExportOption", dict({"bExportGarment": bool(garment), "bExportAvatar": bool(avatar),
                                         "scale": float(scale)}, **(options or {})))
    r = export_api.ExportOBJ(path, o)
    return {"ok": os.path.isfile(path), "returned": repr(r), "path": path}


def save_project(path):
    import export_api
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return {"returned": repr(export_api.ExportZPrj(path)), "ok": os.path.isfile(path), "path": path}
