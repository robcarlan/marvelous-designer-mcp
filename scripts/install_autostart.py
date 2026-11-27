"""Install (or remove) the launch hook that starts md_pump inside Marvelous Designer with no click.

    python scripts/install_autostart.py [--pyhome <MD python311 folder>] [--uninstall]

Writes one .pth file into MD's own Python (default C:/Users/Public/Documents/MarvelousDesigner/Configuration/python311,
site-packages is that folder's Lib/site-packages). Python executes a .pth line that starts with "import" when the
interpreter starts. The line is inert unless MD runs with MD_MCP_AUTOSTART=1 and MD_MCP_ADDON=<this repo>/md_addon, so a
normal Marvelous launch is untouched. Then launch MD with those two variables (scripts/md_launch.py does).
"""
import argparse
import os
import sys

PTH = "md_mcp_autostart.pth"
LINE = ("import os, sys; _a = os.environ.get('MD_MCP_ADDON') if os.environ.get('MD_MCP_AUTOSTART') == '1' else None; "
        "_a and (sys.path.insert(0, _a) or __import__('md_autostart'))\n")


def site_packages(pyhome):
    for p in (os.path.join(pyhome, "Lib", "site-packages"), os.path.join(pyhome, "site-packages")):
        if os.path.isdir(p):
            return p
    raise SystemExit("no site-packages under " + pyhome)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pyhome", default="C:/Users/Public/Documents/MarvelousDesigner/Configuration/python311")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    path = os.path.join(site_packages(a.pyhome), PTH)
    if a.uninstall:
        if os.path.exists(path):
            os.remove(path)
        print("removed " + path)
        return
    old = open(path).read() if os.path.exists(path) else None
    if old != LINE:
        with open(path, "w") as f:
            f.write(LINE)
    print(("unchanged " if old == LINE else "wrote ") + path)
    print("launch MD with MD_MCP_AUTOSTART=1 MD_MCP_ADDON=" +
          os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "md_addon")))


if __name__ == "__main__":
    sys.exit(main())
