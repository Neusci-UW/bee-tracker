#!/usr/bin/env python3
"""Bee Tracker desktop app: the page and the LED helper in one program, with nothing to install.

It runs the LED helper, serves bee-tracker.html from it (http://127.0.0.1:8765/), and opens the
page in a Chrome or Edge window that is already connected to the helper. A small window shows
the DAQ, the code, and buttons to reopen the page, turn the LEDs off, and quit.

The LEDs need NI's NI-DAQmx driver (ni.com) and an NI USB-6003. Without them, or on a Mac
(NI-DAQmx has no macOS driver), the helper runs in simulated mode: tracking works, no LEDs light.

Run from the repository:  python3 app/bee_tracker_app.py
Build the .exe or .dmg:   python3 tools/package.py
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import font, messagebox

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))   # the bundle, or the repository
sys.path.insert(0, str(ROOT / "helper"))
import led_helper as L  # noqa: E402

PORTS = range(8765, 8775)
MAX_VOLTS = 5.0


def open_output() -> tuple[L._Output, str]:
    """The NI DAQ if there is one (a USB-6003 first), otherwise simulated output, with a note why."""
    if sys.platform == "darwin":
        return L.SimulatedOutput(), "Simulated LEDs: NI-DAQmx has no driver for macOS, so a Mac can't drive the DAQ."
    try:
        import nidaqmx.system
        devices = list(nidaqmx.system.System.local().devices)
    except Exception as e:
        return L.SimulatedOutput(), f"Simulated LEDs: the NI-DAQmx driver isn't installed (get it from ni.com). ({e})"
    if not devices:
        return L.SimulatedOutput(), "Simulated LEDs: no NI device found. Plug in the DAQ and restart Bee Tracker."
    dev = next((d for d in devices if "6003" in d.product_type), devices[0])
    try:
        return L.NidaqOutput(dev.name), ""
    except Exception as e:
        return L.SimulatedOutput(), f"Simulated LEDs: couldn't open {dev.name}: {e}"


def running_copy(port: int) -> bool:
    """Is Bee Tracker (or the LED helper) already answering on this port?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hello", timeout=1) as r:
            return json.loads(r.read()).get("app") == "bee-led-helper"
    except Exception:
        return False


def browser() -> str | None:
    """Chrome or Edge, which the page needs (camera, frame timing, file saving)."""
    if sys.platform == "win32":
        cands = [os.path.expandvars(p) for p in (
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")]
    elif sys.platform == "darwin":
        cands = [str(base / app / "Contents" / "MacOS" / app.removesuffix(".app"))
                 for base in (Path("/Applications"), Path.home() / "Applications")
                 for app in ("Google Chrome.app", "Microsoft Edge.app", "Chromium.app")]
    else:
        cands = [shutil.which(n) or "" for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")]
    return next((c for c in cands if c and Path(c).is_file()), None)


def open_page(url: str) -> bool:
    """Open the page in its own Chrome/Edge window; False if neither is installed (then the default browser)."""
    exe = browser()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    webbrowser.open(url)
    return False


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        out, note = open_output()
        page = (ROOT / "bee-tracker.html").read_bytes()
        self.helper = L.Helper(out, MAX_VOLTS, 0.0, page=page)
        for port in PORTS:
            if running_copy(port):          # opened twice: show the copy that's running
                open_page(f"http://127.0.0.1:{port}/")
                raise SystemExit(0)
            try:
                self.srv = self.helper.serve(port)
                break
            except OSError:
                continue
        else:
            raise RuntimeError(f"Ports {PORTS.start}–{PORTS.stop - 1} are all in use.")
        out.zero()
        atexit.register(out.stop)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True, name="helper").start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/"

        root.title("Bee Tracker")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        pad = {"padx": 16, "pady": 4}
        d = out.describe()
        tk.Label(root, text="Bee Tracker is running", font=font.Font(size=15, weight="bold")).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(root, text=(f"LEDs: {d['device']} ({d['product']}), ao0 and ao1 from 0 to {MAX_VOLTS:g} V." if not out.simulated else note),
                 wraplength=380, justify="left", fg="#a05a00" if out.simulated else "#1d6b2f").pack(anchor="w", **pad)
        tk.Label(root, text=f"Page: {self.url}    LED code: {self.helper.code}", fg="#555").pack(anchor="w", **pad)
        self.msg = tk.Label(root, text="", fg="#555", wraplength=380, justify="left")
        self.msg.pack(anchor="w", **pad)
        row = tk.Frame(root)
        row.pack(anchor="w", padx=12, pady=(6, 14))
        tk.Button(row, text="Open Bee Tracker", command=self.open).pack(side="left", padx=4)
        tk.Button(row, text="LEDs off", command=self.leds_off).pack(side="left", padx=4)
        tk.Button(row, text="Quit", command=self.quit).pack(side="left", padx=4)
        tk.Label(root, text="Keep this window open while you use the page. Quitting turns both LEDs off.",
                 fg="#555", wraplength=380, justify="left").pack(anchor="w", padx=16, pady=(0, 14))
        self.open()

    def open(self):
        # The code goes in the address so the page connects without typing it.
        if not open_page(f"{self.url}#led-code={self.helper.code}"):
            self.msg.config(text="Chrome or Edge wasn't found, so the page opened in your default browser. "
                                 "Use Chrome or Edge for cameras and saving files.")

    def leds_off(self):
        self.helper.out.stop()
        self.msg.config(text=self.helper.out.error or "Both LEDs are off.")

    def quit(self):
        if self.helper.out.busy and not messagebox.askokcancel("Bee Tracker", "An LED protocol is playing. Stop it and quit?"):
            return
        self.helper.out.stop()
        threading.Thread(target=self.srv.shutdown, daemon=True).start()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        App(root)
    except SystemExit:
        root.destroy()
        return 0
    except Exception as e:
        root.withdraw()
        messagebox.showerror("Bee Tracker", f"Bee Tracker couldn't start: {e}")
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
