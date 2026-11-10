"""Tests for helper/led_helper.py. Run: python3 -m pytest tests/test_led_helper.py

No DAQ is needed: the NI path is checked against a stand-in `nidaqmx` that records every call.
"""
import json
import shutil
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "helper"))
import led_helper as L  # noqa: E402


# ---------------------------------------------------------------- waveform
def test_steps_become_one_two_channel_wave_ending_at_zero():
    wave, starts = L.build_wave([{"dur": 1, "ao0": 2, "ao1": 0}, {"dur": 0.5, "ao0": 0, "ao1": 3.5}, {"dur": 2}], 100, 5)
    assert wave.shape == (2, 351)
    assert starts == [0, 1.0, 1.5]
    assert np.all(wave[0, :100] == 2) and np.all(wave[1, :100] == 0)
    assert np.all(wave[0, 100:150] == 0) and np.all(wave[1, 100:150] == 3.5)
    assert np.all(wave[:, 150:] == 0)                       # a step with no levels is off, and the end is 0 V


def test_durations_round_to_whole_samples():
    wave, starts = L.build_wave([{"dur": 0.0004, "ao0": 1}, {"dur": 0.0126, "ao0": 2}], 1000, 5)
    assert starts == [0, 0.001] and wave.shape[1] == 1 + 13 + 1


@pytest.mark.parametrize("steps, msg", [
    ([], "no steps"),
    ([{"dur": 0, "ao0": 1}], "more than 0"),
    ([{"dur": 1, "ao0": 6}], "outside"),
    ([{"dur": 1, "ao1": -0.5}], "outside"),
    ([{"dur": "x"}], "numbers"),
    ([{"dur": float("nan")}], "more than 0"),
    ([{"dur": 5 * 3600}], "longer"),
])
def test_bad_protocols_are_refused_with_a_reason(steps, msg):
    with pytest.raises(L.ProtocolError, match=msg):
        L.build_wave(steps, 1000, 5)


def test_negative_levels_only_when_allowed():
    wave, _ = L.build_wave([{"dur": 1, "ao0": -2}], 10, 5, min_v=-5)
    assert wave[0, 0] == -2


def test_rate_limits():
    with pytest.raises(L.ProtocolError, match="Rate"):
        L.build_wave([{"dur": 1}], 5000, 5)


# ---------------------------------------------------------------- NI path, with a stand-in nidaqmx
class FakeTask:
    log: list = []
    done_after = 0.0

    def __init__(self):
        self.chans, self.timing_args, self.started_at = [], None, None
        self.ao_channels = types.SimpleNamespace(add_ao_voltage_chan=lambda name, **kw: self.chans.append((name, kw)))
        self.timing = types.SimpleNamespace(cfg_samp_clk_timing=lambda **kw: setattr(self, "timing_args", kw))
        FakeTask.log.append(self)
        self.writes, self.closed, self.stopped = [], False, False

    def write(self, data, auto_start=False):
        self.writes.append((np.array(data, float), auto_start))

    def start(self):
        self.started_at = time.monotonic()

    def is_task_done(self):
        return time.monotonic() - self.started_at >= FakeTask.done_after

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@pytest.fixture
def fake_nidaqmx(monkeypatch):
    FakeTask.log = []
    mod = types.ModuleType("nidaqmx")
    mod.Task = FakeTask
    system = types.ModuleType("nidaqmx.system")
    dev = types.SimpleNamespace(name="Dev1", product_type="USB-6003")
    system.System = types.SimpleNamespace(local=lambda: types.SimpleNamespace(devices=[dev]))
    consts = types.ModuleType("nidaqmx.constants")
    consts.AcquisitionType = types.SimpleNamespace(FINITE="FINITE")
    mod.system, mod.constants = system, consts
    monkeypatch.setitem(sys.modules, "nidaqmx", mod)
    monkeypatch.setitem(sys.modules, "nidaqmx.system", system)
    monkeypatch.setitem(sys.modules, "nidaqmx.constants", consts)
    return FakeTask


def test_ni_protocol_is_one_finite_task_on_both_outputs(fake_nidaqmx):
    out = L.NidaqOutput("Dev1")
    assert out.describe()["product"] == "USB-6003"
    wave, _ = L.build_wave([{"dur": 0.05, "ao0": 2, "ao1": 1}], 1000, 5)
    fake_nidaqmx.done_after = 0.1
    t0 = out.start(wave, 1000)
    assert abs(t0 - time.time()) < 1
    task = fake_nidaqmx.log[-1]
    assert task.chans == [("Dev1/ao0:1", {"min_val": -10.0, "max_val": 10.0})]
    assert task.timing_args == {"rate": 1000, "sample_mode": "FINITE", "samps_per_chan": 51}
    (data, auto), = task.writes
    assert data.shape == (2, 51) and not auto and data[0, 0] == 2 and data[1, 0] == 1 and np.all(data[:, -1] == 0)
    assert out.busy
    time.sleep(0.3)
    assert not out.busy and task.closed                    # finished by itself
    zero = fake_nidaqmx.log[-1]                            # then both outputs written to 0 V
    assert zero is not task and list(zero.writes[-1][0]) == [0, 0] and zero.closed


