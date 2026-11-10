#!/usr/bin/env python3
"""Bee Tracker LED helper: drives two LEDs from the analog outputs ao0 and ao1 of an
NI USB-6003, for the Bee Tracker page.

A web page can't use NI's driver, so this small program runs next to the page on the
lab computer. The page sends it an LED protocol (steps: how long, and the voltage on
ao0 and ao1), and it plays the whole protocol as one hardware-timed output task, so
step changes are timed by the DAQ's clock, not by Windows.

Setup on the lab computer (once):
    1. NI-DAQmx driver from ni.com (the DAQ must show up in NI MAX, e.g. as Dev1)
    2. py -m pip install nidaqmx numpy
Run, and keep the window open while you use the page:
    py helper\\led_helper.py                     (Dev1, at most 5 V)
    py helper\\led_helper.py --device Dev2 --max-volts 4
    py helper\\led_helper.py --simulated         (no DAQ: try the page without hardware)
It prints the address and a 6-digit code to type into the page.

Safety:
  * Levels above --max-volts (default 5 V) or below 0 V are refused (a single LED can be
    damaged by a negative voltage); --allow-negative lifts the 0 V floor.
  * Every protocol ends at 0 V. Stop, errors, Ctrl+C and quitting set both outputs to 0 V.
    If the program is killed mid-protocol (e.g. the window is closed with its X on
    Windows), the outputs may stay at the last level: run it again, or unplug the DAQ.
  * Only this computer can connect (127.0.0.1), only from pages opened as a file or from
    localhost, and only with the code. After 10 wrong codes it stops accepting requests.

API (JSON over HTTP):
  GET  /                      the Bee Tracker page, when the desktop app serves it
  GET  /api/hello             {"app": "bee-led-helper", "api": 1, ...}       (no code needed)
  GET  /api/status            device, limits, whether a protocol is running, this computer's clock
  POST /api/run   {"steps": [{"dur": s, "ao0": V, "ao1": V}, ...], "rate": S/s}
                  -> {"ok", "t_start" (time.time() when the output task started), "starts", "duration"}
  POST /api/stop              stop now; both outputs to 0 V
"""
from __future__ import annotations

import argparse
import atexit
import hmac
import json
import math
import re
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import numpy as np

API = 1
OUTPUTS = ("ao0", "ao1")
DEFAULT_RATE = 1000.0          # output updates per second, per channel
MAX_RATE = 2500.0              # well under the USB-6003's 5 kS/s per output; 1 ms steps are plenty for LEDs
MAX_SECONDS = 4 * 3600
MAX_STEPS = 5000
MAX_BAD_CODES = 10
_LOCAL_ORIGIN = re.compile(r"^(null|https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?)$", re.I)


class ProtocolError(ValueError):
    pass


def build_wave(steps, rate: float, max_v: float, min_v: float = 0.0):
    """Steps -> (2 x n array of volts for ao0/ao1, ending with one sample at 0 V, step start times).

    Each step lasts round(dur * rate) samples (at least one), so start times are exact
    multiples of 1/rate; the returned starts are the times actually used."""
    if not isinstance(steps, list) or not steps:
        raise ProtocolError("The protocol has no steps.")
    if len(steps) > MAX_STEPS:
        raise ProtocolError(f"At most {MAX_STEPS} steps.")
    if not (isinstance(rate, (int, float)) and 1 <= rate <= MAX_RATE):
        raise ProtocolError(f"Rate must be between 1 and {MAX_RATE:g} updates per second.")
    counts, levels = [], []
    for i, s in enumerate(steps, 1):
        try:
            dur = float(s.get("dur"))
            v = [float(s.get(o, 0.0) or 0.0) for o in OUTPUTS]
        except (TypeError, ValueError, AttributeError):
            raise ProtocolError(f"Step {i}: duration and levels must be numbers.") from None
        if not math.isfinite(dur) or dur <= 0:
            raise ProtocolError(f"Step {i}: the duration must be more than 0 s.")
        for o, x in zip(OUTPUTS, v):
            if not math.isfinite(x) or x > max_v + 1e-9 or x < min_v - 1e-9:
                raise ProtocolError(f"Step {i}: {o} = {x:g} V is outside the allowed {min_v:g} to {max_v:g} V.")
        counts.append(max(1, int(round(dur * rate))))
        levels.append(v)
    n = sum(counts)
    if n / rate > MAX_SECONDS:
        raise ProtocolError(f"The protocol is longer than {MAX_SECONDS // 3600} hours.")
    wave = np.zeros((2, n + 1))
    starts, k = [], 0
    for c, v in zip(counts, levels):
        starts.append(k / rate)
        wave[:, k:k + c] = np.array(v)[:, None]
        k += c
    return wave, starts


