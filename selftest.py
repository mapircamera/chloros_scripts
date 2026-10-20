"""
selftest.py -- self-contained checks that this project's output matches what
Chloros reads on import. Needs only numpy + Pillow; `pip install tifffile` adds
the full set of TIFF read-back checks.

Two halves:
  1. Metadata contract: write a raw LATTICE TIFF (M3C + M3M) and a DAQ .daq,
     then read them back with VERBATIM copies of the Chloros import readers
     (project.py:_is_lattice_image_path, tasks.py:_lattice_exif_context, and
     mip/daq_dls.py's als_meta/als_log reads + image_utc_offset_s timezone
     declaration). A pass means the files round-trip.
  2. DAQ wire codec: build synthetic device packets to the documented byte
     layout and parse them with record_daq.py's codec, plus a full
     connect->acquire flow against a simulated device.

Run:  python selftest.py
"""
import contextlib
import csv
import hashlib
import io
import json
import math
import os
import shutil
import sqlite3
import struct
import sys
import time

import numpy as np

import mapir_metadata as mm
import record_daq as R

try:
    import tifffile
    HAVE_TIFFFILE = True
except ImportError:  # only needed to read TIFFs back the way Chloros does
    tifffile = None
    HAVE_TIFFFILE = False

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_out")
shutil.rmtree(OUT, ignore_errors=True)  # start each run from a clean output dir
os.makedirs(OUT, exist_ok=True)
RESULTS = []
SKIPPED = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def skip(name, reason):
    SKIPPED.append((name, reason))
    print(f"  [SKIP] {name}  -- {reason}")


# ===========================================================================
# Part 1 -- metadata contract (verbatim Chloros readers; hermetic copies)
# ===========================================================================
def chloros_is_lattice(path):  # verbatim: project.py:_is_lattice_image_path
    try:
        if os.path.splitext(path)[1].lower() not in ('.tif', '.tiff', '.jpg', '.jpeg', '.png'):
            return False
        from PIL import Image as _PIL
        from PIL.ExifTags import TAGS as _T
        with _PIL.open(path) as _img:
            raw = _img.getexif()
        if not raw:
            return False
        ex = {_T.get(k, k): v for k, v in dict(raw).items()}
        m = ex.get('Model', '') or ''
        return isinstance(m, str) and m.upper().startswith('LATT-')
    except Exception:
        return False


def chloros_exif_context(path):  # verbatim core: tasks.py:_lattice_exif_context
    out = {}
    with tifffile.TiffFile(path) as t:
        pg = t.pages[0]
        ex = pg.tags.get(34665)
        exd = ex.value if (ex is not None and isinstance(ex.value, dict)) else {}
        serial = ''
        sn = pg.tags.get(50735)
        if sn is not None and sn.value:
            serial = str(sn.value).strip()
        if not serial:
            for k in ('CameraSerialNumber', 'BodySerialNumber', 'SerialNumber'):
                if exd.get(k):
                    serial = str(exd[k]).strip(); break
        if serial:
            out['serial'] = serial
        model = pg.tags.get(272)
        mstr = str(model.value) if model is not None else ''
        out['model'] = mstr
        out['pixel_format'] = 'Mono12' if 'M3M' in mstr.upper() else 'BayerRG12'
        et = exd.get('ExposureTime') if isinstance(exd, dict) else None
        if et:
            out['exp_us'] = (float(et[0]) / float(et[1]) * 1e6
                             if isinstance(et, (tuple, list)) else float(et) * 1e6)
        iso = (exd.get('ISOSpeedRatings') or exd.get('PhotographicSensitivity')) if isinstance(exd, dict) else None
        if iso:
            iso = float(iso[0] if isinstance(iso, (tuple, list)) else iso)
            if iso > 0:
                out['gain_db'] = 20.0 * math.log10(iso / 100.0)
    return out


def chloros_image_utc_offset(path):  # verbatim core: mip/daq_dls.py
    # image_utc_offset_s + _parse_exif_utc_offset -- the image-side timezone
    # declaration Chloros prefers when matching imagery to a .daq by time.
    import re as _re

    def _parse(value):
        s = str(value or "").strip()
        if not s:
            return None
        if s.upper() == "Z":
            return 0.0
        m = _re.fullmatch(r'([+-])(\d{1,2}):?(\d{2})', s)
        if not m:
            return None
        sign = -1.0 if m.group(1) == "-" else 1.0
        hh, mm = int(m.group(2)), int(m.group(3))
        if hh > 14 or mm > 59:
            return None
        return sign * (hh * 3600.0 + mm * 60.0)

    with tifffile.TiffFile(path) as t:
        ex = t.pages[0].tags.get(34665)
        exd = ex.value if (ex is not None and isinstance(ex.value, dict)) else {}
        for k in ("OffsetTimeOriginal", "OffsetTimeDigitized", "OffsetTime",
                  36881, 36882, 36880):
            if k in exd:
                off = _parse(exd[k])
                if off is not None:
                    return off
    return None


def chloros_read_daq(path):  # verbatim shape: mip/daq_dls.py meta + als_log read
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        # Read-by-name with a per-column presence check, like read_daq_meta --
        # utc_offset_minutes exists only in v1.23+ recordings.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(als_meta)")}
        wanted = [c for c in ("version", "product_model", "product_serial",
                              "calibration_applied", "calibration_bundle_sha",
                              "cap_id", "cap_applied", "cap_id_source",
                              "utc_offset_minutes")
                  if c in cols]
        m = dict(zip(wanted, conn.execute(
            "SELECT %s FROM als_meta LIMIT 1" % ", ".join(wanted)).fetchone()))
        rows = conn.execute(
            "SELECT precise_timestamp, spectral_data, is_saturated, integration_time "
            "FROM als_log WHERE event_type=3 AND precise_timestamp IS NOT NULL "
            "AND spectral_data IS NOT NULL ORDER BY precise_timestamp ASC").fetchall()
    finally:
        conn.close()
    specs = [(ts, np.load(io.BytesIO(b)), sat, it) for ts, b, sat, it in rows]
    return {'version': m.get('version'),
            'product_model': m.get('product_model'),
            'product_serial': m.get('product_serial'),
            'calibration_applied': bool(m.get('calibration_applied')),
            'calibration_bundle_sha': m.get('calibration_bundle_sha') or '',
            'cap_id': m.get('cap_id'),
            'cap_applied': m.get('cap_applied'),
            # Absent on pre-v1.25 files, exactly as Chloros sees it: None
            # there means "this file cannot say whether anyone checked".
            'cap_id_source': m.get('cap_id_source'),
            'utc_offset_minutes': m.get('utc_offset_minutes')}, specs


def chloros_would_calibrate(meta):  # verbatim rule: mip/daq_dls.py:_load_daq
    """Whether Chloros will apply a bundle to this file on import.

    The whole decision is one column. ``_load_daq`` reads
    ``als_meta.calibration_applied`` and, when it is 0 and a serial is
    present, fetches that serial's bundle and multiplies it in. Nothing
    inspects the spectra to notice they are already irradiance -- there is no
    signal in the numbers that could tell it, which is why the flag has to be
    right.
    """
    return (not meta['calibration_applied']) and bool(meta['product_serial'])


def test_metadata_contract():
    print("== metadata contract (LATTICE TIFF + DAQ .daq) ==")
    # M3C color: raw Bayer mosaic
    m3c = (np.random.rand(1536, 2048) * 4095).astype(np.uint16)
    pc = os.path.join(OUT, mm.lattice_capture_filename("213602328", 1))
    mm.write_lattice_raw_tiff(pc, m3c, model="LATT-M3C-L41-FRGN",
                              serial="213602328", exposure_s=0.005, iso=100)
    check("M3C detected as LATTICE", chloros_is_lattice(pc))
    check("M3C filename groups trigger", "_0001_" in os.path.basename(pc))
    if HAVE_TIFFFILE:
        c = chloros_exif_context(pc)
        check("M3C serial (cal key)", c.get('serial') == "213602328", c.get('serial'))
        check("M3C model", c.get('model') == "LATT-M3C-L41-FRGN")
        check("M3C BayerRG12", c.get('pixel_format') == 'BayerRG12')
        check("M3C exposure ~5000us", abs(c.get('exp_us', 0) - 5000) < 1, c.get('exp_us'))
        check("M3C gain 0dB", abs(c.get('gain_db', 9)) < 1e-6)
        check("M3C declares UTC (OffsetTimeOriginal +00:00)",
              chloros_image_utc_offset(pc) == 0.0)
        check("M3C raw pixels intact", np.array_equal(tifffile.imread(pc), m3c))
    else:
        skip("M3C EXIF + pixel checks", "pip install tifffile to read TIFFs back")

    # M3M mono
    m3m = (np.random.rand(1536, 2048) * 4095).astype(np.uint16)
    pm = os.path.join(OUT, mm.lattice_capture_filename("213609999", 1))
    mm.write_lattice_raw_tiff(pm, m3m, model="LATT-M3M-L41-F850",
                              serial="213609999", exposure_s=0.002, iso=200)
    check("M3M detected as LATTICE", chloros_is_lattice(pm))
    if HAVE_TIFFFILE:
        cm = chloros_exif_context(pm)
        check("M3M Mono12", cm.get('pixel_format') == 'Mono12')
        check("M3M serial", cm.get('serial') == "213609999")
        check("M3M exposure ~2000us", abs(cm.get('exp_us', 0) - 2000) < 1)
    else:
        skip("M3M EXIF checks", "pip install tifffile")

    # DAQ .daq
    pd = os.path.join(OUT, "test.daq")
    with mm.DaqWriter(pd, product_model="daq-u", product_serial="AA-BB-CC-DD-EE") as w:
        for i in range(15):
            w.write(list((np.random.rand(135) * 50000).astype("float32")),
                    is_saturated=False, integration_time_ms=32,
                    timestamp_ns=1_751_000_000_000_000_000 + i * 50_000_000)
    meta, specs = chloros_read_daq(pd)
    check("daq product_model", meta['product_model'] == 'daq-u')
    check("daq serial (cal key)", meta['product_serial'] == 'AA-BB-CC-DD-EE')
    check("daq calibration_applied=0", meta['calibration_applied'] is False)
    # v1.25 = the cap_id_source column. Pinned to the version whose SHAPE
    # this file writes, not to a number for its own sake: two producers
    # claiming one version with different als_meta columns is the trap the
    # hub already walked into.
    check("daq als_meta v1.27", meta['version'] == '1.27', meta['version'])
    check("daq records WHO chose the cap",
          meta['cap_id_source'] in mm._CAP_ID_SOURCES, meta['cap_id_source'])
    check("daq declares UTC stamps (utc_offset_minutes=0)",
          meta['utc_offset_minutes'] == 0, meta['utc_offset_minutes'])
    check("daq readings recovered", len(specs) == 15)
    check("daq spectrum float32 x135", specs[0][1].dtype == np.float32 and specs[0][1].size == 135)


# ===========================================================================
# Part 2 -- DAQ wire codec (delegates to the same synthetic builders/flow)
# ===========================================================================
def _sid_resp(idb):
    b = bytearray([3, 0xBB, 6, 0]) + bytes(idb); b.append(((~sum(b)) + 1) & 0xFF); return bytes(b)


def _spec_resp(spec, it, sat):
    n = len(spec); b = bytearray(12 + n * 4 + 12)
    b[0:4] = bytes([3, 0xBB, 0x28, 0]); struct.pack_into("<H", b, 4, it)
    b[6] = 1 if sat else 0; struct.pack_into("<I", b, 8, n)
    struct.pack_into("<%df" % n, b, 12, *spec)
    struct.pack_into("<fff", b, 12 + n * 4, .1, .2, .3)
    b.append(((~sum(b)) + 1) & 0xFF); return bytes(b)


def _simple_resp(cmd, total):
    b = bytearray(total - 1); b[0:4] = bytes([3, 0xBB, cmd, 0]); b.append(((~sum(b)) + 1) & 0xFF); return bytes(b)


class _FakeDev:
    host = "fake"
    def __init__(self, idb, spec): self._id, self._spec, self._out = idb, spec, []
    def open(self): pass
    def send(self, d):
        c = d[2]
        if c == R.CMD_HELLO: self._out.append(_simple_resp(R.CMD_HELLO, 5))
        elif c == R.CMD_GET_ID: self._out.append(_sid_resp(self._id))
        elif c == R.CMD_GET_WL: self._out.append(_simple_resp(R.CMD_GET_WL, 279))
        elif c == R.CMD_ACQ:
            self._out.append(_simple_resp(R.CMD_ACQ, 5))
            self._out.append(_spec_resp(self._spec, 32, False))
    def recv_packet(self, t): return self._out.pop(0) if self._out else None
    def close(self): pass


def test_wire_codec():
    print("== DAQ wire codec ==")
    check("hello 5B + valid", len(R.cmd_hello()) == 5 and R._checksum_ok(R.cmd_hello()))
    a = R.cmd_acquire(500, 3, True)
    check("acquire 10B, inttime500 LE, active-return", len(a) == 10 and a[4] == 244 and a[5] == 1 and a[8] == 1 and R._checksum_ok(a))
    sid = _sid_resp([0xAA, 0xBB, 0xCC, 0xDD, 0xEE])
    check("parse_sensor_id", R.parse_sensor_id(sid) == "AA-BB-CC-DD-EE")
    spin = list(np.linspace(0, 6, 135).astype("float32"))
    sp = _spec_resp(spin, 47, True)
    so, it, sat = R.parse_spectrum(sp)
    check("parse spectrum/inttime/sat", it == 47 and sat and np.allclose(np.array(so, "float32"), np.array(spin, "float32")))
    # framing resync past junk
    junk = bytes([0, 0xBB, 0x99]) + sid
    buf = io.BytesIO(junk)
    pk = R.read_stream_packet(lambda: buf.read(1), time.monotonic() + 1)
    check("stream framing resync", pk == sid)
    # full flow against simulated device
    spin2 = list((np.random.rand(135) * 5e4).astype("float32"))
    s = R.DaqSensor("daq-u", _FakeDev([1, 2, 3, 4, 5], spin2), integration_ms=32, frame_avg=3, enable_ae=True)
    check("connect -> serial", s.connect() == "01-02-03-04-05")
    so2, it2, _ = s.read_spectrum()
    check("read_spectrum skips ACK + matches", it2 == 32 and np.allclose(np.array(so2, "float32"), np.array(spin2, "float32")))


class _RecDev(_FakeDev):
    """_FakeDev that remembers every command code it was sent."""
    def __init__(self, idb, spec):
        super().__init__(idb, spec)
        self.sent = []

    def send(self, d):
        self.sent.append(d[2])
        super().send(d)


class _FakeCtl:
    """DAQ-E control channel: answers hello, nothing else needed here."""
    def __init__(self, *a, **k): pass
    def cmd(self, obj):
        return {"ok": True, "name": "daq-e-a1b2c3", "fw": "1.14.1",
                "model": "daq-e"}
    def close(self): pass


