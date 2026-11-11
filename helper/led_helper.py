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
    py helper\\led_helper.py                     (Dev1, at most 10 V)
    py helper\\led_helper.py --device Dev2 --max-volts 4
    py helper\\led_helper.py --simulated         (no DAQ: try the page without hardware)
It prints the address and a 6-digit code to type into the page.

Safety:
  * Levels above --max-volts (default 10 V, the USB-6003's range) or below 0 V are refused (a single LED can be
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
  POST /api/run   {"steps": [{"dur": s, "ao0": V or {"fn": ..., ...}, "ao1": ...}, ...], "rate": S/s}
                  levels are volts, or functions of time: see FUNCTIONS
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

API = 2                        # 2: levels can be functions of time
OUTPUTS = ("ao0", "ao1")
DEFAULT_RATE = 1000.0          # output updates per second, per channel
MAX_RATE = 2500.0              # well under the USB-6003's 5 kS/s per output; 1 ms steps are plenty for LEDs
MAX_SECONDS = 4 * 3600
MAX_STEPS = 5000
MAX_BAD_CODES = 10
_LOCAL_ORIGIN = re.compile(r"^(null|https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?)$", re.I)


class ProtocolError(ValueError):
    pass


# ---------------------------------------------------------------- functions of time
# Each step (segment) gives each output either a level in volts or a function: {"fn": name, ...params},
# with t = seconds into the step. src/core.js (LED_FNS, ledEval) defines the same functions for the
# page's preview and log: change both together.
FUNCTIONS = {
    "off": (), "const": ("v",), "ramp": ("from", "to"), "exp": ("from", "to", "tau"),
    "sine": ("freq", "mid", "amp", "phase"), "pulses": ("freq", "duty", "on", "off"),
    "pulse": ("delay", "width", "v"), "random": ("min", "max", "hold", "seed"), "formula": ("expr",),
}
EDGE = 1e-9       # slack so an edge that falls on a sample time (0.2 + 0.1 s) lands the same way every time


def _round(x):
    return np.floor(x + 0.5)


_FORMULA_FNS = {"sin": (1, np.sin), "cos": (1, np.cos), "tan": (1, np.tan), "abs": (1, np.abs), "sqrt": (1, np.sqrt),
                "exp": (1, np.exp), "log": (1, np.log), "floor": (1, np.floor), "ceil": (1, np.ceil), "round": (1, _round),
                "min": (2, np.minimum), "max": (2, np.maximum), "mod": (2, lambda a, b: a - b * np.floor(a / b)),
                "clamp": (3, lambda x, a, b: np.minimum(np.maximum(x, a), b))}
_TOKEN = re.compile(r"\s*(?:(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)|([A-Za-z_]\w*)|(<=|>=|[-+*/^(),<>]))")


def parse_formula(src):
    """A formula -> f(t array, d) -> array. Same grammar and messages as parseFormula in src/core.js."""
    src = "" if src is None else str(src)
    if not src.strip():
        raise ProtocolError("The formula is empty.")
    if len(src) > 300:
        raise ProtocolError("The formula is longer than 300 characters.")
    toks, at = [], 0
    while at < len(src):
        m = _TOKEN.match(src, at)
        if not m:
            if not src[at:].strip():
                break
            raise ProtocolError(f"The formula has a character it can’t use: “{src[at:].strip()[0]}”.")
        at = m.end()
        toks.append(("n", float(m[1])) if m[1] is not None else ("id", m[2].lower()) if m[2] is not None else ("op", m[3]))
    pos = [0]

    def is_op(o):
        return pos[0] < len(toks) and toks[pos[0]] == ("op", o)

    def need(o):
        if not is_op(o):
            raise ProtocolError(f"The formula is missing “{o}”.")
        pos[0] += 1

    def cmp():
        a = add()
        if pos[0] < len(toks) and toks[pos[0]][0] == "op" and toks[pos[0]][1] in ("<", "<=", ">", ">="):
            o = toks[pos[0]][1]
            pos[0] += 1
            b = add()
            op = {"<": np.less, "<=": np.less_equal, ">": np.greater, ">=": np.greater_equal}[o]
            return lambda t, d: op(a(t, d), b(t, d)).astype(float)
        return a

    def add():
        a = mul()
        while is_op("+") or is_op("-"):
            o = toks[pos[0]][1]
            pos[0] += 1
            x, b = a, mul()
            a = (lambda x, b: lambda t, d: x(t, d) + b(t, d))(x, b) if o == "+" else (lambda x, b: lambda t, d: x(t, d) - b(t, d))(x, b)
        return a

    def mul():
        a = unary()
        while is_op("*") or is_op("/"):
            o = toks[pos[0]][1]
            pos[0] += 1
            x, b = a, unary()
            a = (lambda x, b: lambda t, d: x(t, d) * b(t, d))(x, b) if o == "*" else (lambda x, b: lambda t, d: x(t, d) / b(t, d))(x, b)
        return a

    def unary():
        if is_op("-"):
            pos[0] += 1
            a = unary()
            return lambda t, d: -a(t, d)
        if is_op("+"):
            pos[0] += 1
            return unary()
        a = atom()
        if is_op("^"):
            pos[0] += 1
            b = unary()
            return lambda t, d: np.power(a(t, d), b(t, d))
        return a

    def atom():
        if pos[0] >= len(toks):
            raise ProtocolError("The formula ends too early.")
        kind, v = toks[pos[0]]
        pos[0] += 1
        if kind == "n":
            return lambda t, d: np.full_like(t, v)
        if (kind, v) == ("op", "("):
            a = cmp()
            need(")")
            return a
        if kind == "id":
            consts = {"pi": math.pi, "e": math.e}
            if v == "t":
                return lambda t, d: t
            if v == "d":
                return lambda t, d: np.full_like(t, d)
            if v in consts:
                c = consts[v]
                return lambda t, d: np.full_like(t, c)
            if v not in _FORMULA_FNS:
                raise ProtocolError(f"The formula uses “{v}”, which it doesn’t know. Use t, d, pi, e, or a function such as sin(…).")
            n, fn = _FORMULA_FNS[v]
            need("(")
            args = [cmp()]
            while is_op(","):
                pos[0] += 1
                args.append(cmp())
            need(")")
            if len(args) != n:
                raise ProtocolError(f"{v}(…) takes {n} value{'' if n == 1 else 's'}.")
            return lambda t, d: fn(*(g(t, d) for g in args))
        raise ProtocolError(f"The formula has “{v}” where a number or name should be.")

    f = cmp()
    if pos[0] < len(toks):
        raise ProtocolError(f"The formula has something extra at “{toks[pos[0]][1]:g}”." if toks[pos[0]][0] == "n"
                            else f"The formula has something extra at “{toks[pos[0]][1]}”.")
    return f


def hash01(seed: int, k: np.ndarray) -> np.ndarray:
    """Uniform 0..1 per (seed, k); bit-for-bit the same as hash01 in src/core.js."""
    with np.errstate(over="ignore"):
        k = np.asarray(k, dtype=np.int64).astype(np.uint32)
        s = np.full_like(k, np.uint32(seed & 0xFFFFFFFF) ^ np.uint32(0x9E3779B9))
        h = (s * np.uint32(0x85EBCA6B)) ^ ((k + np.uint32(1)) * np.uint32(0xC2B2AE35))
        h ^= h >> np.uint32(16)
        h *= np.uint32(0x85EBCA6B)
        h ^= h >> np.uint32(13)
        h *= np.uint32(0xC2B2AE35)
        h ^= h >> np.uint32(16)
    return h.astype(np.float64) / 4294967296.0


def _num(f, k, what):
    x = f.get(k)
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise ProtocolError(f"{what}: “{k}” must be a number.")
    return float(x)


def output_values(f, t: np.ndarray, d: float, what: str) -> np.ndarray:
    """Levels for one output over one step: f is a number (volts), None, or {"fn": name, ...}."""
    if f is None or isinstance(f, (int, float)) and not isinstance(f, bool):
        v = float(f or 0.0)
        if not math.isfinite(v):
            raise ProtocolError(f"{what}: the level must be a number.")
        return np.full_like(t, v)
    if not isinstance(f, dict) or f.get("fn") not in FUNCTIONS:
        raise ProtocolError(f"{what}: unknown function {f.get('fn') if isinstance(f, dict) else f!r}.")
    fn = f["fn"]
    if fn == "formula":
        try:
            g = parse_formula(f.get("expr"))
        except ProtocolError as e:
            raise ProtocolError(f"{what}: {e}") from None
        with np.errstate(all="ignore"):
            return np.broadcast_to(np.asarray(g(t, d), dtype=float), t.shape).copy()
    p = {k: _num(f, k, what) for k in FUNCTIONS[fn]}
    with np.errstate(all="ignore"):
        if fn == "off":
            return np.zeros_like(t)
        if fn == "const":
            return np.full_like(t, p["v"])
        if fn == "ramp":
            return p["from"] + (p["to"] - p["from"]) * t / d
        if fn == "exp":
            if p["tau"] <= 0:
                raise ProtocolError(f"{what}: τ must be more than 0 s.")
            return p["to"] + (p["from"] - p["to"]) * np.exp(-t / p["tau"])
        if fn == "sine":
            if p["freq"] < 0:
                raise ProtocolError(f"{what}: the frequency can’t be negative.")
            return p["mid"] + p["amp"] * np.sin(2 * np.pi * p["freq"] * t + p["phase"] * np.pi / 180)
        if fn == "pulses":
            if p["freq"] <= 0 or not 0 < p["duty"] < 100:
                raise ProtocolError(f"{what}: pulses need a rate over 0 Hz and a duty cycle between 0 and 100%.")
            x = t * p["freq"]
            return np.where(x - np.floor(x) < p["duty"] / 100 - EDGE, p["on"], p["off"])
        if fn == "pulse":
            if p["delay"] < 0 or p["width"] <= 0:
                raise ProtocolError(f"{what}: the pulse needs a delay of 0 s or more and a width over 0 s.")
            return np.where((t >= p["delay"] - EDGE) & (t < p["delay"] + p["width"] - EDGE), p["v"], 0.0)
        if fn == "random":
            if p["hold"] < 0.001 or not (p["seed"].is_integer() and 0 <= p["seed"] < 2 ** 31):
                raise ProtocolError(f"{what}: random levels need a hold of at least 0.001 s and a whole-number seed.")
            return p["min"] + (p["max"] - p["min"]) * hash01(int(p["seed"]), np.floor(t / p["hold"] + EDGE))
    raise AssertionError(fn)


def build_wave(steps, rate: float, max_v: float, min_v: float = 0.0):
    """Steps -> (2 x n array of volts for ao0/ao1, ending with one sample at 0 V, step start times).

    Each step lasts floor(dur * rate + 0.5) samples (at least one), so start times are exact
    multiples of 1/rate; the returned starts are the times actually used. Each output's level is
    a number of volts or a function of time (see FUNCTIONS)."""
    if not isinstance(steps, list) or not steps:
        raise ProtocolError("The protocol has no steps.")
    if len(steps) > MAX_STEPS:
        raise ProtocolError(f"At most {MAX_STEPS} steps.")
    if not (isinstance(rate, (int, float)) and 1 <= rate <= MAX_RATE):
        raise ProtocolError(f"Rate must be between 1 and {MAX_RATE:g} updates per second.")
    counts, durs = [], []
    for i, s in enumerate(steps, 1):
        try:
            dur = float(s.get("dur"))
        except (TypeError, ValueError, AttributeError):
            raise ProtocolError(f"Step {i}: duration and levels must be numbers.") from None
        if not math.isfinite(dur) or dur <= 0:
            raise ProtocolError(f"Step {i}: the duration must be more than 0 s.")
        if any(not (s.get(o) is None or isinstance(s.get(o), (int, float, dict)) and not isinstance(s.get(o), bool)) for o in OUTPUTS):
            raise ProtocolError(f"Step {i}: duration and levels must be numbers.")
        counts.append(max(1, int(math.floor(dur * rate + 0.5))))
        durs.append(dur)
    n = sum(counts)
    if n / rate > MAX_SECONDS:
        raise ProtocolError(f"The protocol is longer than {MAX_SECONDS // 3600} hours.")
    wave = np.zeros((2, n + 1))
    starts, k = [], 0
    for i, (s, c, dur) in enumerate(zip(steps, counts, durs), 1):
        starts.append(k / rate)
        t = np.arange(c) / rate
        for ch, o in enumerate(OUTPUTS):
            v = output_values(s.get(o), t, dur, f"Step {i}, {o}")
            bad = ~np.isfinite(v)
            if bad.any():
                raise ProtocolError(f"Step {i}: {o}'s formula gives no number at t = {t[bad.argmax()]:g} s.")
            lo, hi = float(v.min()), float(v.max())
            for x in (hi, lo):
                if x > max_v + 1e-9 or x < min_v - 1e-9:
                    raise ProtocolError(f"Step {i}: {o} = {x:g} V is outside the allowed {min_v:g} to {max_v:g} V.")
            wave[ch, k:k + c] = v
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
        self.note = ""                    # why the output is simulated, when the desktop app knows
        self.code = code or f"{secrets.randbelow(10 ** 6):06d}"
        self.bad_codes = 0
        self.server: ThreadingHTTPServer | None = None

    def status(self) -> dict:
        r = self.out.run
        return {"app": "bee-led-helper", "api": API, "time": time.time(), **self.out.describe(),
                "outputs": list(OUTPUTS), "max_v": self.max_v, "min_v": self.min_v, "max_rate": MAX_RATE,
                "running": self.out.busy, "run": r, "error": self.out.error, "note": self.note}

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
    ap.add_argument("--max-volts", type=float, default=10.0, help="highest level allowed on either output (default 10 V)")
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
