"""Start md_pump inside Marvelous Designer at launch, with no click.

scripts/install_autostart.py puts one line in MD's Python (site-packages/md_mcp_autostart.pth). Python runs it while MD
starts its interpreter. The line does nothing unless MD was started with MD_MCP_AUTOSTART=1 in its environment, so a
person's own Marvelous window never gets a listener; a launcher (e.g. a hidden-desktop runner) sets it, plus
MD_MCP_ADDON (this folder) and optionally MD_MCP_PORT.

The pump binds at once and is served by MD's own event loop from then on (see md_pump.py), so nothing blocks start-up.
Everything is logged to %USERPROFILE%/md_mcp/pump.log (MD_MCP_LOGDIR).
"""
import os
import sys
import traceback


def run():
    if os.environ.get("MD_MCP_AUTOSTART") != "1":
        return
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import md_pump
    try:
        md_pump.log("autostart in pid %d, argv %r" % (os.getpid(), sys.argv))
        md_pump.start()
    except Exception:
        md_pump.log("autostart failed\n" + traceback.format_exc())


run()
