#!/usr/bin/env python3
"""Build the desktop app with PyInstaller: dist/BeeTracker.exe on Windows, dist/BeeTracker.dmg on macOS.

  pip install pyinstaller numpy              (and nidaqmx on Windows, so the .exe can drive the DAQ)
  python3 tools/package.py

PyInstaller can't cross-build, so make the .exe on Windows and the .dmg on a Mac
(.github/workflows/desktop.yml does both on GitHub).
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
dist, work = root / "dist", root / "build"
subprocess.run([sys.executable, str(root / "tools" / "build.py")], check=True)

args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "BeeTracker",
        "--paths", str(root / "helper"), "--add-data", f"{root / 'bee-tracker.html'}{os.pathsep}.",
        "--distpath", str(dist), "--workpath", str(work), "--specpath", str(work)]
if sys.platform == "win32":
    args += ["--onefile", "--collect-all", "nidaqmx"]
elif sys.platform == "darwin":
    args += ["--osx-bundle-identifier", "org.beetracker.app"]
subprocess.run(args + [str(root / "app" / "bee_tracker_app.py")], check=True)

if sys.platform == "darwin":
    dmg = dist / "BeeTracker.dmg"
    with tempfile.TemporaryDirectory() as tmp:          # the app plus an Applications link to drag it onto
        shutil.copytree(dist / "BeeTracker.app", Path(tmp) / "Bee Tracker.app", symlinks=True)
        os.symlink("/Applications", Path(tmp) / "Applications")
        dmg.unlink(missing_ok=True)
        subprocess.run(["hdiutil", "create", "-volname", "Bee Tracker", "-srcfolder", tmp, "-format", "UDZO", str(dmg)], check=True)
    print(f"wrote {dmg.relative_to(root)}")
elif sys.platform == "win32":
    print(f"wrote {(dist / 'BeeTracker.exe').relative_to(root)}")
