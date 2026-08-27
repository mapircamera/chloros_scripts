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
import io
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
                              "cap_id", "cap_applied", "utc_offset_minutes")
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
    check("daq als_meta v1.23", meta['version'] == '1.23', meta['version'])
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

    def execute(self):
        self._nm._writes.append((self._name, "<EXEC>"))


class _FakeNodemap:
    def __init__(self, vals=None, maxes=None, missing=()):
        self._vals = dict(vals or {})
        self._max = dict(maxes or {})
        self._missing = set(missing)
        self._writes = []

    def get_node(self, name):
        if name in self._missing:
            raise KeyError(name)
        return _FakeNode(self, name)


class _FakeBuffer:
    def __init__(self, arr):
        import ctypes
        self.width = arr.shape[1]
        self.height = arr.shape[0]
        self.bits_per_pixel = 16
        b = arr.astype(np.uint16).tobytes()
        self.pdata = (ctypes.c_ubyte * len(b)).from_buffer_copy(b)
        self.frame_id = 7


class _FakeDevice:
    def __init__(self, nm, arr):
        self.nodemap = nm
        self.tl_stream_nodemap = _FakeNodemap()
        self._buf = _FakeBuffer(arr)

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
                       "ExposureTime": 4000.0, "Gain": 0.0},
                      {"Width": 2048, "Height": 1536})
    cam = C.LatticeCamera(_FakeDevice(nm, img))
    cam.identify()
    arr, fid = cam.grab_raw()
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
            # 22 bytes, from PROTOCOL.md -- a wire fact, written out rather
            # than read from daq_stream, so this builder still produces a
            # real DAQ-E-S frame if that module's constant is wrong or absent.
            d += bytes(22)                     # trailer sits AFTER the crc
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
    test_camera_config()
    test_cable_sync_ordering()
    test_camera_capture_flow()
    test_offline_calibration()
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