# ---------------------------------------------------------------- outputs
class _Output:
    """Plays one protocol at a time on a worker thread."""
    simulated = False

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.run: dict | None = None          # {"id", "t_start", "duration"} of the current or last protocol
        self.error = ""

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, wave: np.ndarray, rate: float) -> float:
        self.stop()
        with self._lock:
            self.error = ""
            self._stop.clear()
            t0 = self._begin(wave, rate)
            self.run = {"id": (self.run or {}).get("id", 0) + 1, "t_start": t0, "duration": wave.shape[1] / rate}
            self._thread = threading.Thread(target=self._watch, args=(wave.shape[1] / rate,), daemon=True, name="led-output")
            self._thread.start()
        return t0

    def stop(self) -> bool:
        was = self.busy
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        with self._lock:
            self._end()
            try:
                self.zero()
            except Exception as e:                 # reported; never blocks stopping
                self.error = f"Couldn't set the outputs to 0 V: {e}"
        return was

    def _watch(self, seconds: float) -> None:
        try:
            deadline = time.monotonic() + seconds + 5.0
            while not self._stop.is_set():
                if self._done():
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("The protocol didn't finish in time.")
                time.sleep(0.01)
        except Exception as e:
            self.error = str(e)
        finally:
            if not self._stop.is_set():
                with self._lock:
                    self._end()
                    try:
                        self.zero()
                    except Exception as e:
                        self.error = self.error or f"Couldn't set the outputs to 0 V: {e}"

    # backends fill these in
    def _begin(self, wave, rate) -> float: raise NotImplementedError
    def _done(self) -> bool: raise NotImplementedError
    def _end(self) -> None: ...
    def zero(self) -> None: ...
    def describe(self) -> dict: return {}


class NidaqOutput(_Output):
    """Both outputs in one hardware-timed, finite task with NI's nidaqmx package."""

    def __init__(self, device: str):
        super().__init__()
        import nidaqmx                                 # noqa: F401  (fails early if not installed)
        import nidaqmx.system
        self.device = device
        names = {d.name: d for d in nidaqmx.system.System.local().devices}
        if device not in names:
            found = ", ".join(f"{n} ({d.product_type})" for n, d in names.items()) or "none"
            raise RuntimeError(f"No NI device called {device}. Devices found: {found}. Use --device.")
        self.product = names[device].product_type
        self._task = None

    def _chans(self, task):
        task.ao_channels.add_ao_voltage_chan(f"{self.device}/ao0:1", min_val=-10.0, max_val=10.0)

    def _begin(self, wave, rate):
        import nidaqmx
        from nidaqmx.constants import AcquisitionType
        task = nidaqmx.Task()
        try:
            self._chans(task)
            task.timing.cfg_samp_clk_timing(rate=rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=wave.shape[1])
            task.write(np.ascontiguousarray(wave), auto_start=False)
            task.start()
            t0 = time.time()
        except Exception:
            task.close()
            raise
        self._task = task
        return t0

    def _done(self):
        with self._lock:
            return self._task is None or self._task.is_task_done()

    def _end(self):
        task, self._task = self._task, None
        if task is not None:
            try:
                task.stop()
            finally:
                task.close()

    def zero(self):
        import nidaqmx
        with nidaqmx.Task() as task:
            self._chans(task)
            task.write([0.0, 0.0], auto_start=True)

    def describe(self):
        return {"device": self.device, "product": self.product, "simulated": False}