def _cal_135():
    """A real DeviceCalibration on the 135-point grid the wire codec carries."""
    import daq_cal
    wl = [340.0 + 5.0 * i for i in range(135)]
    return daq_cal.DeviceCalibration.from_bundle({
        "completed_utc": "2026-01-02T03:04:05+00:00",
        "run": {"device_kind": "daq-e", "sensor_id": "daq-e-a1b2c3"},
        "stages": {
            "dark": {"daq_dark_mean_w_per_m2_per_nm": 20.0,
                     "daq_dark_rate_w_per_m2_per_nm": 3.0,
                     "daq_dark_offset_per_ms_w_per_m2_per_nm": 17.0},
            "wavelength_alignment": {"corrected_wavelength_grid_nm": wl},
            "radiometric": {"gain_per_wavelength": [3.0e-5] * 135,
                            "irradiance_geometry_factor_applied": False}}},
        bundle_sha="5e1f" * 16, sensor_id="daq-e-a1b2c3")


def _record(kind, argv, dev, *, cal=None, read_spectrum=None, out=None):
    """Run record_daq.main() against fakes; return (stdout, stderr, error)."""
    import daq_cal
    saved = (R.SerialTransport, R.TcpTransport, R.DaqEControl,
             R.DaqSensor.read_spectrum, daq_cal.DeviceCalibration.from_device)
    R.SerialTransport = lambda *a, **k: dev
    R.TcpTransport = lambda *a, **k: dev
    R.DaqEControl = _FakeCtl
    if cal is not None:
        daq_cal.DeviceCalibration.from_device = classmethod(
            lambda cls, *a, **k: cal)
    if read_spectrum is not None:
        R.DaqSensor.read_spectrum = read_spectrum
    out, err = out or io.StringIO(), io.StringIO()
    error = None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            R.main([kind] + argv)
    except BaseException as e:          # SystemExit included: report it
        error = e
    finally:
        (R.SerialTransport, R.TcpTransport, R.DaqEControl,
         R.DaqSensor.read_spectrum) = saved[:4]
        daq_cal.DeviceCalibration.from_device = saved[4]
    return out.getvalue(), err.getvalue(), error


def test_record_daq_runs():
    """record_daq.main() end to end against a simulated sensor."""
    print("\n-- record_daq runs (simulated sensor) --")
    spec = list((np.random.rand(135) * 5e4 + 100).astype("float32"))

    # STANDBY parks the spectrometer, and nothing on a DAQ-E can wake it:
    # its firmware bridges TCP 5000 straight to the UART and never drives
    # the sensor's reset line, so the next run -- ours, Chloros's, even the
    # unit's own boot check -- gets no answer until the power is pulled
    # (Jetson rig, 2026-10-16). Chloros never sends it to any DAQ.
    p = os.path.join(OUT, "rec_u.daq")
    dev = _RecDev([1, 2, 3, 4, 5], spec)
    so, _, e = _record("u", ["--port", "FAKE", "--frames", "3",
                             "--output", p, "--csv"], dev)
    check("record_daq u completes", e is None, repr(e))
    check("a clean exit does NOT put the sensor into standby",
          R.CMD_STANDBY not in dev.sent,
          "" if R.CMD_STANDBY not in dev.sent else
          "STANDBY sent: a DAQ-E then answers nothing until power-cycled")
    rows = list(csv.reader(open(os.path.splitext(p)[0] + ".csv",
                                newline="", encoding="utf-8")))
    check("raw CSV names the cap the .daq beside it declares",
          "cap=sunshine_cosine" in rows[1][0] and "cap=None" not in rows[1][0],
          rows[1][0])
    check("the console does not call a RAW CSV calibrated",
          "calibrated CSV" not in so,
          next((ln.strip() for ln in so.splitlines() if "CSV" in ln), ""))

    # --calibrate bake --csv: the CSV is "what this run records", and a baked
    # run records W/m^2/nm -- so the CSV must hold the same numbers as the
    # .daq beside it, not the raw counts they were computed from.
    cal = _cal_135()
    p = os.path.join(OUT, "rec_e_bake.daq")
    dev = _RecDev([1, 2, 3, 4, 5], spec)
    _, _, e = _record("e", ["--host", "FAKE", "--frames", "2", "--output", p,
                            "--calibrate", "bake", "--csv"], dev, cal=cal)
    check("record_daq e --calibrate bake completes", e is None, repr(e))
    check("...and a DAQ-E is not put into standby either",
          R.CMD_STANDBY not in dev.sent)
    cv = os.path.splitext(p)[0] + ".csv"
    rows = ([r for r in csv.reader(open(cv, newline="", encoding="utf-8")) if r]
            if os.path.exists(cv) else [["(no CSV written)"], ["timestamp_utc"], []])
    check("bake --csv: the CSV says it is calibrated",
          "calibrated" in rows[0][0] and "RAW" not in rows[0][0], rows[0][0])
    hdr = next(r for r in rows if r and r[0] == "timestamp_utc")
    data = rows[rows.index(hdr) + 1] if rows.index(hdr) + 1 < len(rows) else []
    _, specs = chloros_read_daq(p) if os.path.exists(p) else ({}, [])
    units = data[hdr.index("units")] if "units" in hdr and data else None
    check("bake --csv: units are W/m^2/nm", units == "W/m^2/nm", units)
    vals = np.array([float(v) for v in data[6:]], dtype=np.float64)
    baked = specs[0][1] if specs else np.zeros(0)
    same = (len(specs) == 2 and vals.shape == baked.shape
            and np.allclose(vals, baked, rtol=1e-5, atol=1e-12))
    check("bake --csv: the CSV holds the .daq's own values", same,
          "" if same else "CSV and .daq disagree")

    # --duration must end a run whose sensor has gone silent. A timeout used
    # to `continue` past the duration check, so the run never ended.
    class _Runaway(Exception):
        pass
    t_start = time.monotonic()

    def silent(self):
        if time.monotonic() - t_start > 5.0:
            raise _Runaway()
        time.sleep(0.05)
        raise TimeoutError("no spectrum returned within timeout")

    p = os.path.join(OUT, "rec_silent.daq")
    _, _, e = _record("u", ["--port", "FAKE", "--duration", "0.3",
                            "--output", p], _RecDev([1, 2, 3, 4, 5], spec),
                      read_spectrum=silent)
    check("--duration ends a run whose sensor never answers",
          e is None and time.monotonic() - t_start < 3.0,
          f"{type(e).__name__} after {time.monotonic() - t_start:.1f}s")


class _SlowDisk:
    """sqlite3.connect() whose connections are as slow as a disk under load.

    A commit is a disk flush, and on the Jetson rig (2026-10-19), with four
    cameras writing TIFFs to the same ext4 NVMe, single commits took 12-20 s.
    ``commit_s`` sleeps in every commit after the first (the header commit,
    made before any reading); ``insert_s`` in every row insert, for a disk
    slower than the sensor. Patched at the sqlite3 level so it slows the
    recorder whichever way the recorder drives the connection.
    """

    def __init__(self, *, commit_s=0.0, insert_s=0.0):
        self.commit_s, self.insert_s = commit_s, insert_s
        self.commits = 0
        self._real = sqlite3.connect

    def __enter__(self):
        disk = self

        class _Conn:
            def __init__(self, conn):
                self._c = conn

            def commit(self):
                if disk.commits:
                    time.sleep(disk.commit_s)
                disk.commits += 1
                self._c.commit()

            def execute(self, sql, *a):
                if disk.insert_s and sql.startswith("INSERT INTO als_log"):
                    time.sleep(disk.insert_s)
                return self._c.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self._c, name)

        sqlite3.connect = lambda *a, **k: _Conn(disk._real(*a, **k))
        return self

    def __exit__(self, *exc):
        sqlite3.connect = self._real


class _StallingStream(io.StringIO):
    """A stdout that blocks once, the way a log file on a busy disk does:
    with record_daq's log on the NVMe the cameras were writing to, single
    progress lines took up to 3.3 s to print (Jetson rig, 2026-10-19)."""

    def __init__(self, marker, stall_s):
        super().__init__()
        self.marker, self.stall_s, self.stalled = marker, stall_s, False

    def write(self, text):
        if not self.stalled and self.marker in text:
            self.stalled = True
            time.sleep(self.stall_s)
        return super().write(text)


def _paced_sensor(period_s, taken, *, sigint_after=None, fail_after=None):
    """A read_spectrum that answers every ``period_s``, as a sensor does, and
    notes (in ``taken``) the wall clock each reading came back at."""
    import signal as _signal
    spec = [float(i) for i in range(135)]

    def read_spectrum(self):
        time.sleep(period_s)
        if fail_after is not None and len(taken) >= fail_after:
            raise ConnectionError("DAQ-E closed the connection")
        taken.append(time.time_ns())
        if sigint_after is not None and len(taken) == sigint_after:
            os.kill(os.getpid(), _signal.SIGINT)        # an operator's Ctrl-C
        return list(spec), 32, False
    return read_spectrum


def _stamps(path):
    _, specs = chloros_read_daq(path) if os.path.exists(path) else ({}, [])
    return [ts for ts, _, _, _ in specs]


def _stamped_on_arrival(stamps, taken, tol_ns=50_000_000):
    """Every reading taken is in the file, in order, stamped when it came off
    the sensor rather than when the disk took it."""
    return (len(stamps) == len(taken)
            and all(0 <= s - t <= tol_ns for s, t in zip(stamps, taken)))