def test_ni_stop_closes_the_task_and_zeroes_both_outputs(fake_nidaqmx):
    out = L.NidaqOutput("Dev1")
    fake_nidaqmx.done_after = 60
    wave, _ = L.build_wave([{"dur": 30, "ao0": 3}], 100, 5)
    out.start(wave, 100)
    task = fake_nidaqmx.log[-1]
    assert out.stop() is True
    assert task.stopped and task.closed and not out.busy
    zero = fake_nidaqmx.log[-1]
    assert zero.chans[0][0] == "Dev1/ao0:1" and zero.writes[-1][1] is True and list(zero.writes[-1][0]) == [0, 0]


def test_ni_missing_device_names_what_was_found(fake_nidaqmx):
    with pytest.raises(RuntimeError, match=r"No NI device called Dev3.*Dev1 \(USB-6003\)"):
        L.NidaqOutput("Dev3")


# ---------------------------------------------------------------- over HTTP (simulated output)
@pytest.fixture
def helper():
    out = L.SimulatedOutput()
    h = L.Helper(out, 5.0, 0.0, code="123456")
    srv = h.serve(0)
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    th.start()
    h.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield h
    out.stop()
    srv.shutdown()
    srv.server_close()


def call(h, path, code="123456", body=None, origin="null"):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(h.url + path, data=data, method="POST" if data is not None or path.endswith(("run", "stop")) else "GET")
    if code is not None:
        req.add_header("X-Led-Code", code)
    if origin:
        req.add_header("Origin", origin)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


def test_hello_is_open_status_needs_the_code(helper):
    st, body, hdr = call(helper, "/api/hello", code=None)
    assert st == 200 and body["app"] == "bee-led-helper" and body["simulated"]
    assert hdr["Access-Control-Allow-Origin"] == "null"
    assert call(helper, "/api/status", code="000000")[0] == 401
    st, body, _ = call(helper, "/api/status")
    assert st == 200 and body["outputs"] == ["ao0", "ao1"] and body["max_v"] == 5 and body["min_v"] == 0
    assert abs(body["time"] - time.time()) < 1


def test_other_websites_are_refused(helper):
    assert call(helper, "/api/status", origin="https://evil.example")[0] == 403
    assert call(helper, "/api/status", origin="http://localhost:8000")[0] == 200


def test_run_plays_the_protocol_then_stops_by_itself(helper):
    st, body, _ = call(helper, "/api/run", body={"steps": [{"dur": 0.3, "ao0": 2}, {"dur": 0.2, "ao1": 1}], "rate": 100})
    assert st == 200 and body["ok"] and body["starts"] == [0, 0.3] and body["duration"] == pytest.approx(0.51)
    assert call(helper, "/api/status")[1]["running"]
    t0, wave, rate = helper.out.played[-1]
    assert t0 == body["t_start"] and rate == 100 and wave[0, 0] == 2 and wave[1, 35] == 1
    time.sleep(0.8)
    st, body, _ = call(helper, "/api/status")
    assert not body["running"] and body["run"]["id"] == 1 and body["error"] == ""


def test_stop_ends_a_protocol_early(helper):
    call(helper, "/api/run", body={"steps": [{"dur": 30, "ao0": 2}]})
    st, body, _ = call(helper, "/api/stop")
    assert body["ok"] and body["was_running"] and not helper.out.busy and helper.out.levels == [0, 0]


def test_bad_protocol_and_bad_json_get_reasons(helper):
    st, body, _ = call(helper, "/api/run", body={"steps": [{"dur": 1, "ao0": 9}]})
    assert st == 400 and "outside" in body["message"]
    st, body, _ = call(helper, "/api/run", body=b"{not json")
    assert st == 400 and "JSON" in body["message"]


def test_page_is_served_only_when_given(helper):
    assert call(helper, "/", code=None, origin=None)[0] == 404
    helper.page = b"<!doctype html><title>Bee Tracker</title>"
    with urllib.request.urlopen(helper.url + "/", timeout=5) as r:
        assert r.status == 200 and r.headers["Content-Type"].startswith("text/html") and r.read() == helper.page


def test_too_many_wrong_codes_lock_it(helper):
    for _ in range(L.MAX_BAD_CODES):
        call(helper, "/api/status", code="999999")
    st, body, _ = call(helper, "/api/status")
    assert st == 403 and "restart" in body["error"].lower()