class SimulatedOutput(_Output):
    """No hardware: keeps time and remembers the levels, for trying the page and for tests."""
    simulated = True

    def __init__(self):
        super().__init__()
        self._end_at = 0.0
        self.levels = [0.0, 0.0]
        self.played: list[tuple[float, np.ndarray, float]] = []

    def _begin(self, wave, rate):
        t0 = time.time()
        self._end_at = time.monotonic() + wave.shape[1] / rate
        self.played.append((t0, wave, rate))
        return t0

    def _done(self):
        return time.monotonic() >= self._end_at

    def _end(self):
        self._end_at = 0.0

    def zero(self):
        self.levels = [0.0, 0.0]

    def describe(self):
        return {"device": "simulated", "product": "no DAQ", "simulated": True}


# ---------------------------------------------------------------- HTTP
class Helper:
    def __init__(self, output: _Output, max_v: float, min_v: float, code: str | None = None, page: bytes | None = None):
        self.out, self.max_v, self.min_v = output, max_v, min_v
        self.page = page                  # the Bee Tracker page, served at / by the desktop app
        self.code = code or f"{secrets.randbelow(10 ** 6):06d}"
        self.bad_codes = 0
        self.server: ThreadingHTTPServer | None = None

    def status(self) -> dict:
        r = self.out.run
        return {"app": "bee-led-helper", "api": API, "time": time.time(), **self.out.describe(),
                "outputs": list(OUTPUTS), "max_v": self.max_v, "min_v": self.min_v, "max_rate": MAX_RATE,
                "running": self.out.busy, "run": r, "error": self.out.error}

    def run_protocol(self, body: dict) -> tuple[int, dict]:
        rate = float(body.get("rate", DEFAULT_RATE) or DEFAULT_RATE)
        try:
            wave, starts = build_wave(body.get("steps"), rate, self.max_v, self.min_v)
        except ProtocolError as e:
            return 400, {"ok": False, "message": str(e)}
        try:
            t0 = self.out.start(wave, rate)
        except Exception as e:
            try:
                self.out.stop()
            except Exception:
                pass
            return 500, {"ok": False, "message": f"The DAQ refused the protocol: {e}"}
        return 200, {"ok": True, "t_start": t0, "starts": starts, "duration": wave.shape[1] / rate, "rate": rate,
                     "run": self.out.run["id"]}

    def serve(self, port: int) -> ThreadingHTTPServer:
        srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        srv.daemon_threads = True
        srv.helper = self
        self.server = srv
        return srv