def test_disk_stall():
    """A recording must not stop taking readings while the disk is busy.

    Jetson rig, 2026-10-19: record_daq on the DAQ-E alongside four LATTICE
    cameras writing ~11 TIFFs/s to the same NVMe. Readings flowed at ~5/s,
    then stopped for 12-20 s with nothing printed, twice in three runs:
    record_daq committed from its acquisition loop, every commit flushes the
    disk, and on ext4 that flush waited for the cameras' data. The readings
    of those seconds were never taken.
    """
    print("\n-- a busy disk never stops the readings (record_daq, daq_stream) --")
    period, stall = 0.02, 1.0

    # 1. Commits that take a second each. The sensor answers every 20 ms; the
    #    file must too, with no hole anywhere near the length of a commit.
    p = os.path.join(OUT, "stall_rate.daq")
    taken = []
    with _SlowDisk(commit_s=stall):
        so, _, e = _record("u", ["--port", "FAKE", "--duration", "2.5",
                                 "--output", p], _RecDev([1, 2, 3, 4, 5], []),
                           read_spectrum=_paced_sensor(period, taken))
    stamps = _stamps(p)
    gap = max((b - a for a, b in zip(stamps, stamps[1:])), default=0) / 1e9
    check("stalled commits: the run completes", e is None, repr(e))
    check("stalled commits: no gap in the readings near the stall length",
          stamps and gap < stall / 4,
          f"longest gap {gap:.2f} s with {stall:.1f} s commits")
    want = 2.5 / period
    check("stalled commits: readings keep the sensor's rate",
          len(stamps) >= 0.6 * want,
          f"{len(stamps)} readings in 2.5 s; the sensor gave {want:.0f}")
    check("stalled commits: every reading is in the file, stamped on arrival",
          _stamped_on_arrival(stamps, taken),
          f"{len(stamps)} in the file, {len(taken)} taken")
    span = (stamps[-1] - stamps[0]) / 1e9 if stamps else 0.0
    check("stalled commits: readings run to the end of --duration",
          span > 2.5 - 0.3, f"first to last reading {span:.2f} s")
    check("stalled commits: the summary says the disk was slow, nothing lost",
          "no reading was lost" in so,
          next((ln.strip() for ln in so.splitlines() if "was slow" in ln),
               "(no such line)"))

    # 2. --frames ends the run while readings are still queued behind a disk
    #    slower than the sensor. Every one of them has to land.
    p = os.path.join(OUT, "stall_drain.daq")
    taken = []
    with _SlowDisk(commit_s=0.5, insert_s=0.02):
        so, _, e = _record("u", ["--port", "FAKE", "--frames", "60",
                                 "--output", p, "--csv"],
                           _RecDev([1, 2, 3, 4, 5], []),
                           read_spectrum=_paced_sensor(0.002, taken))
    stamps = _stamps(p)
    check("--frames with a slow disk: readings were still queued at the end",
          "queued reading(s) to disk" in so,
          next((ln for ln in so.splitlines() if "queued" in ln), "no backlog"))
    check("--frames with a slow disk: all of them land in the .daq",
          e is None and len(stamps) == 60 and _stamped_on_arrival(stamps, taken),
          f"{len(stamps)} of 60 ({e!r})")
    cv = os.path.splitext(p)[0] + ".csv"
    rows = ([r for r in csv.reader(open(cv, newline="", encoding="utf-8"))
             if r and not r[0].startswith("#") and r[0] != "timestamp_utc"]
            if os.path.exists(cv) else [])
    check("--frames with a slow disk: ...and in the CSV, same stamps",
          [int(r[1]) for r in rows] == stamps, f"{len(rows)} CSV rows")
    # The journal and sync settings are SQLite's defaults, as before; what
    # changed is which thread commits and how often. Read the result back
    # through the real Chloros loader too.
    repo = _chloros_repo()
    if repo is None:
        skip("write-behind .daq through mip.daq_dls",
             "set CHLOROS_REPO to a mapirlab checkout")
    else:
        saved = list(sys.path)
        try:
            sys.path.insert(0, repo)
            from mip.daq_dls import load_calibrated      # noqa: E402
            scan = load_calibrated(p)
            _, specs = chloros_read_daq(p)
            same = (scan is not None and len(scan.times) == len(specs) == 60
                    and np.allclose(scan.times, [s[0] / 1e9 for s in specs],
                                    rtol=0, atol=1e-6)
                    and np.array_equal(
                        np.asarray(scan.spectra, dtype=np.float32),
                        np.stack([s[1] for s in specs])))
            check("real Chloros reader: the write-behind .daq reads back "
                  "identically", same,
                  "" if same else f"{None if scan is None else len(scan.times)}"
                                  f" frames via mip.daq_dls")
        except ImportError as e:
            skip("write-behind .daq through mip.daq_dls", f"import failed: {e}")
        finally:
            sys.path[:] = saved

    # 3. Ctrl-C mid-backlog: a clean stop writes everything already taken.
    p = os.path.join(OUT, "stall_sigint.daq")
    taken = []
    with _SlowDisk(commit_s=0.5, insert_s=0.02):
        so, _, e = _record("u", ["--port", "FAKE", "--output", p],
                           _RecDev([1, 2, 3, 4, 5], []),
                           read_spectrum=_paced_sensor(0.002, taken,
                                                       sigint_after=40))
    stamps = _stamps(p)
    check("Ctrl-C with readings queued: every reading taken lands",
          e is None and len(taken) >= 40
          and _stamped_on_arrival(stamps, taken),
          f"{len(stamps)} in the file, {len(taken)} taken ({e!r})")

    # 4. The sensor link dies mid-backlog: the error still surfaces, and the
    #    readings taken before it are not lost with it.
    p = os.path.join(OUT, "stall_error.daq")
    taken = []
    with _SlowDisk(commit_s=0.5, insert_s=0.02):
        so, _, e = _record("u", ["--port", "FAKE", "--output", p],
                           _RecDev([1, 2, 3, 4, 5], []),
                           read_spectrum=_paced_sensor(0.002, taken,
                                                       fail_after=30))
    stamps = _stamps(p)
    check("sensor error with readings queued: the error is raised",
          isinstance(e, ConnectionError), repr(e))
    check("sensor error with readings queued: the 30 taken still land",
          _stamped_on_arrival(stamps, taken), f"{len(stamps)} of {len(taken)}")

    # 5. A disk stalled for longer than the queue holds: readings ARE lost
    #    then, and the run must say so -- live and in its summary -- and
    #    account for every one.
    p = os.path.join(OUT, "stall_overflow.daq")
    taken = []
    saved = getattr(R, "WRITE_QUEUE_READINGS", None)
    R.WRITE_QUEUE_READINGS = 5
    try:
        with _SlowDisk(insert_s=0.05):
            so, se, e = _record("u", ["--port", "FAKE", "--frames", "60",
                                      "--output", p],
                                _RecDev([1, 2, 3, 4, 5], []),
                                read_spectrum=_paced_sensor(0.002, taken))
    finally:
        R.WRITE_QUEUE_READINGS = saved
    stamps = _stamps(p)
    lost = 60 - len(stamps)
    check("queue overflow: says so while it happens",
          "being DROPPED" in se, se.strip().splitlines()[0] if se else "")
    check("queue overflow: the summary counts exactly what was lost",
          e is None and lost > 0
          and f"{lost} of 60 readings were DROPPED" in se,
          f"{lost} lost; stderr: {se.strip().splitlines()[-1:] or ''}")
    _it = iter(taken)                           # kept = a subsequence
    check("queue overflow: what was kept is in order, stamped on arrival",
          all(any(0 <= s - t <= 50_000_000 for t in _it) for s in stamps),
          f"{len(stamps)} kept")

    # 6. The disk fails outright: the recorder stops with that error rather
    #    than taking readings it can no longer write, and does not hang.
    p = os.path.join(OUT, "stall_diskfull.daq")
    real_write = mm.DaqWriter.write

    def full(self, *a, **k):
        if self.record_count >= 3:
            raise sqlite3.OperationalError("database or disk is full")
        return real_write(self, *a, **k)
    mm.DaqWriter.write = full
    taken = []
    try:
        _, _, e = _record("u", ["--port", "FAKE", "--duration", "5",
                                "--output", p], _RecDev([1, 2, 3, 4, 5], []),
                          read_spectrum=_paced_sensor(period, taken))
    finally:
        mm.DaqWriter.write = real_write
    # Counted in readings, not seconds: how long the file takes to close
    # is the disk's business. A loop that carried on would take ~250.
    check("disk error: the run stops with it, promptly",
          isinstance(e, sqlite3.OperationalError) and len(taken) < 30,
          f"{e!r} after {len(taken)} readings")
    check("disk error: the rows written before it are kept",
          len(_stamps(p)) == 3, f"{len(_stamps(p))} rows")

    # 7. stdout in a log file on that same disk: a print there blocks like
    #    any other write, so the loop's own progress lines must not wait.
    p = os.path.join(OUT, "stall_stdout.daq")
    taken = []
    slow_out = _StallingStream("readings ...", stall)
    _, _, e = _record("u", ["--port", "FAKE", "--duration", "2.0",
                            "--output", p], _RecDev([1, 2, 3, 4, 5], []),
                      read_spectrum=_paced_sensor(period, taken), out=slow_out)
    stamps = _stamps(p)
    gap = max((b - a for a, b in zip(stamps, stamps[1:])), default=0) / 1e9
    check("stdout blocked mid-run: the readings do not wait for it",
          e is None and slow_out.stalled and gap < stall / 4
          and len(stamps) >= 0.6 * 2.0 / period,
          f"{len(stamps)} readings, longest gap {gap:.2f} s with a "
          f"{stall:.1f} s print ({e!r})")
    check("stdout blocked mid-run: its lines still all come out",
          "Stopped. Wrote" in slow_out.getvalue()
          and slow_out.getvalue().count("readings ...") == len(taken) // 10,
          f"{slow_out.getvalue().count('readings ...')} progress lines for "
          f"{len(taken)} readings")

    # 8. daq_stream --daq: the same stalls must not hold up the socket. A
    #    frame without a device clock is stamped on arrival, so a receive
    #    loop stuck in a commit or a print shows up as a hole in its stamps.
    import daq_stream as ds

    def _stream(p, argv, stdout):
        arrived = []

        class _S:
            def setsockopt(self, *a): pass
            def settimeout(self, *a): pass
            def bind(self, *a): pass
            def close(self): pass

            def recvfrom(self, _n):
                time.sleep(period)
                b = bytearray(_v2_frame(b"\x11\x22\x33\x44\x55",
                                        b"\xaa\xbb\xcc\xdd\xee\x01",
                                        seq=len(arrived) + 1))
                b[3] &= ~ds.FLAG_ABS_TS & 0xFF       # no device clock
                b[-2:] = struct.pack("<H", ds._crc16_ccitt(bytes(b[:-2])))
                arrived.append(time.time_ns())
                return bytes(b), ("10.0.0.9", 5002)
        real, real_q = ds.socket.socket, ds._query_unit
        ds.socket.socket = lambda *a, **k: _S()
        ds._query_unit = lambda host, **k: {"name": "daq-e-a1b2c3",
                                            "status": None}
        e = None
        try:
            with contextlib.redirect_stdout(stdout):
                ds.main(["--daq", p, "--duration", "2.5"] + argv)
        except BaseException as ex:              # noqa: BLE001
            e = ex
        finally:
            ds.socket.socket, ds._query_unit = real, real_q
        stamps = _stamps(p)
        gap = max((b - a for a, b in zip(stamps, stamps[1:])),
                  default=0) / 1e9
        return e, stamps, arrived, gap

    p = os.path.join(OUT, "stall_stream.daq")
    with _SlowDisk(commit_s=stall):
        e, stamps, arrived, gap = _stream(p, ["--quiet"], io.StringIO())
    # Counted too: a loop stuck for the whole run has no gaps either.
    check("daq_stream --daq, stalled commits: no gap near the stall length",
          e is None and gap < stall / 4 and len(stamps) >= 0.6 * 2.5 / period,
          f"{len(stamps)} frames, longest gap {gap:.2f} s with {stall:.1f} s "
          f"commits ({e!r})")
    # The first frame is the one that opens the .daq (a disk flush of its
    # own); it is stamped on arrival like the rest.
    check("daq_stream --daq, stalled commits: every frame, stamped on arrival",
          _stamped_on_arrival(stamps, arrived),
          f"{len(stamps)} in the file, {len(arrived)} arrived")
    p = os.path.join(OUT, "stall_stream_stdout.daq")
    slow_out = _StallingStream("seq=", stall)
    e, stamps, arrived, gap = _stream(p, [], slow_out)
    check("daq_stream --daq, stdout blocked: no gap near the stall length",
          e is None and slow_out.stalled and gap < stall / 4
          and len(stamps) >= 0.6 * 2.5 / period,
          f"{len(stamps)} frames, longest gap {gap:.2f} s with a "
          f"{stall:.1f} s print ({e!r})")


# ===========================================================================
# Part 3 -- LATTICE camera config + capture (fake arena_api device)
# ===========================================================================
import capture_lattice as C


class _FakeNode:
    def __init__(self, nm, name):
        self._nm, self._name = nm, name

    @property
    def value(self):
        return self._nm._vals.get(self._name)

    @value.setter
    def value(self, v):
        self._nm._writes.append((self._name, v))
        self._nm._vals[self._name] = v

    @property
    def max(self):
        return self._nm._max.get(self._name, 0)

    @property
    def min(self):
        return self._nm._min.get(self._name, 0)

    def execute(self):
        self._nm._writes.append((self._name, "<EXEC>"))


class _FakeNodemap:
    def __init__(self, vals=None, maxes=None, missing=(), mins=None):
        self._vals = dict(vals or {})
        self._max = dict(maxes or {})
        self._min = dict(mins or {})
        self._missing = set(missing)
        self._writes = []

    def get_node(self, name):
        if name in self._missing:
            raise KeyError(name)
        return _FakeNode(self, name)

    def invalidate_nodes(self):
        self._writes.append(("<INVALIDATE>", None))


class _ArmingNodemap(_FakeNodemap):
    """TriggerArmed reads False ``arm_after`` times, then True. Every read
    and every TriggerSoftware execute goes into the shared ``log`` as
    (tag, event), so the order across several cameras can be checked."""

    def __init__(self, vals, arm_after, log, tag):
        super().__init__(vals, {"Width": 8, "Height": 8})
        self._left, self._log, self._tag = arm_after, log, tag

    def get_node(self, name):
        nm = self
        if name == "TriggerArmed":
            class _Armed:
                @property
                def value(self):
                    armed = nm._left <= 0
                    nm._left -= 1
                    nm._log.append((nm._tag, "armed" if armed else "not-armed"))
                    return armed
            return _Armed()
        if name == "TriggerSoftware":
            class _Exec:
                def execute(self):
                    nm._log.append((nm._tag, "TRIGGER"))
            return _Exec()
        return super().get_node(name)


class _FakeBuffer:
    def __init__(self, arr, chunks=None):
        import ctypes
        self.width = arr.shape[1]
        self.height = arr.shape[0]
        self.bits_per_pixel = 16
        b = arr.astype(np.uint16).tobytes()
        self.pdata = (ctypes.c_ubyte * len(b)).from_buffer_copy(b)
        self.frame_id = 7
        self._chunks = dict(chunks or {})

    def get_chunk(self, names):
        class _V:
            def __init__(self, v): self.value = v
        if not all(n in self._chunks for n in names):
            raise ValueError("chunk data not present in this buffer")
        return {n: _V(self._chunks[n]) for n in names}


class _FakeDevice:
    def __init__(self, nm, arr, chunks=None):
        self.nodemap = nm
        self.tl_stream_nodemap = _FakeNodemap()
        self._buf = _FakeBuffer(arr, chunks)

    def start_stream(self, n): pass
    def stop_stream(self): pass
    def get_buffer(self, timeout=2000): return self._buf
    def requeue_buffer(self, b): pass


def _writes_seq(dev):
    return [f"{n}={v}" for n, v in dev.nodemap._writes]


def test_camera_config():
    print("== LATTICE camera config (fake device) ==")
    # identity: M3C color
    nm = _FakeNodemap({"DeviceSerialNumber": "213602328",
                       "DeviceUserID": "M3C-L41-FRGN"},
                      {"Width": 2048, "Height": 1536})
    cam = C.LatticeCamera(_FakeDevice(nm, np.zeros((4, 4), np.uint16)))
    s, m = cam.identify()
    check("identity serial", s == "213602328")
    check("identity model -> LATT- prefix", m == "LATT-M3C-L41-FRGN", m)
    check("M3C detected as color", cam.is_mono is False)

    cam.configure_raw(exposure_us=None)
    w = dict(cam.dev.nodemap._writes)
    check("PixelFormat BayerRG12 (color)", w.get("PixelFormat") == "BayerRG12")
    check("Width/Height set to max", w.get("Width") == 2048 and w.get("Height") == 1536)
    check("ISP off", w.get("GammaEnable") is False and w.get("LUTEnable") is False)
    check("DefectCorrection off", w.get("DefectCorrectionEnable") is False)
    check("ExposureAuto Continuous (auto)", w.get("ExposureAuto") == "Continuous")

    # M3M mono + fixed exposure
    nm2 = _FakeNodemap({"DeviceSerialNumber": "9", "DeviceUserID": "M3M-L41-F850"},
                       {"Width": 2048, "Height": 1536})
    cam2 = C.LatticeCamera(_FakeDevice(nm2, np.zeros((4, 4), np.uint16)))
    cam2.identify()
    check("M3M detected as mono", cam2.is_mono is True)
    cam2.configure_raw(exposure_us=5000)
    w2 = dict(cam2.dev.nodemap._writes)
    check("PixelFormat Mono12 (mono)", w2.get("PixelFormat") == "Mono12")
    check("fixed exposure: ExposureAuto Off + ExposureTime", w2.get("ExposureAuto") == "Off" and w2.get("ExposureTime") == 5000.0)

    # --model: fallback for a factory-reset camera (empty DeviceUserID), and
    # NOT an override for one that still knows its own model.
    blank = _FakeNodemap({"DeviceSerialNumber": "77", "DeviceUserID": ""},
                         {"Width": 8, "Height": 8})
    cam3 = C.LatticeCamera(_FakeDevice(blank, np.zeros((4, 4), np.uint16)))
    try:
        cam3.identify()
        check("empty DeviceUserID with no --model raises", False)
    except RuntimeError as e:
        check("empty DeviceUserID with no --model raises", "--model" in str(e))

    cam3 = C.LatticeCamera(_FakeDevice(blank, np.zeros((4, 4), np.uint16)))
    _, m3 = cam3.identify("LATT-M3M-L41-F850")
    check("--model fills in an empty DeviceUserID",
          m3 == "LATT-M3M-L41-F850", m3)
    check("--model fallback still sets mono", cam3.is_mono is True)

    cam3b = C.LatticeCamera(_FakeDevice(blank, np.zeros((4, 4), np.uint16)))
    check("--model without the LATT- prefix is prefixed",
          cam3b.identify("M3C-L41-FRGN")[1] == "LATT-M3C-L41-FRGN")

    known = _FakeNodemap({"DeviceSerialNumber": "78",
                          "DeviceUserID": "M3C-L41-FRGN"},
                         {"Width": 8, "Height": 8})
    cam4 = C.LatticeCamera(_FakeDevice(known, np.zeros((4, 4), np.uint16)))
    check("--model does NOT override a camera that reports its own model",
          cam4.identify("LATT-M3M-L41-F850")[1] == "LATT-M3C-L41-FRGN")


def test_cable_sync_ordering():
    print("== cable sync wiring + firmware-quirk ordering ==")
    # master
    nm = _FakeNodemap({"DeviceSerialNumber": "1", "DeviceUserID": "M3C-L41-FRGN"}, {})
    master = C.LatticeCamera(_FakeDevice(nm, np.zeros((4, 4), np.uint16)))
    master.configure_master()
    mw = dict(master.dev.nodemap._writes)
    check("master TriggerSource Software", mw.get("TriggerSource") == "Software")
    check("master LineMode Output", mw.get("LineMode") == "Output")
    check("master LineSource ExposureActive", mw.get("LineSource") == "ExposureActive")

    # slave -- ORDER matters
    nm2 = _FakeNodemap({"DeviceSerialNumber": "2", "DeviceUserID": "M3C-L41-FRGN"}, {})
    slave = C.LatticeCamera(_FakeDevice(nm2, np.zeros((4, 4), np.uint16)))
    slave.configure_slave()
    seq = _writes_seq(slave.dev)
    def idx(s): return seq.index(s)
    check("slave biases LineSource=ExposureActive (deaf-input quirk)", "LineSource=ExposureActive" in seq)
    check("slave TriggerSource=Line2", "TriggerSource=Line2" in seq)
    check("quirk: TriggerMode=Off BEFORE LineMode=Input",
          idx("TriggerMode=Off") < idx("LineMode=Input"))
    check("quirk: LineMode=Input BEFORE TriggerSource=Line2",
          idx("LineMode=Input") < idx("TriggerSource=Line2"))
    check("quirk: bias LineSource BEFORE TriggerSource",
          idx("LineSource=ExposureActive") < idx("TriggerSource=Line2"))
    check("slave armed last (TriggerMode=On is final TriggerMode write)",
          seq[-1] == "TriggerMode=On")


