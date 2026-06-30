#!/usr/bin/env python3
"""
capture_lattice.py -- control + RAW capture from MAPIR LATTICE cameras
(M3C color / M3M mono) to Chloros-compatible TIFFs, with NO processing and
NO calibration.

LATTICE cameras are GigE Vision machine-vision cameras driven via the Arena SDK
and its `arena_api` Python wrapper. This script:

  * configures each camera for RAW capture (Bayer mosaic / mono, on-camera
    ISP off),
  * hardware-syncs a multi-camera array over the MAPIR M8 sync cables
    (one master drives ExposureActive on Line2; slaves trigger off it),
  * saves each frame as a raw TIFF stamped with serial + model + exposure, so
    Chloros detects it, groups the synced shot, and fetches the camera's
    factory calibration by serial at import.

It does NOT debayer, calibrate, or apply any index. Capture is yours; Chloros
does the science at import.

Requires the Arena SDK + arena_api (NOT pip-installable alone -- install the
native Arena SDK for your OS/arch first, including arm64 builds for
Jetson / Raspberry Pi). See README.

Examples
--------
    # single camera, auto-exposure, 50 frames
    python capture_lattice.py --frames 50

    # 5-camera array, hardware cable sync, camera 213602328 as master
    python capture_lattice.py --sync cable --master 213602328 --interval 1.0

    # fixed 5 ms exposure, capture until Ctrl-C
    python capture_lattice.py --exposure-us 5000
"""

import argparse
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import numpy as np

from mapir_metadata import lattice_capture_filename, write_lattice_raw_tiff

SYNC_LINE = "Line2"  # MAPIR M8 sync cable: pin 2 (brown) = Line2 on every cam


# ---------------------------------------------------------------------------
# arena_api buffer -> numpy (standard Arena pattern; copies so we can requeue)
# ---------------------------------------------------------------------------
def buffer_to_numpy(buffer):
    """Return a 2-D (H, W) array for a mono/bayer buffer. uint8 for 8-bit
    formats, uint16 for 10/12/16-bit (the raw sensor plane, NOT demosaiced)."""
    w, h, bits = buffer.width, buffer.height, buffer.bits_per_pixel
    if bits <= 8:
        data = np.ctypeslib.as_array(buffer.pdata, shape=(h * w,))
        return data.astype(np.uint8, copy=True).reshape((h, w))
    raw = np.ctypeslib.as_array(buffer.pdata, shape=(h * w * 2,))
    return raw.view(np.uint16).copy().reshape((h, w))