class _Handler(BaseHTTPRequestHandler):
    server_version = "bee-led-helper"
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True       # send replies at once (otherwise ~40 ms late), for a tight clock check

    def log_message(self, *a):
        pass

    @property
    def helper(self) -> Helper:
        return self.server.helper

    def _origin_ok(self):
        o = self.headers.get("Origin")
        return o is None or bool(_LOCAL_ORIGIN.match(o))

    def _cors(self):
        o = self.headers.get("Origin")
        if o and _LOCAL_ORIGIN.match(o):
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "X-Led-Code, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")     # browsers skip the pre-check for 10 min

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self, need_code=True):
        h = self.helper
        if not self._origin_ok():
            self._reply(403, {"error": "Only pages on this computer can use the LED helper."})
            return False
        if need_code:
            if h.bad_codes >= MAX_BAD_CODES:
                self._reply(403, {"error": "Too many wrong codes: restart the LED helper to get a new code."})
                return False
            if not hmac.compare_digest(self.headers.get("X-Led-Code", "").encode(), h.code.encode()):
                h.bad_codes += 1
                if h.bad_codes == MAX_BAD_CODES:
                    print(f"\n{MAX_BAD_CODES} wrong codes: no longer accepting requests. Restart to get a new code.", flush=True)
                self._reply(401, {"error": "Wrong code. Use the code shown in the LED helper window."})
                return False
        return True

    def do_OPTIONS(self):
        self.send_response(204 if self._origin_ok() else 403)
        if self._origin_ok():
            self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/bee-tracker.html") and self.helper.page is not None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(self.helper.page)))
                self.end_headers()
                self.wfile.write(self.helper.page)
            elif path == "/api/hello":
                if self._allowed(need_code=False):
                    self._reply(200, {"app": "bee-led-helper", "api": API, **self.helper.out.describe()})
            elif path == "/api/status":
                if self._allowed():
                    self._reply(200, self.helper.status())
            else:
                self._reply(404, {"error": "Unknown address."})
        except Exception as e:
            self._reply(500, {"error": str(e)})

    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 4 * 1024 * 1024)) if n else b""
        try:
            if path == "/api/run":
                if self._allowed():
                    try:
                        body = json.loads(raw or b"{}")
                    except ValueError:
                        self._reply(400, {"ok": False, "message": "The protocol wasn't valid JSON."})
                        return
                    self._reply(*self.helper.run_protocol(body))
            elif path == "/api/stop":
                if self._allowed():
                    was = self.helper.out.stop()
                    self._reply(200, {"ok": True, "was_running": was, "time": time.time(), "error": self.helper.out.error})
            else:
                self._reply(404, {"error": "Unknown address."})
        except Exception as e:
            self._reply(500, {"error": str(e)})


def main(argv=None):
    ap = argparse.ArgumentParser(description="Drive two LEDs on an NI USB-6003 (ao0, ao1) for the Bee Tracker page.")
    ap.add_argument("--device", default="Dev1", help="NI device name, as in NI MAX (default Dev1)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--max-volts", type=float, default=5.0, help="highest level allowed on either output (default 5 V)")
    ap.add_argument("--allow-negative", action="store_true", help="allow levels down to -max-volts (default: 0 V floor)")
    ap.add_argument("--code", help="a fixed 6-digit code instead of a new one each time")
    ap.add_argument("--simulated", action="store_true", help="no DAQ: keep time only, for trying the page")
    args = ap.parse_args(argv)
    if not 0 < args.max_volts <= 10:
        ap.error("--max-volts must be between 0 and 10 (the USB-6003's range).")
    if args.code is not None and not re.fullmatch(r"\d{6}", args.code):
        ap.error("--code must be 6 digits.")
    try:
        out = SimulatedOutput() if args.simulated else NidaqOutput(args.device)
    except ImportError:
        print("The nidaqmx package isn't installed. Run:  py -m pip install nidaqmx numpy\n"
              "(and install the NI-DAQmx driver from ni.com), or use --simulated to try without a DAQ.", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Couldn't open the DAQ: {e}", file=sys.stderr)
        return 2
    out.zero()
    atexit.register(out.stop)
    helper = Helper(out, args.max_volts, -args.max_volts if args.allow_negative else 0.0, args.code)
    try:
        srv = helper.serve(args.port)
    except OSError as e:
        print(f"Port {args.port} is in use ({e}). Is the LED helper already running? Use --port to pick another.", file=sys.stderr)
        return 2

    def quit_(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()
    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), quit_)
    d = out.describe()
    print(f"LED helper ready: {d['device']} ({d['product']}), ao0 and ao1 from {helper.min_v:g} to {helper.max_v:g} V.\n"
          f"In the Bee Tracker page, under LEDs:  address http://127.0.0.1:{args.port}   code {helper.code}\n"
          "Keep this window open while you use the page. Ctrl+C stops it and sets both outputs to 0 V.", flush=True)
    try:
        srv.serve_forever(poll_interval=0.2)
    finally:
        out.stop()
        srv.server_close()
        print("LED helper stopped; outputs at 0 V.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