def test_camera_capture_flow():
    print("== full capture flow (fake cameras -> TIFF -> Chloros reader) ==")
    img = (np.random.rand(1536, 2048) * 4095).astype(np.uint16)
    nm = _FakeNodemap({"DeviceSerialNumber": "213602328", "DeviceUserID": "M3C-L41-FRGN",
                       "ExposureTime": 4000.0, "Gain": 0.0, "TriggerArmed": True},
                      {"Width": 2048, "Height": 1536})
    cam = C.LatticeCamera(_FakeDevice(nm, img))
    cam.identify()
    arr, info = cam.grab_raw()
    check("buffer_to_numpy shape+dtype", arr.shape == (1536, 2048) and arr.dtype == np.uint16)
    check("buffer_to_numpy pixel-exact", np.array_equal(arr, img))

    class _Args:
        output_dir = os.path.join(OUT, "cam"); frames = 1; duration = 0
        interval = 0; timeout_ms = 2000
    import threading
    stop = threading.Event()
    cam.role = "single"
    C.capture_loop([cam], "software", _Args(), stop)
    tiffs = [f for f in os.listdir(_Args.output_dir) if f.endswith(".tiff")]
    check("a TIFF was written", len(tiffs) == 1, tiffs)
    p = os.path.join(_Args.output_dir, tiffs[0])
    check("capture TIFF detected as LATTICE", chloros_is_lattice(p))
    if HAVE_TIFFFILE:
        ctx = chloros_exif_context(p)
        check("capture TIFF serial (cal key)", ctx.get("serial") == "213602328", ctx.get("serial"))
        check("capture TIFF model", ctx.get("model") == "LATT-M3C-L41-FRGN")
        check("capture TIFF exposure ~4000us", abs(ctx.get("exp_us", 0) - 4000) < 1, ctx.get("exp_us"))
        check("capture TIFF declares UTC (OffsetTimeOriginal +00:00)",
              chloros_image_utc_offset(p) == 0.0)
    else:
        skip("capture TIFF EXIF checks", "pip install tifffile")


def test_camera_frame_metadata():
    """Per-frame exposure, and triggering only an armed camera.

    Both measured on the Jetson rig, 2026-10-16. The ExposureTime NODE is a
    cached GenICam value: under auto-exposure it stayed at 1851 us in every
    frame's EXIF while the frames were really exposed at 1603 -> 2299 us, and
    Chloros divides by EXIF exposure, so radiance drifted +44%. Only the
    frame's own chunk data says what that frame got. Separately, a software
    trigger sent before TriggerArmed is dropped: one camera in software mode
    lost every other frame.
    """
    print("\n-- per-frame exposure + trigger arming (fake device) --")
    img = (np.random.rand(8, 8) * 4095).astype(np.uint16)
    nm = _FakeNodemap({"DeviceSerialNumber": "214000551",
                       "DeviceUserID": "M3C-L41-FNGB",
                       "ExposureTime": 1851.4, "Gain": 0.0,    # stale cache
                       "TriggerArmed": True},
                      {"Width": 8, "Height": 8})
    cam = C.LatticeCamera(_FakeDevice(
        nm, img, chunks={"ChunkExposureTime": 2298.5, "ChunkGain": 6.0}))
    cam.identify()
    cam.configure_raw(exposure_us=None)
    seq = _writes_seq(cam.dev)
    check("configure_raw turns chunk data on", "ChunkModeActive=True" in seq)
    for what in ("ExposureTime", "Gain"):
        i = seq.index(f"ChunkSelector={what}") if f"ChunkSelector={what}" in seq else -1
        check(f"...with the {what} chunk enabled",
              i >= 0 and "ChunkEnable=True" in seq[i + 1:i + 2], str(seq[-6:]))

    class _Args:
        output_dir = os.path.join(OUT, "cam_chunk"); frames = 1; duration = 0
        interval = 0; timeout_ms = 2000
    import threading
    cam.role = "single"
    C.capture_loop([cam], "software", _Args(), threading.Event())
    tiffs = [f for f in os.listdir(_Args.output_dir) if f.endswith(".tiff")]
    if HAVE_TIFFFILE and tiffs:
        ctx = chloros_exif_context(os.path.join(_Args.output_dir, tiffs[0]))
        check("EXIF exposure is the FRAME's (chunk), not the cached node",
              abs(ctx.get("exp_us", 0) - 2298.5) < 1, ctx.get("exp_us"))
        check("EXIF gain is the FRAME's too (6 dB -> ISO 200)",
              abs(ctx.get("gain_db", 0) - 6.0) < 0.1, ctx.get("gain_db"))
    else:
        skip("per-frame EXIF checks", "pip install tifffile")

    # Software trigger: wait for TriggerArmed, then fire.
    log = []
    cam = C.LatticeCamera(_FakeDevice(_ArmingNodemap(
        {"DeviceSerialNumber": "1", "DeviceUserID": "M3C-L41-FOCN"},
        3, log, "solo"), img))
    cam.trigger()
    fired = [i for i, (_, ev) in enumerate(log) if ev == "TRIGGER"]
    check("software trigger waits until the camera is armed",
          len(fired) == 1 and fired[0] > 0 and log[fired[0] - 1][1] == "armed"
          and ("solo", "not-armed") in log[:fired[0]], str(log))

    # Cable: the master's trigger is what every slave exposes on, so it may
    # only fire once the SLAVES are armed too, or they miss the edge.
    log = []
    master = C.LatticeCamera(_FakeDevice(_ArmingNodemap(
        {"DeviceSerialNumber": "1", "DeviceUserID": "M3C-L41-FRGB"},
        0, log, "master"), img))
    slave = C.LatticeCamera(_FakeDevice(_ArmingNodemap(
        {"DeviceSerialNumber": "2", "DeviceUserID": "M3C-L41-FRGN"},
        2, log, "slave"), img))
    for c in (master, slave):
        c.identify()

    class _Args2(_Args):
        output_dir = os.path.join(OUT, "cam_arm")
    C.capture_loop([master, slave], "cable", _Args2(), threading.Event())
    fired = [i for i, (t, ev) in enumerate(log) if ev == "TRIGGER"]
    slave_armed = [i for i, (t, ev) in enumerate(log)
                   if t == "slave" and ev == "armed"]
    check("cable: the master fires only after the slave is armed",
          fired and slave_armed and slave_armed[0] < fired[0], str(log))


def test_shared_link():
    """Several cameras on one shared 1 GbE link (Jetson rig, 2026-10-19).

    Frames sent at the same moment collide in the switch: about half the
    packets were lost, every multi-camera frame arrived incomplete, and the
    script saved them as ordinary TIFFs with a third of their rows zero.
    Staggering each camera's frame TRANSMISSION (GevSCFTD) fixed it while
    the exposures stayed simultaneous. Cable sync additionally needed the
    slave trigger-input deglitch Chloros applies (lattice_cam/multi.py):
    without it the slaves fired on M8 line chatter. And a trigger whose
    control packet was lost raised out of the loop and ended the run.
    """
    print("\n-- shared 1 GbE link: incomplete frames, stagger, deglitch --")
    import threading
    img = (np.random.rand(8, 8) * 4095).astype(np.uint16)

    class _Args:
        frames = 1; duration = 0; interval = 0; timeout_ms = 500

    # 1. An incomplete buffer is never written.
    nm = _FakeNodemap({"DeviceSerialNumber": "214000551",
                       "DeviceUserID": "M3C-L41-FNGB", "TriggerArmed": True},
                      {"Width": 8, "Height": 8})
    dev = _FakeDevice(nm, img, chunks={"ChunkExposureTime": 1000.0,
                                       "ChunkGain": 0.0})
    dev._buf.is_incomplete = True
    cam = C.LatticeCamera(dev); cam.identify(); cam.role = "single"
    args = _Args(); args.output_dir = os.path.join(OUT, "cam_partial")
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        res = C.capture_loop([cam], "software", args, threading.Event())
    written = [f for f in os.listdir(args.output_dir) if f.endswith(".tiff")]
    check("an incomplete frame is NOT saved as a TIFF", written == [],
          f"{len(written)} written" if written else "")
    check("...and the run says it lost it", "incomplete" in out.getvalue(),
          out.getvalue().strip().splitlines()[-1] if out.getvalue().strip() else "(silent)")
    check("...and reports the loss to its caller",
          isinstance(res, dict) and res.get("lost") == 1, repr(res))

    # 2. Slave deglitch, where Chloros puts it: after the LineSource bias,
    #    before TriggerSource.
    nm = _FakeNodemap({"DeviceSerialNumber": "2", "DeviceUserID": "M3C-L41-FRGN"}, {})
    slave = C.LatticeCamera(_FakeDevice(nm, img))
    slave.configure_slave()
    seq = _writes_seq(slave.dev)
    def at(w): return seq.index(w) if w in seq else -1
    check("slave trigger input is deglitched (5 us)",
          at("LineFilterSelector=Deglitch") >= 0 and at("LineFilterWidth=5.0") >= 0,
          str([w for w in seq if w.startswith("LineFilter")]))
    check("...after the LineSource bias and before TriggerSource",
          at("LineSource=ExposureActive") < at("LineFilterSelector=Deglitch")
          < at("LineFilterWidth=5.0") < at("TriggerSource=Line2"), str(seq))
    check("slave still armed last", seq[-1] == "TriggerMode=On")

    # 3. Transmission stagger: camera i holds its frame i x stagger.
    cams = []
    for i in range(4):
        nm = _FakeNodemap({"DeviceSerialNumber": str(10 + i), "DeviceUserID": "M3C-L41-FRGN",
                           "PayloadSize": 2048 * 1536 * 2 + 64,
                           "DeviceLinkSpeed": 125_000_000}, {})
        cams.append(C.LatticeCamera(_FakeDevice(nm, img)))
    try:
        auto = C.auto_stagger_ms(cams)
        C.configure_stagger(cams, auto)
        ftd = [dict(c.dev.nodemap._writes).get("GevSCFTD") for c in cams]
        # 65 ms for a full-res 12-bit frame at 1 GbE. Measured edges: 55 ms
        # was clean through a jumbo-frame switch but lost frames on a no-jumbo
        # PoE path (1500-byte packets), where 60 was the first clean value
        # -- so the default sits a step above both (Jetson rig, 2026-10-19).
        ok = (auto == 65 and ftd == [int(i * auto * 1e6) for i in range(4)])
        detail = f"auto {auto} ms -> GevSCFTD {ftd}"
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    check("4 cameras on 1 GbE: transmissions staggered 65 ms apart (no-jumbo margin)",
          ok, detail)
    try:
        one = C.auto_stagger_ms(cams[:1])
        ok, detail = one == 0, f"auto {one} ms"
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    check("a single camera is not delayed", ok, detail)

    # 4. A trigger whose control packet is lost does not end the run.
    class _FlakyNodemap(_FakeNodemap):
        fails = 1
        def get_node(self, name):
            if name == "TriggerSoftware" and self.fails > 0:
                self.fails -= 1
                raise TimeoutError("SC_ERR_TIMEOUT -1011")
            return super().get_node(name)
    nm = _FlakyNodemap({"DeviceSerialNumber": "214000535", "DeviceUserID": "M3C-L41-FRGB",
                        "TriggerArmed": True}, {"Width": 8, "Height": 8})
    cam = C.LatticeCamera(_FakeDevice(nm, img, chunks={"ChunkExposureTime": 1000.0,
                                                       "ChunkGain": 0.0}))
    cam.identify(); cam.role = "single"
    args = _Args(); args.frames = 2; args.output_dir = os.path.join(OUT, "cam_flaky")
    out, err = io.StringIO(), None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            res = C.capture_loop([cam], "software", args, threading.Event())
    except Exception as e:
        err, res = e, None
    written = [f for f in os.listdir(args.output_dir) if f.endswith(".tiff")] \
        if os.path.isdir(args.output_dir) else []
    check("a lost trigger does not end the run", err is None, repr(err))
    check("...the shot is reported lost and the next one is saved",
          len(written) == 1 and isinstance(res, dict) and res.get("lost") == 1,
          f"{len(written)} written, {res!r}")


def test_link_pacing():
    """Pacing another program left on a camera (Jetson rig, 2026-10-20).

    Chloros paces an array by giving every camera a packet delay (GevSCPD;
    405454 ns on the rig) and, for bandwidth splitting, a link throughput
    limit -- and both stay on the camera after Chloros lets go. The script
    never wrote either, so it inherited them: one camera fell from 12.67 fps
    to 2.66 and four cable-synced cameras from about 3.6 to 1.80. The script
    keeps frames apart with the transmission stagger, so it now sends each
    frame at full link speed: the throughput limit off (the factory
    default) and the packet delay it is given (0 unless --packet-delay-ns).
    """
    print("\n-- link pacing left on a camera by another program --")
    img = (np.random.rand(8, 8) * 4095).astype(np.uint16)

    def paced(sn="214701173", scpd=405454, mode="On"):
        nm = _FakeNodemap({"DeviceSerialNumber": sn, "DeviceUserID": "M3C-L41-FRGN",
                           "GevSCPD": scpd, "DeviceLinkThroughputLimitMode": mode,
                           "TriggerArmed": True},
                          {"Width": 8, "Height": 8, "GevSCPD": 12748056},
                          mins={"GevSCPD": 0})
        cam = C.LatticeCamera(_FakeDevice(nm, img)); cam.identify()
        return cam

    def final(cam, name):
        return cam.dev.nodemap._vals.get(name)

    # 1. The helper: both pacing nodes, in an order the camera accepts.
    cam = paced()
    notes, err = [], None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            notes = C.configure_link_pacing([cam])
    except Exception as e:
        err = e
    check("pacing left on a camera is cleared (packet delay 0, no throughput limit)",
          err is None and final(cam, "GevSCPD") == 0
          and final(cam, "DeviceLinkThroughputLimitMode") == "Off",
          repr(err) if err else f"GevSCPD={final(cam, 'GevSCPD')} "
          f"mode={final(cam, 'DeviceLinkThroughputLimitMode')}")
    seq = _writes_seq(cam.dev)
    def at(w): return seq.index(w) if w in seq else -1
    check("...the throughput limit goes off BEFORE the packet delay is written",
          0 <= at("DeviceLinkThroughputLimitMode=Off") < at("GevSCPD=0"), str(seq))
    check("...and the run is told what was cleared",
          len(notes) == 1 and "214701173" in notes[0] and "405454" in notes[0],
          repr(notes))

    cam = paced(scpd=0, mode="Off")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            notes = C.configure_link_pacing([cam])
        ok, detail = notes == [], repr(notes)
    except Exception as e:
        ok, detail = False, repr(e)
    check("a camera already at full speed gets no note", ok, detail)

    cams = [paced(sn="1"), paced(sn="2")]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            C.configure_link_pacing(cams[:1], packet_delay_ns=1000)
            C.configure_link_pacing(cams[1:], packet_delay_ns=99_999_999)
        got = [final(c, "GevSCPD") for c in cams]
        ok, detail = got == [1000, 12748056], f"GevSCPD {got}"
    except Exception as e:
        ok, detail = False, repr(e)
    check("an explicit packet delay is written, clamped to the camera's range", ok, detail)

    # 2. main() applies it on every run, before any frame is taken.
    cam = paced()
    seen = {}
    real = (C._load_system, C.open_cameras, C.capture_loop)
    class _Sys:
        def destroy_device(self): pass
    def fake_loop(ordered, mode, args, stop):
        seen["GevSCPD"] = final(ordered[0], "GevSCPD")
        seen["mode"] = final(ordered[0], "DeviceLinkThroughputLimitMode")
        return {"lost": 0}
    C._load_system = lambda: _Sys()
    C.open_cameras = lambda system, wanted=None, model=None: [cam]
    C.capture_loop = fake_loop
    out, err = io.StringIO(), None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            C.main(["--sync", "software", "--frames", "1",
                    "--output-dir", os.path.join(OUT, "cam_pacing")])
    except BaseException as e:
        err = e
    finally:
        C._load_system, C.open_cameras, C.capture_loop = real
    check("a capture run clears the pacing before its first frame",
          err is None and seen == {"GevSCPD": 0, "mode": "Off"},
          repr(err) if err else repr(seen))
    check("...and says so", "405454" in out.getvalue(),
          out.getvalue().strip().splitlines()[-1] if out.getvalue().strip() else "(silent)")


