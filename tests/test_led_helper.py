"""Tests for helper/led_helper.py. Run: python3 -m pytest tests/test_led_helper.py

No DAQ is needed: the NI path is checked against a stand-in `nidaqmx` that records every call.
"""
import json
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
