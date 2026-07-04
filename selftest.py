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
    SKIPPED.append(name)
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
                              "calibration_applied", "utc_offset_minutes")
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
            'utc_offset_minutes': m.get('utc_offset_minutes')}, specs


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


def main():
    test_metadata_contract()
    test_wire_codec()
    test_camera_config()
    test_cable_sync_ordering()
    test_camera_capture_flow()
    n = sum(RESULTS)
    print(f"\n==== {n}/{len(RESULTS)} checks passed ====")
    if SKIPPED:
        print(f"     ({len(SKIPPED)} TIFF read-back check group(s) skipped -- "
              f"`pip install tifffile` to run the full self-test)")
    return 0 if n == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