def test_offline_calibration():
    """daq_cal: the transform applied straight off the DAQ-E's onboard bundle.

    Self-contained by design -- reference values are computed here from the
    documented chain rather than by importing chloros, so this file keeps
    working in a checkout that has no chloros beside it. The *cross-repo*
    parity guard (bit equality against chloros's own DAQCalibration) lives in
    mapirlab/tests/test_daq_scripts_offline_parity.py.
    """
    print("\n-- offline calibration (daq_cal) --")
    import daq_cal

    n = 8
    wl = [400.0 + 10.0 * i for i in range(n)]
    gain = [3.0e-5 * (1 + 0.1 * i) for i in range(n)]
    raw = [100.0 * (i + 1) for i in range(n)]

    def bundle(*, decomp=True, geom=False):
        dark = {"daq_dark_mean_w_per_m2_per_nm": 20.0}
        if decomp:
            dark["daq_dark_rate_w_per_m2_per_nm"] = 3.0
            dark["daq_dark_offset_per_ms_w_per_m2_per_nm"] = 17.0
        return {"completed_utc": "2026-01-02T03:04:05+00:00",
                "run": {"device_kind": "daq-e", "sensor_id": "AA-BB-CC-DD-EE"},
                "stages": {
                    "dark": dark,
                    "wavelength_alignment": {"corrected_wavelength_grid_nm": wl},
                    "radiometric": {"gain_per_wavelength": gain,
                                    "irradiance_geometry_factor_applied": geom}}}

    cal = daq_cal.DeviceCalibration.from_bundle(bundle())
    check("dark model is the per-unit decomposition",
          cal.dark_model == "rate_plus_offset_over_t", cal.dark_model)
    # dark(t) = rate + offset/t
    check("dark(1 ms) = rate + offset", abs(cal.effective_dark(1) - 20.0) < 1e-9)
    check("dark(50 ms) = 3 + 17/50", abs(cal.effective_dark(50) - 3.34) < 1e-9)
    check("dark(None) falls back to the fixed scalar",
          abs(cal.effective_dark(None) - 20.0) < 1e-9)

    # out = (raw - dark) * gain * pi, floored at zero.
    want = np.maximum(
        (np.asarray(raw, dtype=np.float32) - np.float32(3.34))
        * np.asarray(gain, dtype=np.float32) * np.float32(math.pi), 0.0)
    got = cal.apply(raw, integration_time_ms=50)
    check("apply = (raw - dark(t)) * gain * pi",
          np.allclose(got, want, rtol=1e-6), f"max diff {np.max(np.abs(got - want)):.3g}")

    # A bundle with the geometry already folded in must NOT get pi again.
    got_geom = daq_cal.DeviceCalibration.from_bundle(
        bundle(geom=True)).apply(raw, integration_time_ms=50)
    check("pi is not applied twice when baked into gain",
          np.allclose(got_geom, want / math.pi, rtol=1e-6))

    # Negative dark residue is clamped, never emitted.
    dim = [1.0] * n
    check("sub-dark readings clamp to zero, not negative",
          np.all(cal.apply(dim, integration_time_ms=50) >= 0.0))

    # Scalar-only bundle + fleet fraction pushed in the profiles document.
    f = 0.9
    prof = {"schema_version": 1, "device_kind": "e", "cap_id": "none",
            "dark_fraction": {"offset_fraction": f}}
    scal = daq_cal.DeviceCalibration.from_bundle(bundle(decomp=False))
    scal_f = daq_cal.DeviceCalibration.from_bundle(bundle(decomp=False),
                                                   profiles=prof)
    check("scalar-only bundle alone -> fixed scalar",
          scal.dark_model == "fixed_scalar", scal.dark_model)
    check("scalar-only + pushed fleet fraction -> integration aware",
          scal_f.dark_model == "fleet_fraction", scal_f.dark_model)
    check("fleet fraction: dark(t) = mean * ((1-f) + f/t)",
          abs(scal_f.effective_dark(50) - 20.0 * ((1 - f) + f / 50)) < 1e-9)
    check("a per-unit decomposition outranks the fleet fraction",
          daq_cal.DeviceCalibration.from_bundle(
              bundle(decomp=True), profiles=prof).dark_model
          == "rate_plus_offset_over_t")

    # Cap profile multiplies in; as_recorded skips every per-lambda profile.
    corr = [0.5] * n
    prof_cap = {"schema_version": 1, "device_kind": "e",
                "cap_id": "sunshine_cosine",
                "cap_profile": {"cap_id": "sunshine_cosine",
                                "wavelength_grid_nm": wl,
                                "correction_mean": corr}}
    capped = daq_cal.DeviceCalibration.from_bundle(bundle(), profiles=prof_cap)
    check("cap correction is applied",
          np.allclose(capped.apply(raw, integration_time_ms=50),
                      want * 0.5, rtol=1e-6))
    check("as_recorded skips the cap",
          np.allclose(capped.apply(raw, integration_time_ms=50,
                                   cap_id="as_recorded"), want, rtol=1e-6))
    # The device carries ONE resolved profile. An override naming a different
    # cap can't be honoured -- there is no local copy of that cap's curve --
    # and applying the stored curve under another name is a ~11x error between
    # sunshine and bare. Refuse instead.
    def _cap_refused(cid):
        try:
            capped.apply(raw, integration_time_ms=50, cap_id=cid)
            return False
        except daq_cal.CalibrationError:
            return True

    check("cap_id naming a different cap is refused, not mis-applied",
          _cap_refused("fov_45"))
    check("cap_id='none' on a capped device is refused (would be ~11x off)",
          _cap_refused("none"))
    check("cap_id matching the stored profile is a no-op override",
          np.allclose(capped.apply(raw, integration_time_ms=50,
                                   cap_id="sunshine_cosine"),
                      want * 0.5, rtol=1e-6))
    check("cap profile NaN bins pass through uncorrected", np.allclose(
        daq_cal.DeviceCalibration.from_bundle(
            bundle(),
            profiles={"schema_version": 1, "cap_id": "c",
                      "cap_profile": {"cap_id": "c", "wavelength_grid_nm": wl,
                                      "correction_mean": [float("nan")] * n}}
        ).apply(raw, integration_time_ms=50), want, rtol=1e-6))

    # Failure modes that must be loud rather than silently wrong.
    def raises(fn):
        try:
            fn()
            return False
        except daq_cal.CalibrationError:
            return True

    check("spectrum/gain length mismatch is rejected",
          raises(lambda: cal.apply(raw[:3], integration_time_ms=50)))
    check("a newer profiles schema is refused, not guessed at",
          raises(lambda: daq_cal.DeviceCalibration.from_bundle(
              bundle(), profiles={"schema_version":
                                  daq_cal.PROFILES_SCHEMA_VERSION + 1})))
    check("a bundle with no gain vector is rejected",
          raises(lambda: daq_cal.DeviceCalibration.from_bundle({"stages": {}})))
    check("no profiles -> reported as bare/uncorrected",
          cal.profiles_source == "none" and cal.cap_id == "none")


def test_cap_declaration():
    """The cap a recording declares, and what Chloros makes of it.

    The cap is the one field an operator can get wrong from outside: the
    sensor cannot sense what is screwed onto it, so the recording has to
    SAY. Getting it wrong is 20-30x in downwelling and there is nothing
    downstream that can check the number against anything.

    MAPIR ships DAQ-U / DAQ-M / DAQ-E with the sunshine cosine corrector
    permanently installed, and Chloros's own recorder declares
    sunshine_cosine for that reason -- so a raw recording written here has to
    declare the same thing, or the two paths disagree on identical hardware.
    A DAQ-E-S is the exception in the opposite direction: its diffuser was on
    the unit when its factory bundle was measured, so NO profile may apply.
    """
    print("\n-- cap declaration (what Chloros will apply) --")
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_selftest_out")
    os.makedirs(tmp, exist_ok=True)

    def stamped(model, **kw):
        path = os.path.join(tmp, "cap_%s.daq" % model.replace("-", "_"))
        w = mm.DaqWriter(path, product_model=model, product_serial="SN", **kw)
        cap = w.cap_id
        w.write(np.ones(135, dtype=np.float32), is_saturated=False,
                integration_time_ms=32, timestamp_ns=1_752_600_000_000_000_000)
        w.close()
        return cap, chloros_read_daq(path)[0]

    for model in ("daq-u", "daq-m", "daq-e"):
        cap, meta = stamped(model)
        check(f"{model} defaults to the cap it ships wearing",
              cap == "sunshine_cosine" and meta["cap_id"] == "sunshine_cosine",
              f"declared {cap!r}; 'none' on a capped unit reads 20-30x low")
        check(f"{model} records the cap without applying it",
              not meta["cap_applied"],
              "raw spectra: Chloros applies the cap at import")

    cap, meta = stamped("daq-e-s")
    check("daq-e-s defaults to as_recorded (diffuser is inside its gain)",
          cap == "as_recorded" and meta["cap_id"] == "as_recorded",
          f"declared {cap!r}; sunshine on top double-counts it ~11x")

    cap, _ = stamped("daq-u", cap_id="none")
    check("an explicit 'none' is still honoured (physically stripped unit)",
          cap == "none")

    # The corrector is REMOVABLE on U/M/E and the sensor cannot sense it, so
    # the default is a guess. It has to be recorded AS a guess -- that is what
    # lets Chloros flag it and an operator override it later. A stamp claiming
    # someone checked is the one thing that makes the mistake permanent.
    _, meta = stamped("daq-u")
    check("an assumed cap is recorded as assumed",
          meta["cap_id_source"] == "auto_default", meta["cap_id_source"])
    _, meta = stamped("daq-u", cap_id="sunshine_cosine")
    check("a stated cap is recorded as operator-declared",
          meta["cap_id_source"] == "operator", meta["cap_id_source"])
    _, meta = stamped("daq-e", cap_id="fov_45", cap_id_source="device")
    check("a cap read off the device is recorded as such",
          meta["cap_id_source"] == "device", meta["cap_id_source"])
    _, meta = stamped("daq-e-s")
    check("a DAQ-E-S cap is model-forced, not assumed",
          meta["cap_id_source"] == "model", meta["cap_id_source"])

    def _bad_source():
        try:
            mm.DaqWriter(os.path.join(tmp, "cap_bad.daq"),
                         product_model="daq-u", product_serial="SN",
                         cap_id="none", cap_id_source="probably")
            return False
        except ValueError:
            return True
    check("an unknown cap_id_source is refused, not written", _bad_source(),
          "Chloros reads these four values; a fifth is unreadable provenance")

    # A DAQ-E-S must be distinguishable in the file. Chloros maps both to
    # bundle kind 'e', but their cap treatment is opposite, so a recording
    # that calls a DAQ-E-S 'daq-e' cannot be processed correctly.
    check("daq-e-s is a valid product_model", "daq-e-s" in mm._VALID_KINDS)
    _, meta = stamped("daq-e-s")
    check("...and survives into the file as itself",
          meta["product_model"] == "daq-e-s", meta["product_model"])

    # cap_applied describes the BLOBS. record_daq computes it from the cap it
    # actually handed to apply(), so 'as_recorded' (which skips every
    # profile) can never be stamped as applied -- doing so makes Chloros
    # refuse a later operator override instead of correcting the file.
    # record_daq gates cap_applied on this sentinel, and daq_cal.apply()
    # returns early on it. If the two ever spell it differently, a recording
    # made with --cap-id as_recorded is stamped as carrying a cap it does not
    # have, and Chloros then REFUSES a later operator override instead of
    # correcting the file.
    import record_daq as _rd
    import daq_cal as _dc
    check("as_recorded sentinel agrees between record_daq and daq_cal",
          _rd.CAP_ID_AS_RECORDED == _dc.CAP_ID_AS_RECORDED == "as_recorded",
          f"{_rd.CAP_ID_AS_RECORDED!r} vs {_dc.CAP_ID_AS_RECORDED!r}")

    repo = _chloros_repo()
    if repo is None:
        skip("live cap cross-check against mip.daq_dls",
             "set CHLOROS_REPO to a mapirlab checkout")
        return
    saved = list(sys.path)
    try:
        sys.path.insert(0, repo)
        from mip.daq_dls import _resolve_effective_cap, _cap_curve_or_none
        # What Chloros resolves for a raw recording is the cap we declared.
        for model, want in (("daq-u", "sunshine_cosine"),
                            ("daq-e", "sunshine_cosine"),
                            ("daq-e-s", "as_recorded")):
            cap, _ = stamped(model)
            check(f"Chloros resolves {model} default to {want}",
                  _resolve_effective_cap("auto", cap, False) == want)
        # And the correction really is the large factor claimed above, so
        # "declare it or be 20-30x out" is a measured statement.
        for kind, floor in (("daq-u", 20.0), ("daq-m", 15.0), ("daq-e", 10.0)):
            curve = _cap_curve_or_none("sunshine_cosine", kind)
            check(f"{kind} sunshine correction is large (>{floor:g}x)",
                  curve is not None and float(np.mean(curve)) > floor,
                  None if curve is None else "mean %.3gx" % float(np.mean(curve)))
        # NOTE: whether Chloros PRINTS its assumed-cap warning is not
        # asserted here. That warning lives inside the bundle-applying branch,
        # so it only fires when a calibration bundle for the serial actually
        # resolves -- which depends on the machine's cache and its network,
        # not on anything this file writes. Asserting it here would pass or
        # fail by environment. It is pinned on the Chloros side instead
        # (tests/test_daq_cap_provenance_warning.py), where the bundle can be
        # controlled.
    except ImportError as e:
        skip("live cap cross-check against mip.daq_dls", f"import failed: {e}")
    finally:
        sys.path[:] = saved


def _chloros_repo():
    """A mapirlab checkout to cross-check against, or None.

    Env var first, then the sibling directory these two repos are normally
    cloned into. Absent is the normal case for anyone outside MAPIR, so the
    live half of the round-trip check skips rather than fails.
    """
    cand = os.environ.get("CHLOROS_REPO", "").strip()
    if not cand:
        cand = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "mapirlab")
    return cand if os.path.isfile(os.path.join(cand, "mip", "daq_dls.py"))         else None