def test_main_refuses_bad_options_and_reports_a_missing_package(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        L.main(["--max-volts", "12"])
    monkeypatch.setitem(sys.modules, "nidaqmx", None)        # import fails
    assert L.main(["--port", "0"]) == 2
    assert "pip install nidaqmx" in capsys.readouterr().err


# ---------------------------------------------------------------- functions of time
def fn_step(dur, ao0=None, ao1=None):
    return {"dur": dur, "ao0": ao0, "ao1": ao1}


def test_each_function_makes_the_expected_levels():
    steps = [fn_step(1, {"fn": "ramp", "from": 0, "to": 4}),
             fn_step(1, {"fn": "sine", "freq": 1, "mid": 2, "amp": 1, "phase": 90}),
             fn_step(1, {"fn": "pulses", "freq": 10, "duty": 30, "on": 3, "off": 0.5}),
             fn_step(1, {"fn": "pulse", "delay": 0.2, "width": 0.1, "v": 2}, {"fn": "exp", "from": 4, "to": 0, "tau": 0.5}),
             fn_step(1, {"fn": "random", "min": 1, "max": 3, "hold": 0.25, "seed": 7}),
             fn_step(1, {"fn": "formula", "expr": "1 + (t >= 0.5) * 2"}, 1.5)]
    wave, starts = L.build_wave(steps, 100, 5)
    assert starts == [0, 1, 2, 3, 4, 5]
    assert wave[0, 50] == pytest.approx(2) and wave[0, 99] == pytest.approx(3.96)          # ramp
    assert wave[0, 100] == pytest.approx(3) and wave[0, 150] == pytest.approx(1)           # sine, 90° in
    assert list(wave[0, 200:210]) == [3, 3, 3, .5, .5, .5, .5, .5, .5, .5]                  # 30% of 10 Hz
    assert list(wave[0, 319:331]) == [0] + [2] * 10 + [0]                                   # 0.2-0.3 s pulse
    assert wave[1, 300] == 4 and wave[1, 350] == pytest.approx(4 * np.exp(-1))
    r = wave[0, 400:500]
    assert len(set(r[:25])) == 1 and len(set(r)) == 4 and r.min() >= 1 and r.max() <= 3     # 4 held levels
    assert wave[0, 549] == 1 and wave[0, 550] == 3 and wave[1, 520] == 1.5 and wave[:, -1].tolist() == [0, 0]


@pytest.mark.parametrize("step, msg", [
    (fn_step(1, {"fn": "sine", "freq": 1, "mid": 4, "amp": 2, "phase": 0}), "outside"),
    (fn_step(1, {"fn": "formula", "expr": "sqrt(-1)"}), "no number"),
    (fn_step(1, {"fn": "formula", "expr": "sin(t"}), "missing"),
    (fn_step(1, {"fn": "formula", "expr": "__import__(1)"}), "doesn’t know"),
    (fn_step(1, {"fn": "pulses", "freq": 10, "duty": 100, "on": 1, "off": 0}), "duty"),
    (fn_step(1, {"fn": "ramp", "from": 0}), "must be a number"),
    (fn_step(1, {"fn": "laser"}), "unknown function"),
])
def test_bad_functions_are_refused_with_a_reason(step, msg):
    with pytest.raises(L.ProtocolError, match=msg):
        L.build_wave([step], 1000, 5)


def test_formula_grammar():
    f = L.parse_formula("2.5 + 2*sin(2*pi*t) - -1^2 + (t<1)*3 + clamp(t, 0, 1) + mod(-1, 3) + round(0.5) + d")
    assert f(np.array([0.25]), 10)[0] == pytest.approx(2.5 + 2 + 1 + 3 + 0.25 + 2 + 1 + 10)


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="needs Node to run src/core.js")
def test_page_and_helper_make_the_same_wave():
    """The page's preview and LED log (src/core.js) must match what the helper plays."""
    steps = [fn_step(0.7, {"fn": "ramp", "from": 0.5, "to": 4}, {"fn": "sine", "freq": 3.3, "mid": 2, "amp": 1.5, "phase": 30}),
             fn_step(1.2345, {"fn": "pulses", "freq": 13, "duty": 37, "on": 3, "off": 0.25}, {"fn": "exp", "from": 1, "to": 4, "tau": 0.3}),
             fn_step(0.5, {"fn": "pulse", "delay": 0.1, "width": 0.123, "v": 2}, {"fn": "random", "min": 0, "max": 5, "hold": 0.017, "seed": 123456}),
             fn_step(0.9, {"fn": "formula", "expr": "2 + sin(2*pi*5*t)^3 - floor(t*4)/4 + max(t, 0.3) * (t > d/2)"}, {"fn": "const", "v": 1.25})]
    rate = 1000
    wave, _ = L.build_wave(steps, rate, 5)
    js = """const C = require(process.argv[1]); const steps = JSON.parse(process.argv[2]), rate = +process.argv[3], out = [[], []];
      steps.forEach(s => ['ao0', 'ao1'].forEach((o, ch) => { const g = C.ledEval(s[o]), n = C.ledSamples(s.dur, rate);
        for(let j = 0; j < n; j++) out[ch].push(g(j / rate, s.dur)); })); console.log(JSON.stringify(out));"""
    core = str(Path(__file__).resolve().parent.parent / "src" / "core.js")
    res = subprocess.run([NODE, "-e", js, core, json.dumps(steps), str(rate)], capture_output=True, text=True, check=True)
    page = np.array(json.loads(res.stdout))
    assert page.shape[1] == wave.shape[1] - 1
    assert np.max(np.abs(page - wave[:, :-1])) < 1e-9