# ---------------------------------------------------------------------------
# One camera
# ---------------------------------------------------------------------------
class LatticeCamera:
    """Thin wrapper over an arena_api device: identity, raw config, sync
    wiring, and raw grab. All GenICam access goes through _set/_get/_exec so
    the configuration logic is unit-testable with a fake device."""

    def __init__(self, device):
        self.dev = device
        self.serial = None
        self.model = None       # "LATT-M3C-L41-FRGN"
        self.is_mono = None
        self.role = None        # 'master' / 'slave' / 'single'

    # -- node helpers --
    def _set(self, name, value):
        self.dev.nodemap.get_node(name).value = value

    def _get(self, name):
        return self.dev.nodemap.get_node(name).value

    def _exec(self, name):
        self.dev.nodemap.get_node(name).execute()

    def _try_set(self, name, value):
        try:
            self._set(name, value)
            return True
        except Exception as e:
            print(f"  [{self.serial}] note: could not set {name}={value} "
                  f"({type(e).__name__}); continuing", flush=True)
            return False

    def _set_max(self, name):
        node = self.dev.nodemap.get_node(name)
        node.value = node.max

    # -- identity --
    def identify(self):
        """Read serial (cal key) + model from the camera. The MAPIR model
        string lives in DeviceUserID (e.g. 'M3C-L41-FRGN'); we prefix 'LATT-'
        for the EXIF Model tag Chloros keys on."""
        self.serial = str(self._get("DeviceSerialNumber")).strip()
        uid = ""
        try:
            uid = str(self._get("DeviceUserID") or "").strip()
        except Exception:
            pass
        if not uid:
            raise RuntimeError(
                f"camera {self.serial}: DeviceUserID is empty (was it factory "
                f"reset?). Pass the model with --model LATT-M3C-L41-FRGN.")
        self.model = uid if uid.upper().startswith("LATT-") else "LATT-" + uid
        self.is_mono = "M3M" in self.model.upper()
        return self.serial, self.model

    # -- raw capture config --
    def configure_raw(self, exposure_us=None):
        """Set the camera up for raw scientific capture: raw pixel format,
        full sensor, on-camera ISP off, defect correction off."""
        self._set("PixelFormat", "Mono12" if self.is_mono else "BayerRG12")
        # full sensor ROI (offsets first so width/height aren't constrained)
        self._try_set("OffsetX", 0)
        self._try_set("OffsetY", 0)
        self._set_max("Width")
        self._set_max("Height")
        # On-camera ISP OFF -- keep the data linear/raw (color models only;
        # mono cameras may not expose these, hence _try_set).
        self._try_set("GammaEnable", False)
        self._try_set("LUTEnable", False)
        self._try_set("ColorTransformationEnable", False)
        # Defect correction OFF: on some camera firmware it halves the frame rate.
        self._try_set("DefectCorrectionEnable", False)
        # Exposure: auto by default, or a fixed value.
        if exposure_us is None:
            self._try_set("ExposureAuto", "Continuous")
        else:
            self._try_set("ExposureAuto", "Off")
            self._set("ExposureTime", float(exposure_us))
        self._try_set("AcquisitionMode", "Continuous")

    # -- hardware cable sync --------------------------------------------------
    # The master is software-triggered; each trigger drives an ExposureActive
    # pulse out on Line2, which the slaves take as their hardware trigger over
    # the M8 cable. Sub-frame simultaneous exposure, no PTP needed.
    def configure_master(self, line=SYNC_LINE):
        self.role = "master"
        self._set("TriggerMode", "On")
        self._set("TriggerSource", "Software")
        self._set("LineSelector", line)
        self._set("LineMode", "Output")
        self._set("LineSource", "ExposureActive")

    def configure_slave(self, line=SYNC_LINE):
        self.role = "slave"
        # IMPORTANT ordering / firmware quirks (observed on some camera firmware):
        #  1. TriggerMode must be Off while we rewrite the trigger wiring --
        #     some firmware locks the config while armed.
        #  2. The bidirectional line's mode persists in flash; if it was ever
        #     left as Output, the firmware refuses to bind TriggerSource to it.
        #     So force LineMode=Input BEFORE setting TriggerSource.
        #  3. A slave with LineSource=Off goes DEAF to the cable pulse even
        #     with LineMode=Input -- the input sensing path must be "biased"
        #     with a non-Off LineSource. Set it to ExposureActive explicitly.
        self._set("TriggerMode", "Off")
        self._set("LineSelector", line)
        self._set("LineMode", "Input")
        self._set("LineSource", "ExposureActive")     # bias quirk (#3)
        self._set("TriggerSource", line)
        self._set("TriggerActivation", "RisingEdge")
        self._try_set("TriggerOverlap", "PreviousFrame")
        self._set("TriggerMode", "On")

    def configure_software_trigger(self):
        """Single-camera / no-cable fallback: software-triggered, no GPIO.
        (Multiple cameras in this mode are NOT hardware-synced.)"""
        self.role = "single"
        self._set("TriggerMode", "On")
        self._set("TriggerSource", "Software")

    def trigger(self):
        self._exec("TriggerSoftware")

    # -- streaming + grab --
    def start(self, num_buffers=10):
        tl = self.dev.tl_stream_nodemap
        # deliver buffers in arrival order; let the SDK negotiate the largest
        # safe GigE packet size; ask for lost-packet resends.
        for node, val in (("StreamBufferHandlingMode", "OldestFirst"),
                          ("StreamAutoNegotiatePacketSize", True),
                          ("StreamPacketResendEnable", True)):
            try:
                tl.get_node(node).value = val
            except Exception:
                pass
        self.dev.start_stream(num_buffers)

    def stop(self):
        try:
            self.dev.stop_stream()
        except Exception:
            pass

    def frame_exposure_iso(self):
        """Read the exposure (s) + an ISO equivalent for EXIF. With auto
        exposure this is the current node value (<= 1 frame stale)."""
        exp_us, iso = 5000.0, 100
        try:
            exp_us = float(self._get("ExposureTime"))
        except Exception:
            pass
        try:
            gain_db = float(self._get("Gain"))
            iso = max(1, int(round(100 * (10 ** (gain_db / 20.0)))))
        except Exception:
            pass
        return exp_us / 1e6, iso

    def grab_raw(self, timeout_ms=2000):
        buf = self.dev.get_buffer(timeout=timeout_ms)
        try:
            arr = buffer_to_numpy(buf)
            frame_id = getattr(buf, "frame_id", None)
        finally:
            self.dev.requeue_buffer(buf)
        return arr, frame_id


# ---------------------------------------------------------------------------
# Arena SDK plumbing (kept out of the testable logic above)
# ---------------------------------------------------------------------------
def _load_system():
    try:
        from arena_api.system import system
        return system
    except ImportError as e:
        raise SystemExit(
            "arena_api is not installed. Install the Arena SDK for "
            "your OS/arch (incl. arm64 for Jetson/Raspberry Pi) and its Python "
            "wrapper, then retry. See README.") from e


def open_cameras(system, wanted_serials=None):
    infos = system.device_infos
    if not infos:
        raise SystemExit("No cameras found. Check power, cabling, and that the "
                         "host NIC is on the cameras' subnet.")
    devices = system.create_device()
    cams = []
    for dev in devices:
        cam = LatticeCamera(dev)
        cam.identify()
        cams.append(cam)
    if wanted_serials:
        wanted = set(wanted_serials)
        cams = [c for c in cams if c.serial in wanted]
        missing = wanted - {c.serial for c in cams}
        if missing:
            raise SystemExit(f"requested serials not found: {sorted(missing)}")
    return cams