def test_set_device_cap():
    """Writing the cap a DAQ-E applies to its own calibrated stream.

    The device folds this into the W/m^2/nm it publishes, so it is the one
    knob that decides whether an offline consumer of that stream gets the
    truth. The document has to be byte-identical to what Chloros builds for
    the same cap: its sha IS its identity, so a serialisation that differs
    only cosmetically makes every Chloros connect rewrite flash.
    """
    print("\n-- set the cap on a DAQ-E (daq_cal --set-cap) --")
    import daq_cal as dc

    sent = {}

    class _Ctrl:
        def __init__(self, existing=None):
            self._existing = existing

        def cmd(self, obj):
            if obj.get("cmd") == "get_profiles":
                if self._existing is None:
                    return {"ok": False, "error": "no document"}
                return {"ok": True, "profiles_json": self._existing}
            if obj.get("cmd") == "set_profiles":
                sent.clear()
                sent.update(obj)
                return {"ok": True, "verified": True,
                        "sha256": obj["sha256"]}
            return {"ok": False}

    dc.set_device_cap(_Ctrl(), "as_recorded", device_kind="e")
    doc = json.loads(sent["profiles_json"])
    check("as_recorded needs no curve and carries none",
          doc["cap_id"] == "as_recorded" and doc["cap_profile"] is None)
    check("the pushed sha is the sha OF the pushed bytes",
          sent["sha256"] == hashlib.sha256(
              sent["profiles_json"].encode("utf-8")).hexdigest())

    def _refuses():
        try:
            dc.set_device_cap(_Ctrl(), "sunshine_cosine", device_kind="e")
            return False
        except dc.CalibrationError:
            return True
    check("a real cap without its curve is refused, not written as bare",
          _refuses(),
          "the device applies what it is given; there is no curve library "
          "aboard to look one up in")

    # Firmware that only echoes our sha back proves nothing about the write,
    # so a read-back must catch a corrupt store.
    class _Liar(_Ctrl):
        def cmd(self, obj):
            if obj.get("cmd") == "set_profiles":
                return {"ok": True, "sha256": obj["sha256"]}   # no 'verified'
            if obj.get("cmd") == "get_profiles":
                return {"ok": True, "profiles_json": '{"not":"what we sent"}'}
            return {"ok": False}

    def _catches_corruption():
        try:
            dc.set_device_cap(_Liar(), "as_recorded", device_kind="e")
            return False
        except dc.CalibrationError as exc:
            return "corrupt" in str(exc)
    check("a read-back that does not match is reported as corruption",
          _catches_corruption())

    repo = _chloros_repo()
    if repo is None:
        skip("profiles document matches Chloros byte for byte",
             "set CHLOROS_REPO to a mapirlab checkout")
        return
    saved = list(sys.path)
    try:
        sys.path.insert(0, repo)
        from daq.device_sync import (build_profiles_document as _chl_build,
                                     _canonical_json as _chl_json)
        for cap in ("as_recorded", "none", "sunshine_cosine", "fov_45"):
            theirs = _chl_build("daq-e", cap)
            mine = dc.build_profiles_document(
                "e", cap, cap_profile=theirs.get("cap_profile"),
                dark_fraction=theirs.get("dark_fraction"))
            check(f"profiles document for {cap!r} is byte-identical to "
                  f"Chloros's", _chl_json(theirs) == dc._canonical_json(mine))
        # And the dark_fraction the scripts cannot compute is carried over
        # from whatever the unit already holds, so setting a cap here does
        # not make Chloros rewrite flash on its next connect.
        existing = _chl_json(_chl_build("daq-e", "sunshine_cosine"))
        dc.set_device_cap(_Ctrl(existing), "fov_45",
                          cap_profile=_chl_build("daq-e", "fov_45")["cap_profile"],
                          device_kind="e")
        check("the unit's dark_fraction is carried forward, not dropped",
              sent["profiles_json"] == _chl_json(_chl_build("daq-e", "fov_45")))
    except ImportError as e:
        skip("profiles document matches Chloros byte for byte",
             f"import failed: {e}")
    finally:
        sys.path[:] = saved


def test_stream_recording():
    """All three DAQ-E streams, into the format each can honestly carry.

    raw and calibrated spectra become a .daq AND a .csv; attitude becomes a
    .csv and is refused a .daq, because Chloros only reads als_log rows that
    have spectral_data and an attitude sample is not a spectrum. A DAQ-E-S
    folds its attitude into the spectral frames as a trailer, and THAT does
    reach the .daq's imu_* columns -- which is the path that carries tilt
    alongside the irradiance it qualifies.
    """
    print("\n-- record the streams (daq_stream --daq / --csv / --imu) --")
    import daq_stream as ds
    import sqlite3 as _sq

    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_selftest_out")
    os.makedirs(tmp, exist_ok=True)

    def _trailer():
        t = bytearray(22)
        t[0] = 0x01
        t[1] = 0x01 | 0x02 | 0x04 | 0x08 | 0x10
        struct.pack_into("<H", t, 2, 12)
        struct.pack_into("<hhh", t, 4, 10, -5, 1000)
        struct.pack_into("<H", t, 10, 250)      # tilt   2.50
        struct.pack_into("<h", t, 12, -125)     # roll  -1.25
        struct.pack_into("<h", t, 14, 75)       # pitch  0.75
        struct.pack_into("<H", t, 16, 1001)
        struct.pack_into("<b", t, 18, 7)
        struct.pack_into("<H", t, 20, ds._crc16_ccitt(bytes(t[:20])))
        return bytes(t)

    def _spectral(cal, n=4):
        if cal:
            payload = struct.pack("<%df" % n, *[1.5] * n)
            flags = 0x02 | 0x08
        else:
            payload = (b"\x03\xbb\x28\x00" + struct.pack("<H", 50)
                       + b"\x00\x00" + struct.pack("<I", n)
                       + struct.pack("<%df" % n, *[100.0] * n))
            flags = 0x02
        flags |= 0x04 | 0x10                    # ptp + imu trailer
        d = bytearray(32)
        d[0:2] = b"\xda\x0e"; d[2] = 2; d[3] = flags
        struct.pack_into("<I", d, 4, 3)
        struct.pack_into("<Q", d, 8, 1_752_600_000_000_000)
        d[16:22] = b"\xaa\xbb\xcc\xdd\xee\xff"
        d[22:27] = b"\x11\x22\x33\x44\x55"
        d[27] = 1                               # daq-e-s
        struct.pack_into("<H", d, 28, 50)
        struct.pack_into("<H", d, 30, len(payload))
        d += payload
        d += struct.pack("<H", ds._crc16_ccitt(bytes(d)))
        return bytes(d) + _trailer()

    def _attitude(seq=42, ts_us=1_752_600_000_000_000):
        d = bytearray(58)
        d[0:2] = b"\xda\x0e"; d[2] = 0x03; d[3] = 0x02 | 0x04
        struct.pack_into("<I", d, 4, seq)
        struct.pack_into("<Q", d, 8, ts_us)
        d[16:22] = b"\xaa\xbb\xcc\xdd\xee\xff"
        d[22:27] = b"\x11\x22\x33\x44\x55"
        d[27] = 1
        d[28] = 0x01 | 0x02 | 0x04 | 0x08 | 0x10
        struct.pack_into("<H", d, 30, 9)
        struct.pack_into("<hhh", d, 32, 1, 2, 3)
        struct.pack_into("<hhh", d, 38, 4, 5, 6)
        struct.pack_into("<HhhHH", d, 44, 250, -125, 75, 1234, 500)
        struct.pack_into("<h", d, 54, 7)
        struct.pack_into("<H", d, 56, ds._crc16_ccitt(bytes(d[:56])))
        return bytes(d)

    def _run(argv, payload):
        class _S:
            def setsockopt(self, *a): pass
            def setblocking(self, *a): pass
            def settimeout(self, *a): pass
            def bind(self, *a): pass
            def close(self): pass
            def recvfrom(self, _n): return payload, ("10.0.0.9", 5002)
        real = ds.socket.socket
        real_q = getattr(ds, "_query_unit", None)
        ds.socket.socket = lambda *a, **k: _S()
        ds._query_unit = lambda host, **k: {
            "name": "daq-e-a1b2c3",
            "status": {"cap_id": "as_recorded", "profiles_present": False}}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ds.main(argv)
        finally:
            ds.socket.socket = real
            if real_q is not None:
                ds._query_unit = real_q

    for cal in (False, True):
        tag = "cal" if cal else "raw"
        dq = os.path.join(tmp, "stream_rec_%s.daq" % tag)
        cv = os.path.join(tmp, "stream_rec_%s.csv" % tag)
        argv = ["--count", "2", "--daq", dq, "--csv", cv, "--quiet",
                "--duration", "5"] + (["--calibrated"] if cal else [])
        _run(argv, _spectral(cal))

        meta, specs = chloros_read_daq(dq)
        check(f"{tag} stream -> a .daq Chloros can read",
              len(specs) == 2)
        # The calibration key is the unit's NAME (hello), not the
        # spectrometer serial the frame carries -- Chloros found no bundle
        # under the latter on hardware and skipped the recording.
        check(f"{tag} stream .daq is stamped with the unit name",
              meta["product_serial"] == "daq-e-a1b2c3", meta["product_serial"])
        # THE labelling property: a calibrated stream that does not declare
        # itself gets its bundle applied a second time at import.
        check(f"{tag} stream .daq declares calibration_applied={int(cal)}",
              meta["calibration_applied"] is cal,
              f"got {meta['calibration_applied']}")
        check(f"{tag} stream .daq records the model it heard",
              meta["product_model"] == "daq-e-s", meta["product_model"])

        conn = _sq.connect(dq)
        row = conn.execute("SELECT imu_tilt_deg, imu_roll_deg, imu_pitch_deg, "
                           "imu_cal_applied, device_ts_ptp FROM als_log "
                           "LIMIT 1").fetchone()
        conn.close()
        check(f"{tag} stream .daq carries the attitude trailer",
              row == (2.5, -1.25, 0.75, 1, 1), str(row))

        rows = list(csv.reader(open(cv, newline="", encoding="utf-8")))
        hdr = next(r for r in rows if r and r[0] == "device")
        data = rows[rows.index(hdr) + 1]
        check(f"{tag} stream .csv states its units",
              data[hdr.index("units")] == ("W/m^2/nm" if cal else "counts"))
        check(f"{tag} stream .csv carries tilt alongside the spectrum",
              data[hdr.index("tilt_deg")] == "2.5")

    cv = os.path.join(tmp, "stream_rec_imu.csv")
    _run(["--imu", "--count", "2", "--csv", cv, "--quiet", "--duration", "5"],
         _attitude())
    rows = list(csv.reader(open(cv, newline="", encoding="utf-8")))
    check("attitude stream -> its own .csv",
          rows[0][0].startswith("# MAPIR") and "attitude" in rows[0][0],
          rows[0][0])
    hdr, data = rows[1], rows[2]
    check("attitude .csv carries the corrected AND raw triplets",
          {"x_mg", "raw_x_mg", "tare_angle_deg"} <= set(hdr))
    check("attitude .csv decodes the angles",
          data[hdr.index("tilt_deg")] == "2.5"
          and data[hdr.index("roll_deg")] == "-1.25")

    # Attitude into a .daq: its own event_type, no spectrum, inert to every
    # reader that is looking for spectra.
    adq = os.path.join(tmp, "stream_rec_imu.daq")
    _run(["--imu", "--daq", adq, "--count", "3", "--quiet", "--imu-rate", "0",
          "--duration", "5"], _attitude())
    conn = _sq.connect(adq)
    kinds = dict(conn.execute(
        "SELECT event_type, COUNT(*) FROM als_log GROUP BY event_type"))
    arow = conn.execute("SELECT spectral_data, imu_tilt_deg, imu_roll_deg "
                        "FROM als_log WHERE event_type=4 LIMIT 1").fetchone()
    conn.close()
    check("attitude -> .daq under its OWN event type, not as spectra",
          kinds == {4: 3}, str(kinds))
    check("...carrying no spectrum, and the decoded angles",
          arow == (None, 2.5, -1.25), str(arow))
    # THE safety property. An attitude row that any spectral reader mistook
    # for a reading would put a phantom into the downwelling.
    _, specs = chloros_read_daq(adq)
    check("...and no spectral reader counts them as readings", specs == [],
          f"{len(specs)} phantom spectrum(s)")

    # --- rate control -------------------------------------------------
    # The device streams ~50 Hz and every sample is a row in BOTH outputs.
    # Most work does not need that, so the default thins it.
    _step_us = 20_000                       # 50 Hz
    _frames = [_attitude(seq=i, ts_us=1_752_600_000_000_000 + i * _step_us)
               for i in range(20)]          # 0.4 s of samples

    def _kept(rate):
        """(count, timestamps) written at *rate*; None = use the default."""
        tag = "def" if rate is None else str(rate).replace(".", "_")
        out = os.path.join(tmp, "rate_%s.daq" % tag)
        argv = ["--imu", "--count", str(len(_frames)), "--daq", out,
                "--quiet", "--duration", "30"]
        if rate is not None:
            argv += ["--imu-rate", str(rate)]
        box = {"i": 0}

        class _S:
            def setsockopt(self, *a): pass
            def setblocking(self, *a): pass
            def settimeout(self, *a): pass
            def bind(self, *a): pass
            def close(self): pass
            def recvfrom(self, _n):
                fr = _frames[min(box["i"], len(_frames) - 1)]
                box["i"] += 1
                return fr, ("10.0.0.9", 5004)
        real, real_q = ds.socket.socket, getattr(ds, "_query_unit", None)
        ds.socket.socket = lambda *a, **k: _S()
        ds._query_unit = lambda host, **k: {"name": "daq-e-a1b2c3",
                                            "status": None}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ds.main(argv)
        finally:
            ds.socket.socket = real
            if real_q is not None:
                ds._query_unit = real_q
        c = _sq.connect(out)
        ts = [r[0] for r in c.execute("SELECT precise_timestamp FROM als_log "
                                      "WHERE event_type=4 ORDER BY id")]
        c.close()
        return len(ts), ts

    n_all, _ = _kept(0)
    check("--imu-rate 0 keeps every sample the device sent",
          n_all == len(_frames), str(n_all))

    n_5, ts_5 = _kept(5)
    check("--imu-rate 5 thins a 50 Hz stream to 5 Hz", n_5 == 2, str(n_5))
    # precise_timestamp is NANOseconds; 1/5 Hz is 0.2 s.
    gaps = [(b - a) / 1e9 for a, b in zip(ts_5, ts_5[1:])]
    check("...spaced by the samples' OWN timestamps, not a frame counter",
          gaps and all(abs(g - 0.2) < 1e-6 for g in gaps),
          "a counter would drift with the device's actual rate; gaps(s)="
          + str(gaps))

    n_def, _ = _kept(None)
    check("thinning is the DEFAULT, so an unattended run stays small",
          n_def == n_5,
          f"default kept {n_def}, --imu-rate 5 kept {n_5}, full rate is "
          f"{len(_frames)}")
    check("...and a higher rate really does keep more", _kept(25)[0] > n_def)

    # An undefined angle must stay empty, never 0 -- 0 degrees means
    # PERFECTLY LEVEL, the single most dangerous value to invent for a column
    # whose purpose is deciding whether a cosine correction held.
    t = bytearray(_trailer())
    struct.pack_into("<H", t, 10, 0xFFFF)          # tilt: not computed
    struct.pack_into("<H", t, 20, ds._crc16_ccitt(bytes(t[:20])))
    imu = ds.parse_imu_trailer(bytes(t))
    check("an uncomputed angle decodes to None, not 0",
          imu is not None and imu["tilt_deg"] is None)
    check("...and reaches the CSV as empty, not 0", ds._csv_cell(None) == "")

    # Two decoders of one wire format that disagree are worse than one
    # decoder. This file now carries a copy of Chloros's IMU decode, so pin
    # the copy to the original over the values that actually vary: the flag
    # combinations and the "firmware wrote nothing" sentinels.
    repo = _chloros_repo()
    if repo is None:
        skip("IMU decode matches Chloros field for field",
             "set CHLOROS_REPO to a mapirlab checkout")
        return
    saved = list(sys.path)
    try:
        sys.path.insert(0, repo)
        from daq.sensors import e as _chl
        bad = []
        for flags in (0x00, 0x02, 0x04, 0x10, 0x1F, 0xFF):
            for tilt, roll, pitch in ((250, -125, 75), (0xFFFF, 0x7FFF, 0x7FFF),
                                      (0, 0, 0), (18000, -18000, 9000)):
                t = bytearray(_trailer())
                t[1] = flags
                struct.pack_into("<H", t, 10, tilt)
                struct.pack_into("<h", t, 12, roll)
                struct.pack_into("<h", t, 14, pitch)
                struct.pack_into("<H", t, 20,
                                 ds._crc16_ccitt(bytes(t[:20])))
                if ds.parse_imu_trailer(bytes(t)) != _chl.parse_imu_trailer(bytes(t)):
                    bad.append((flags, tilt, roll, pitch))
        check("IMU trailer decode matches Chloros field for field",
              not bad, f"{len(bad)} mismatch(es), e.g. {bad[:2]}")

        a = ds.parse_imu_datagram(_attitude())
        b = _chl.parse_imu_datagram(_attitude())
        shared = set(a) & set(b)
        diff = sorted(k for k in shared if a[k] != b[k])
        check("attitude datagram decode matches Chloros on every shared key",
              not diff, str(diff))
        # The two timestamp keys are deliberately named for THIS module's
        # spectral parser; everything else must line up.
        check("only the timestamp key names differ, and knowingly",
              sorted(set(b) - set(a)) == ["absolute_ts", "ts_us"],
              str(sorted(set(b) - set(a))))
    except ImportError as e:
        skip("IMU decode matches Chloros field for field", f"import failed: {e}")
    finally:
        sys.path[:] = saved


def _v2_frame(serial, mac, *, cal=False, seq=1, n=135):
    """One v2 spectral datagram (plain DAQ-E, no trailer)."""
    import daq_stream as ds
    if cal:
        payload = struct.pack("<%df" % n, *[0.02] * n)
        flags = 0x02 | 0x08
    else:
        payload = (b"\x03\xbb\x28\x00" + struct.pack("<H", 50) + b"\x00\x00"
                   + struct.pack("<I", n) + struct.pack("<%df" % n, *[100.0] * n))
        flags = 0x02
    d = bytearray(32)
    d[0:2] = b"\xda\x0e"; d[2] = 2; d[3] = flags
    struct.pack_into("<I", d, 4, seq)
    struct.pack_into("<Q", d, 8, 1_752_600_000_000_000 + seq * 400_000)
    d[16:22] = mac
    d[22:27] = serial
    d[27] = 0                                   # daq-e
    struct.pack_into("<H", d, 28, 50)
    struct.pack_into("<H", d, 30, len(payload))
    d += payload
    d += struct.pack("<H", ds._crc16_ccitt(bytes(d)))
    return bytes(d)


def test_stream_identity():
    """What daq_stream --daq stamps: whose recording it is, and which cap.

    On hardware (2026-10-16) a raw stream .daq stamped with the frame's
    spectrometer serial was skipped by Chloros -- "no calibration bundle for
    daq-e CB-7C-A8-12-43" -- while record_daq's recording of the same unit,
    stamped with the unit's hello name, calibrated. And the calibrated stream
    carried the device's sunshine profile while the file said as_recorded /
    not applied, which invites a second cap on top.
    """
    print("\n-- daq_stream --daq identity + cap (simulated units) --")
    import daq_stream as ds
    A = (b"\x11\x22\x33\x44\x55", b"\xaa\xbb\xcc\xdd\xee\x01")
    B = (b"\x66\x77\x88\x99\xaa", b"\xaa\xbb\xcc\xdd\xee\x02")

    def run(argv, frames, query):
        calls = []

        def q(host, **k):
            calls.append((host, k))
            r = query(host, **k)
            if isinstance(r, Exception):
                raise r
            return r

        box = {"i": 0}

        class _S:
            def setsockopt(self, *a): pass
            def setblocking(self, *a): pass
            def settimeout(self, *a): pass
            def bind(self, *a): pass
            def close(self): pass
            def recvfrom(self, _n):
                fr, ip = frames[box["i"] % len(frames)]
                box["i"] += 1
                return fr, (ip, 5002)
        real, real_q = ds.socket.socket, getattr(ds, "_query_unit", None)
        ds.socket.socket = lambda *a, **k: _S()
        ds._query_unit = q
        out, err, error = io.StringIO(), io.StringIO(), None
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                ds.main(argv)
        except BaseException as e:
            error = e
        finally:
            ds.socket.socket = real
            if real_q is not None:
                ds._query_unit = real_q
        return calls, out.getvalue() + err.getvalue(), error

    def daq(name):
        return os.path.join(OUT, name)

    named = lambda host, **k: {"name": "daq-e-a1b2c3", "status": None}
    raw_a = [(_v2_frame(*A, seq=i), "10.0.0.9") for i in range(1, 5)]

    p = daq("ident_raw.daq")
    calls, _, e = run(["--count", "3", "--daq", p, "--quiet",
                       "--duration", "5"], raw_a, named)
    meta, specs = chloros_read_daq(p) if os.path.exists(p) else ({}, [])
    check("raw stream .daq carries the unit name Chloros keys on",
          e is None and meta.get("product_serial") == "daq-e-a1b2c3",
          f"{meta.get('product_serial')!r} {e!r}")
    check("...asked of the unit that sent the frames",
          bool(calls) and calls[0][0] == "10.0.0.9", str(calls[:1]))

    p = daq("ident_named.daq")
    calls, _, e = run(["--count", "2", "--daq", p, "--quiet", "--duration",
                       "5", "--unit-name", "daq-e-fffff0"], raw_a, named)
    meta, _ = chloros_read_daq(p) if os.path.exists(p) else ({}, [])
    check("--unit-name stamps that name without asking the unit",
          e is None and meta.get("product_serial") == "daq-e-fffff0"
          and not calls, f"{meta.get('product_serial')!r} calls={len(calls)} {e!r}")

    p = daq("ident_unreachable.daq")
    cv = daq("ident_unreachable.csv")
    calls, said, e = run(["--count", "3", "--daq", p, "--csv", cv, "--quiet",
                          "--duration", "5"], raw_a,
                         lambda host, **k: OSError("connection refused"))
    check("a unit that cannot be asked gets NO .daq under a guessed serial",
          e is None and not os.path.exists(p), f"exists={os.path.exists(p)} {e!r}")
    check("...says how to record anyway (--unit-name)", "--unit-name" in said,
          said.strip().splitlines()[-1] if said.strip() else "(silent)")
    check("...and asks only once, not on every frame", len(calls) == 1,
          f"{len(calls)} queries")
    check("...while the CSV still records", os.path.exists(cv)
          and sum(1 for _ in open(cv)) >= 5)

    cal_a = [(_v2_frame(*A, cal=True, seq=i), "10.0.0.9") for i in range(1, 5)]
    for status, want_cap, want_applied, label in (
            ({"cap_id": "sunshine_cosine", "profiles_present": True},
             "sunshine_cosine", 1, "a device carrying the sunshine profile"),
            ({"cap_id": "sunshine_cosine", "profiles_present": False},
             "as_recorded", 0, "a device carrying no profile")):
        p = daq("ident_cal_%d.daq" % want_applied)
        _, _, e = run(["--calibrated", "--count", "2", "--daq", p, "--quiet",
                       "--duration", "5"], cal_a,
                      lambda host, s=status, **k: {"name": "daq-e-a1b2c3",
                                                   "status": s})
        meta, _ = chloros_read_daq(p) if os.path.exists(p) else ({}, [])
        got = (meta.get("cap_id"), int(bool(meta.get("cap_applied"))),
               meta.get("cap_id_source"))
        check(f"calibrated .daq from {label} declares what it folded in",
              e is None and got == (want_cap, want_applied, "device"),
              f"got {got} {e!r}")

    p = daq("ident_cal_op.daq")
    _, _, e = run(["--calibrated", "--count", "2", "--daq", p, "--quiet",
                   "--duration", "5", "--cap-id", "fov_45"], cal_a,
                  lambda host, **k: {"name": "daq-e-a1b2c3", "status": None})
    meta, _ = chloros_read_daq(p) if os.path.exists(p) else ({}, [])
    got = (meta.get("cap_id"), int(bool(meta.get("cap_applied"))),
           meta.get("cap_id_source"))
    check("--cap-id on a calibrated stream is recorded as the operator's",
          e is None and got == ("fov_45", 1, "operator"), f"got {got} {e!r}")

    p = daq("ident_cal_nostatus.daq")
    _, said, e = run(["--calibrated", "--count", "2", "--daq", p, "--quiet",
                      "--duration", "5"], cal_a,
                     lambda host, **k: PermissionError("auth_failed"))
    check("calibrated stream with no way to learn the cap: no .daq, and why",
          e is None and not os.path.exists(p) and "--cap-id" in said,
          f"exists={os.path.exists(p)}")

    # Two units on one group: one .daq is ONE unit's recording.
    mixed = []
    for i in range(1, 5):
        mixed.append((_v2_frame(*A, seq=i), "10.0.0.9"))
        mixed.append((_v2_frame(*B, seq=i), "10.0.0.10"))
    p = daq("ident_mixed.daq")
    _, said, e = run(["--count", "8", "--daq", p, "--quiet", "--duration", "5"],
                     mixed, named)
    meta, specs = chloros_read_daq(p) if os.path.exists(p) else ({}, [])
    check("a .daq holds only the unit that opened it",
          e is None and len(specs) == 4, f"{len(specs)} readings from 8 frames")
    check("...and says the other unit was left out",
          "66-77-88-99-AA" in said and "--serial" in said,
          said.strip().splitlines()[-1] if said.strip() else "(silent)")


def test_export_round_trip():
    """Chloros exports a calibrated .daq; re-importing it must not calibrate
    it a second time.

    Chloros writes <project>/Light Sensor/<name>_calibrated.daq beside every
    recording it imports. That file is a valid .daq -- so it can be imported
    again, deliberately or by a recursive re-import of the project folder --
    and the ONLY thing standing between it and being multiplied by its bundle
    a second time is als_meta.calibration_applied. Nothing in the spectra
    reveals that they are already W/m^2/nm, so a wrong flag squares the
    correction and reports success.
    """
    print("\n-- Chloros export round-trip (no double calibration) --")
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_selftest_out")
    os.makedirs(tmp, exist_ok=True)
    serial = "CB-7C-A8-16-83"
    counts = np.full(135, 250.0, dtype=np.float32)
    gain = 4.0                      # stands in for the factory bundle
    stamps = [1_752_600_000_000_000_000 + i * 1_000_000_000 for i in range(3)]

    # 1. What record_daq.py writes in the field: raw counts, flag clear.
    raw = os.path.join(tmp, "rt_raw.daq")
    with mm.DaqWriter(raw, product_model="daq-u", product_serial=serial,
                      tz_offset_minutes=0) as w:
        for ts in stamps:
            w.write(counts, is_saturated=False, integration_time_ms=50,
                    timestamp_ns=ts)
    meta_raw, specs_raw = chloros_read_daq(raw)
    check("raw recording asks Chloros to calibrate it",
          chloros_would_calibrate(meta_raw))

    # 2. What Chloros writes back out: the same readings, calibrated, with
    #    the flag SET and the bundle that produced them named.
    sha = "a" * 64
    exported = os.path.join(tmp, "rt_raw_calibrated.daq")
    with mm.DaqWriter(exported, product_model="daq-u", product_serial=serial,
                      tz_offset_minutes=0, calibration_applied=True,
                      calibration_bundle_sha=sha,
                      calibration_completed_utc="2026-01-02T03:04:05+00:00",
                      cap_applied=False) as w:
        for ts, (_, spec, _, _) in zip(stamps, specs_raw):
            w.write(spec * gain, is_saturated=False, integration_time_ms=50,
                    timestamp_ns=ts)

    meta_exp, specs_exp = chloros_read_daq(exported)
    check("export declares itself calibrated",
          meta_exp['calibration_applied'] is True)
    check("export names the bundle that produced it",
          meta_exp['calibration_bundle_sha'] == sha)
    check("re-importing the export does NOT calibrate it again",
          not chloros_would_calibrate(meta_exp))
    check("export carries the RECORDING's timezone, not the host's",
          meta_exp['utc_offset_minutes'] == 0,
          f"got {meta_exp['utc_offset_minutes']}")
    check("readings survive the round-trip",
          len(specs_exp) == len(specs_raw)
          and all(a[0] == b[0] for a, b in zip(specs_exp, specs_raw))
          and all(float(a[1][0]) == float(b[1][0]) * gain
                  for a, b in zip(specs_exp, specs_raw)))

    # 3. Negative control. The same calibrated numbers with the flag CLEAR is
    #    what a naive export writes -- and it is indistinguishable from a raw
    #    recording, so import applies the bundle again: gain^2, silently.
    mislabelled = os.path.join(tmp, "rt_mislabelled.daq")
    with mm.DaqWriter(mislabelled, product_model="daq-u",
                      product_serial=serial, tz_offset_minutes=0) as w:
        for ts, (_, spec, _, _) in zip(stamps, specs_raw):
            w.write(spec * gain, is_saturated=False, integration_time_ms=50,
                    timestamp_ns=ts)
    meta_bad, specs_bad = chloros_read_daq(mislabelled)
    check("a calibrated file with the flag CLEAR would be calibrated twice",
          chloros_would_calibrate(meta_bad),
          "this is the failure the flag exists to prevent")
    twice = float(specs_bad[0][1][0]) * gain
    check("...which would be a %gx error" % gain,
          abs(twice - float(counts[0]) * gain * gain) < 1e-3,
          f"{counts[0]:g} counts -> {twice:g} instead of "
          f"{counts[0] * gain:g} W/m^2/nm")

    # 4. DaqWriter refuses to write an unauditable calibrated recording.
    try:
        mm.DaqWriter(os.path.join(tmp, "rt_nosha.daq"), product_model="daq-u",
                     product_serial=serial, calibration_applied=True)
        ok = False
    except ValueError:
        ok = True
    check("calibration_applied=1 without a bundle sha is refused", ok)

    # 5. Cross-check the rule above against the REAL Chloros reader, when a
    #    checkout is reachable. The model in this file is a copy, and a copy
    #    can drift; this is what catches that.
    repo = _chloros_repo()
    if repo is None:
        skip("live cross-check against mip.daq_dls",
             "set CHLOROS_REPO to a mapirlab checkout")
        return
    saved = list(sys.path)
    try:
        sys.path.insert(0, repo)
        from mip.daq_dls import load_calibrated       # noqa: E402
        scan = load_calibrated(exported)
        check("real Chloros reader: export reads back as calibrated",
              scan is not None and scan.calibrated is True)
        check("real Chloros reader: values unchanged (no second application)",
              scan is not None
              and abs(float(scan.spectra[0][0])
                      - float(counts[0]) * gain) < 1e-3,
              None if scan is None else f"{float(scan.spectra[0][0]):g}")
        check("real Chloros reader: bundle sha survives",
              scan is not None and scan.bundle_sha == sha)
        # The mislabelled file must NOT be taken at face value. What that
        # looks like depends on whether this machine can reach a bundle for
        # the serial, and BOTH outcomes are the correct behaviour:
        #   bundle available   -> it is applied again; the values move (the
        #                         squaring this whole check is about) and the
        #                         sha reported is the fetched bundle's, never
        #                         the one the file failed to declare.
        #   bundle unavailable -> calibrated=False; the numbers pass through
        #                         but are flagged as un-calibrated counts.
        # Asserting either one alone would make this check machine-dependent.
        bad = load_calibrated(mislabelled)
        want = float(counts[0]) * gain
        got = None if bad is None else float(bad.spectra[0][0])
        trusted = (bad is not None and bad.calibrated
                   and bad.bundle_sha == sha and abs(got - want) < 1e-3)
        if bad is not None and bad.calibrated:
            detail = (f"a bundle was reachable, so it was applied AGAIN: "
                      f"{want:g} -> {got:g} W/m^2/nm ({got / want:.3g}x)")
        else:
            detail = "no bundle reachable, so it is flagged as raw counts"
        check("real Chloros reader: the mislabelled file is NOT trusted "
              "as calibrated", not trusted, detail)
    except ImportError as e:
        skip("live cross-check against mip.daq_dls", f"import failed: {e}")
    finally:
        sys.path[:] = saved