# ---------------------------------------------------------------------------
# Capture orchestration
# ---------------------------------------------------------------------------
def assign_roles(cams, sync, master_serial):
    """Return (ordered_cams, mode) and apply the sync wiring. ordered_cams has
    the master first so we trigger it before grabbing the array."""
    if sync == "cable" and len(cams) > 1:
        if master_serial:
            masters = [c for c in cams if c.serial == master_serial]
            if not masters:
                raise SystemExit(f"--master {master_serial} not among connected "
                                 f"cameras {[c.serial for c in cams]}")
            master = masters[0]
        else:
            master = sorted(cams, key=lambda c: c.serial)[0]
            print(f"No --master given; using lowest serial {master.serial} as "
                  f"master.", flush=True)
        slaves = [c for c in cams if c is not master]
        master.configure_master()
        for s in slaves:
            s.configure_slave()
        return [master] + slaves, "cable"
    # software / single
    for c in cams:
        c.configure_software_trigger()
    if len(cams) > 1:
        print("WARNING: software sync -- multiple cameras are triggered in "
              "software and are NOT hardware-synchronized. Use --sync cable "
              "with M8 sync cables for simultaneous exposure.", flush=True)
    return cams, "software"


def capture_loop(ordered_cams, mode, args, stop):
    out_dir = args.output_dir or "."
    os.makedirs(out_dir, exist_ok=True)
    for c in ordered_cams:
        c.start()
    t0 = time.monotonic()
    seq = 1
    saved = 0
    try:
        while not stop.is_set():
            shot_time = datetime.now(timezone.utc)
            if mode == "cable":
                ordered_cams[0].trigger()            # master drives the array
            else:
                for c in ordered_cams:
                    c.trigger()
            for c in ordered_cams:
                try:
                    arr, _ = c.grab_raw(timeout_ms=args.timeout_ms)
                except Exception as e:
                    print(f"  ! {c.serial}: grab failed ({e})", file=sys.stderr,
                          flush=True)
                    continue
                exp_s, iso = c.frame_exposure_iso()
                fn = lattice_capture_filename(c.serial, seq, when=shot_time)
                write_lattice_raw_tiff(os.path.join(out_dir, fn), arr,
                                       model=c.model, serial=c.serial,
                                       exposure_s=exp_s, iso=iso, when=shot_time)
                saved += 1
            if seq % 10 == 0:
                print(f"  {seq} shots ({saved} frames) ...", flush=True)
            seq += 1
            if args.frames and (seq - 1) >= args.frames:
                break
            if args.duration and (time.monotonic() - t0) >= args.duration:
                break
            if args.interval:
                # space shots out; honor Ctrl-C during the wait
                end = time.monotonic() + args.interval
                while time.monotonic() < end and not stop.is_set():
                    time.sleep(min(0.05, end - time.monotonic()))
    finally:
        for c in ordered_cams:
            c.stop()
        print(f"Stopped. Saved {saved} frames across {seq - 1} shots to "
              f"{os.path.abspath(out_dir)}")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Capture RAW frames from MAPIR LATTICE cameras to "
                    "Chloros-compatible TIFFs (no SDK lock-in, no calibration).")
    p.add_argument("--sync", choices=["cable", "software"], default="cable",
                   help="multi-camera sync: 'cable' = hardware M8 trigger "
                        "(default), 'software' = single cam / no-cable fallback")
    p.add_argument("--master", help="serial of the master camera (cable sync)")
    p.add_argument("--serials", help="comma-separated serials to use "
                                     "(default: all connected)")
    p.add_argument("--exposure-us", type=float,
                   help="fixed exposure in microseconds (default: auto-exposure)")
    p.add_argument("--frames", type=int, default=0,
                   help="stop after N shots (0 = until Ctrl-C)")
    p.add_argument("--duration", type=float, default=0.0,
                   help="stop after N seconds (0 = until Ctrl-C)")
    p.add_argument("--interval", type=float, default=0.0,
                   help="seconds between shots (0 = as fast as possible)")
    p.add_argument("--timeout-ms", type=int, default=2000,
                   help="per-frame grab timeout in ms (default 2000)")
    p.add_argument("--output-dir", help="where to write TIFFs (default: .)")
    args = p.parse_args(argv)

    system = _load_system()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    wanted = [s.strip() for s in args.serials.split(",")] if args.serials else None
    cams = open_cameras(system, wanted)
    print(f"Found {len(cams)} LATTICE camera(s):")
    for c in cams:
        print(f"  {c.serial}  {c.model}  ({'mono' if c.is_mono else 'color'})")
    for c in cams:
        c.configure_raw(exposure_us=args.exposure_us)

    ordered, mode = assign_roles(cams, args.sync, args.master)
    print(f"Sync mode: {mode}"
          + (f" (master {ordered[0].serial})" if mode == "cable" else ""))
    print("Recording RAW. Chloros will calibrate by serial at import. Ctrl-C to stop.",
          flush=True)
    try:
        capture_loop(ordered, mode, args, stop)
    finally:
        try:
            system.destroy_device()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