def test_multicast_stream():
    """daq_stream: v1/v2 datagram decode and multi-sensor separation.

    The reason v2 exists is that v1 carried no sensor identity, so two units
    on one group were separable only by UDP source address -- and a receiver
    that didn't filter read a ~50/50 blend of both while looking healthy
    (hardware, 2026-07-14).
    """
    print("\n-- multicast stream (daq_stream) --")
    import daq_stream as ds

    def frame(ver=2, *, mac=b"\xaa\xbb\xcc\xdd\xee\xff",
              serial=b"\x11\x22\x33\x44\x55", cal=False, seq=3, n=4,
              integ=50, model=0, corrupt=False, imu=False):
        if cal:
            payload = struct.pack("<%df" % n, *[1.5] * n)
            flags = 0x02 | 0x08
        else:
            payload = (b"\x03\xbb\x28\x00" + struct.pack("<H", integ)
                       + b"\x00\x00" + struct.pack("<I", n)
                       + struct.pack("<%df" % n, *[100.0] * n))
            flags = 0x02
        if ver == 1:
            d = bytearray(18)
            d[0:2] = b"\xda\x0e"; d[2] = 1; d[3] = flags
            struct.pack_into("<I", d, 4, seq); struct.pack_into("<Q", d, 8, 7)
            struct.pack_into("<H", d, 16, len(payload))
        else:
            d = bytearray(32)
            d[0:2] = b"\xda\x0e"; d[2] = 2; d[3] = flags
            struct.pack_into("<I", d, 4, seq); struct.pack_into("<Q", d, 8, 7)
            d[16:22] = mac; d[22:27] = serial; d[27] = model
            struct.pack_into("<H", d, 28, integ)
            struct.pack_into("<H", d, 30, len(payload))
        if imu:
            d[3] |= 0x10          # FLAG_IMU, firmware >= 1.8.0
        d += payload
        crc = ds._crc16_ccitt(bytes(d)) ^ (0xFFFF if corrupt else 0)
        d += struct.pack("<H", crc)
        if imu:
            # A REAL 22-byte trailer, not 22 zero bytes: has_imu means the
            # trailer DECODED, so a placeholder would test the failure path
            # while looking like it tested the success one. Offsets are wire
            # facts from PROTOCOL.md, written out rather than read from
            # daq_stream so this builder still produces a genuine DAQ-E-S
            # frame if that module's constants are wrong or absent.
            t = bytearray(22)
            t[0] = 0x01                                    # trailer version
            t[1] = 0x01 | 0x02 | 0x04 | 0x08 | 0x10        # fresh|angles|cal|healthy|temp
            struct.pack_into("<H", t, 2, 12)               # sample_age_ms
            struct.pack_into("<hhh", t, 4, 10, -5, 1000)   # x/y/z mg
            struct.pack_into("<H", t, 10, 250)             # tilt  2.50 deg
            struct.pack_into("<h", t, 12, -125)            # roll -1.25 deg
            struct.pack_into("<h", t, 14, 75)              # pitch 0.75 deg
            struct.pack_into("<H", t, 16, 1001)            # |a| mg
            struct.pack_into("<b", t, 18, 7)               # temp trend
            struct.pack_into("<H", t, 20, ds._crc16_ccitt(bytes(t[:20])))
            d += bytes(t)                     # trailer sits AFTER the crc
        return bytes(d)

    f = ds.parse_datagram(frame(2), "10.0.0.5")
    check("v2 carries the sensor serial", f["sensor_serial"] == "11-22-33-44-55")
    check("v2 carries the model", f["model"] == "daq-e")
    check("v2 carries this frame's integration time",
          f["integration_time_ms"] == 50)
    check("v2 keys on serial, not source IP", f["device_key"] == "11-22-33-44-55")
    check("raw spectrum decoded", f["spectrum"] == [100.0] * 4)
    check("model byte 1 = daq-e-s",
          ds.parse_datagram(frame(2, model=1), "x")["model"] == "daq-e-s")

    c = ds.parse_datagram(frame(2, cal=True), "x")
    check("calibrated flag set", c["calibrated"])
    check("calibrated payload is float32 irradiance",
          c["spectrum"] == [1.5] * 4)

    # Firmware >= 1.8.0 (every DAQ-E-S) appends a 22-byte IMU trailer AFTER
    # the crc. An exact-length check rejects those frames wholesale -- the
    # sensor streams perfectly and reads as absent, its frames counted as
    # malformed. Chloros hit exactly this and moved to a lower bound on
    # 2026-08-18; the same rule is required here.
    with_imu = frame(2, model=1, imu=True)
    fi = ds.parse_datagram(with_imu, "10.0.0.5")
    check("a DAQ-E-S frame (IMU trailer after the crc) still parses",
          fi is not None,
          "exact-length check would drop every fw>=1.8.0 frame")
    check("...and its spectrum is intact",
          fi is not None and fi["spectrum"] == [100.0] * 4)
    check("...and the trailer's presence is reported",
          fi is not None and fi.get("has_imu") is True)
    check("...and the attitude decodes",
          fi is not None and fi.get("imu") is not None
          and fi["imu"]["tilt_deg"] == 2.5
          and fi["imu"]["roll_deg"] == -1.25
          and fi["imu"]["pitch_deg"] == 0.75
          and fi["imu"]["cal_applied"] is True
          and fi["imu"]["temp_raw"] == 7,
          None if fi is None else str(fi.get("imu"))[:70])
    # A trailer whose crc does not check out must read as "no IMU on this
    # frame", never as a tilt of whatever the corrupt bytes happened to say.
    _bad = bytearray(with_imu)
    _bad[-1] ^= 0xFF
    _g = ds.parse_datagram(bytes(_bad), "10.0.0.5")
    check("a corrupt trailer degrades to no-IMU, keeping the spectrum",
          _g is not None and _g["spectrum"] == [100.0] * 4
          and _g["imu"] is None and _g["has_imu"] is False)
    check("a frame with no trailer reports none",
          (ds.parse_datagram(frame(2), "x") or {}).get("has_imu") is False)
    check("a TRUNCATED trailer does not cost the spectrum",
          (lambda g: g is not None and g["spectrum"] == [100.0] * 4
           and g["has_imu"] is False)(
              ds.parse_datagram(with_imu[:-4], "10.0.0.5")),
          "the trailer rides outside the crc so a bad one must not "
          "invalidate the frame")

    v1 = ds.parse_datagram(frame(1), "10.0.0.5")
    check("v1 still decodes (legacy units keep working)", v1["version"] == 1)
    check("v1 has no identity, falls back to source IP",
          v1["sensor_serial"] is None and v1["device_key"] == "10.0.0.5")

    check("corrupt CRC rejected",
          ds.parse_datagram(frame(2, corrupt=True), "x") is None)
    check("bad magic rejected",
          ds.parse_datagram(b"\x00\x00" + frame(2)[2:], "x") is None)
    check("unknown version rejected",
          ds.parse_datagram(frame(2)[:2] + b"\x09" + frame(2)[3:], "x") is None)
    truncated = frame(2)[:-1]
    check("truncated datagram rejected",
          ds.parse_datagram(truncated, "x") is None)

    # The 2026-07-14 failure, as a test: two units, one group, no blending.
    a_ser, b_ser = b"\x11\x22\x33\x44\x55", b"\x66\x77\x88\x99\xaa"
    seen = {}
    for i in range(6):
        for ser, integ in ((a_ser, 50), (b_ser, 200)):
            fr = ds.parse_datagram(
                frame(2, serial=ser, seq=i, integ=integ), "10.0.0.9")
            seen.setdefault(fr["device_key"], []).append(
                fr["integration_time_ms"])
    check("two sensors on one group separate cleanly", len(seen) == 2)
    check("no interleaving between sensors",
          all(len(set(v)) == 1 for v in seen.values()))

    # Dual-stream labelling. Counts and W/m^2/nm differ by ~4 orders of
    # magnitude, so a CSV that does not record which one it holds is
    # indistinguishable from the other on inspection -- and there is no
    # scale at which the mistake announces itself.
    import csv as _csv

    def _stream_csv(path):
        """(provenance_line, column_row, first_data_row) from a stream CSV.

        Located by CONTENT, not by row index: a file written without the
        provenance line still parses here, so the checks below report what is
        missing instead of dying with an IndexError three frames deep.
        """
        rows = list(_csv.reader(open(path, newline="", encoding="utf-8")))
        prov = rows[0][0] if rows and rows[0] and rows[0][0].startswith("#")             else ""
        ci = next((i for i, r in enumerate(rows) if r and r[0] == "device"), -1)
        cols = rows[ci] if ci >= 0 else []
        data = rows[ci + 1] if 0 <= ci < len(rows) - 1 else []
        return prov, cols, data

    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_selftest_out")
    os.makedirs(tmp, exist_ok=True)
    for cal_mode in (False, True):
        out = os.path.join(tmp, f"stream_{'cal' if cal_mode else 'raw'}.csv")
        argv = ["--count", "1", "--csv", out, "--quiet",
                "--duration", "5"] + (["--calibrated"] if cal_mode else [])
        payload = frame(2, cal=cal_mode)

        class _Sock:
            def __init__(self): self.n = 0
            def setsockopt(self, *a): pass
            def setblocking(self, *a): pass
            def settimeout(self, *a): pass
            def bind(self, *a): pass
            def close(self): pass
            def recvfrom(self, _n):
                self.n += 1
                return payload, ("10.0.0.9", 5002)

        real_socket = ds.socket.socket
        ds.socket.socket = lambda *a, **k: _Sock()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ds.main(argv)
        finally:
            ds.socket.socket = real_socket

        head, cols, data = _stream_csv(out)
        want = "calibrated W/m^2/nm" if cal_mode else "raw counts"
        label = "calibrated" if cal_mode else "raw"
        check(f"{label} CSV names its stream in the file", want in head, head)
        labelled = "calibrated" in cols and "units" in cols
        check(f"{label} CSV has a per-frame calibrated column", labelled,
              "columns: %s" % (cols or "(none found)"))
        check(f"{label} CSV row states its units",
              labelled and data[cols.index("units")]
              == ("W/m^2/nm" if cal_mode else "counts"),
              data[cols.index("units")] if labelled else "no units column")
        check(f"{label} CSV calibrated flag comes from the FRAME",
              labelled and data[cols.index("calibrated")] == str(int(cal_mode)),
              None if labelled else "no calibrated column")

    # A frame whose flag contradicts the group it arrived on is a firmware
    # bug, and silently trusting the group would misread counts as irradiance
    # (or the reverse) by ~1e4. Chloros drops such a frame outright; this
    # script keeps it but must SAY so, and must record the frame's own flag.
    payload = frame(2, cal=True)          # calibrated frame...

    class _Sock2:
        def setsockopt(self, *a): pass
        def setblocking(self, *a): pass
        def settimeout(self, *a): pass
        def bind(self, *a): pass
        def close(self): pass
        def recvfrom(self, _n): return payload, ("10.0.0.9", 5002)

    out = os.path.join(tmp, "stream_mismatch.csv")
    real_socket = ds.socket.socket
    ds.socket.socket = lambda *a, **k: _Sock2()
    err = io.StringIO()
    try:
        with contextlib.redirect_stdout(io.StringIO()),                 contextlib.redirect_stderr(err):
            ds.main(["--count", "1", "--csv", out, "--quiet",
                     "--duration", "5"])     # ...on the RAW subscription
    finally:
        ds.socket.socket = real_socket

    check("a calibrated frame on the raw group is reported",
          "WARNING" in err.getvalue() and "raw group" in err.getvalue(),
          err.getvalue().strip()[:90] or "(nothing on stderr)")
    _, cols, data = _stream_csv(out)
    labelled = "calibrated" in cols and "units" in cols
    check("...and the CSV records the FRAME's flag, not the group's",
          labelled and data[cols.index("calibrated")] == "1"
          and data[cols.index("units")] == "W/m^2/nm",
          None if labelled else "the CSV has no calibrated/units column")


def main():
    test_metadata_contract()
    test_wire_codec()
    test_record_daq_runs()
    test_disk_stall()
    test_camera_config()
    test_cable_sync_ordering()
    test_camera_capture_flow()
    test_camera_frame_metadata()
    test_shared_link()
    test_link_pacing()
    test_offline_calibration()
    test_cap_declaration()
    test_set_device_cap()
    test_stream_recording()
    test_stream_identity()
    test_export_round_trip()
    test_multicast_stream()
    n = sum(RESULTS)
    print(f"\n==== {n}/{len(RESULTS)} checks passed ====")
    if SKIPPED:
        print(f"     ({len(SKIPPED)} optional check group(s) skipped:")
        for name, reason in SKIPPED:
            print(f"        {name} -- {reason}")
        print("     )")
    return 0 if n == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
