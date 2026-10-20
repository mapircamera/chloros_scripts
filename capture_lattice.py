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
import math
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import numpy as np

from mapir_metadata import lattice_capture_filename, write_lattice_raw_tiff

SYNC_LINE = "Line2"  # MAPIR M8 sync cable: pin 2 (brown) = Line2 on every cam

# Slave trigger-input deglitch, microseconds -- the same filter Chloros
# applies (lattice_cam/multi.py). See LatticeCamera.configure_slave.
TRIGGER_DEGLITCH_US = 5.0


class IncompleteFrame(Exception):
    """A frame arrived with packets missing. It is never written: on a
    shared 1 GbE link a third of such a frame's rows came back zero."""


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
        self._warned_no_chunks = False

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
    def identify(self, model_fallback=None):
        """Read serial (cal key) + model from the camera. The MAPIR model
        string lives in DeviceUserID (e.g. 'M3C-L41-FRGN'); we prefix 'LATT-'
        for the EXIF Model tag Chloros keys on.

        ``model_fallback`` (the --model flag) is used ONLY when the camera's
        own DeviceUserID is empty -- a factory-reset unit. It is deliberately
        not an override: in a mixed array the cameras that still know their
        own model must keep reporting it, or the wrong calibration gets
        stamped on frames that were perfectly self-describing.
        """
        self.serial = str(self._get("DeviceSerialNumber")).strip()
        uid = ""
        try:
            uid = str(self._get("DeviceUserID") or "").strip()
        except Exception:
            pass
        if not uid:
            uid = str(model_fallback or "").strip()
            if not uid:
                raise RuntimeError(
                    f"camera {self.serial}: DeviceUserID is empty (was it "
                    f"factory reset?). Pass the model with "
                    f"--model LATT-M3C-L41-FRGN.")
            print(f"  [{self.serial}] DeviceUserID empty; using --model "
                  f"{uid}", flush=True)
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
        self.enable_frame_chunks()

    def enable_frame_chunks(self):
        """Have the camera attach each frame's OWN exposure and gain to it
        (GenICam chunk data), for the EXIF Chloros calibrates from.

        The ExposureTime node is no substitute: GenICam serves a node's
        cached value, so under auto-exposure every frame's EXIF said 1851 us
        while the frames were really exposed at 1603 -> 2299 us (Jetson rig,
        2026-10-16) -- and radiance is counts divided by that exposure. Must
        run before streaming starts, since it changes the payload size.
        """
        self._try_set("ChunkModeActive", True)
        for what in ("ExposureTime", "Gain"):
            if self._try_set("ChunkSelector", what):
                self._try_set("ChunkEnable", True)

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

    def configure_slave(self, line=SYNC_LINE, deglitch_us=TRIGGER_DEGLITCH_US):
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
        #  4. Deglitch the trigger input. The shared M8 line chatters while
        #     the master holds it, and an unfiltered slave fires on every
        #     glitch: one delivered 10 frames in 5 s with no trigger sent,
        #     and the extra frames flooded a 1 GbE link (Jetson rig,
        #     2026-10-19). Chloros applies the same filter; 5 us is ~10x the
        #     chatter and far shorter than any real exposure pulse. The
        #     firmware only takes it on an input, so it follows LineMode.
        self._set("TriggerMode", "Off")
        self._set("LineSelector", line)
        self._set("LineMode", "Input")
        self._set("LineSource", "ExposureActive")     # bias quirk (#3)
        if deglitch_us and deglitch_us > 0:           # (#4)
            if not (self._try_set("LineFilterSelector", "Deglitch")
                    and self._try_set("LineFilterWidth", float(deglitch_us))):
                print(f"  [{self.serial}] note: no trigger-input deglitch on "
                      f"this firmware; the slave may fire on M8 line chatter",
                      flush=True)
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

    def wait_armed(self, timeout_s=2.0):
        """Wait until the camera reports TriggerArmed.

        A trigger that arrives before then is DROPPED, not queued: one camera
        in software mode lost every other frame this way (Jetson rig,
        2026-10-16). Returns False on timeout, or at once on firmware without
        the node; the caller triggers anyway and the grab reports any loss.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if self._get("TriggerArmed"):
                    return True
            except Exception:
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.001)

    def trigger(self, timeout_s=2.0):
        self.wait_armed(timeout_s)
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

    def frame_exposure_iso(self, info=None):
        """Exposure (s) + an ISO equivalent for EXIF, for ONE frame.

        Taken from the frame's own chunk data (``info``, from grab_raw)
        whenever the camera attached it. Otherwise the live nodes, re-read
        from the camera rather than GenICam's cache -- and say so once,
        because under auto-exposure a live read can already describe the
        NEXT frame.
        """
        info = info or {}
        exp_us, gain_db = info.get("exposure_us"), info.get("gain_db")
        if exp_us is None or gain_db is None:
            if not self._warned_no_chunks:
                self._warned_no_chunks = True
                print(f"  [{self.serial}] note: no chunk data on this frame; "
                      f"EXIF exposure/gain read live from the camera (may be "
                      f"one auto-exposure step off)", flush=True)
            try:
                self.dev.nodemap.invalidate_nodes()
            except Exception:
                pass
            if exp_us is None:
                try:
                    exp_us = float(self._get("ExposureTime"))
                except Exception:
                    exp_us = 5000.0
            if gain_db is None:
                try:
                    gain_db = float(self._get("Gain"))
                except Exception:
                    gain_db = 0.0
        iso = max(1, int(round(100 * (10 ** (gain_db / 20.0)))))
        return exp_us / 1e6, iso

    def grab_raw(self, timeout_ms=2000):
        """Return (frame, info): the raw plane, and this frame's frame_id plus
        -- when chunk data is on -- its own exposure_us and gain_db.

        Raises IncompleteFrame, after requeueing the buffer, when packets
        were lost: the SDK still hands such a frame over, with the missing
        rows left zero, and a raw capture must never save it as good data.
        """
        buf = self.dev.get_buffer(timeout=timeout_ms)
        if getattr(buf, "is_incomplete", False):
            frame_id = getattr(buf, "frame_id", None)
            self.dev.requeue_buffer(buf)
            raise IncompleteFrame(f"frame {frame_id}: packets lost in transit")
        try:
            arr = buffer_to_numpy(buf)
            info = {"frame_id": getattr(buf, "frame_id", None)}
            try:
                # Read before requeue: the chunk lives in this buffer.
                ch = buf.get_chunk(["ChunkExposureTime", "ChunkGain"])
                info["exposure_us"] = float(ch["ChunkExposureTime"].value)
                info["gain_db"] = float(ch["ChunkGain"].value)
            except Exception:
                pass
        finally:
            self.dev.requeue_buffer(buf)
        return arr, info

    def discard_pending(self, timeout_ms=2000):
        """Throw away a frame that may still arrive for a shot we gave up on.

        A trigger whose reply was lost may still have fired. Left in the
        queue, that frame would be grabbed by the NEXT shot and saved under
        its timestamp, and every shot after it would be off by one.
        """
        try:
            self.dev.requeue_buffer(self.dev.get_buffer(timeout=timeout_ms))
            return True
        except Exception:
            return False


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


def open_cameras(system, wanted_serials=None, model_fallback=None):
    infos = system.device_infos
    if not infos:
        raise SystemExit("No cameras found. Check power, cabling, and that the "
                         "host NIC is on the cameras' subnet.")
    devices = system.create_device()
    cams = []
    for dev in devices:
        cam = LatticeCamera(dev)
        cam.identify(model_fallback)
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
def assign_roles(cams, sync, master_serial, deglitch_us=TRIGGER_DEGLITCH_US):
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
            s.configure_slave(deglitch_us=deglitch_us)
        return [master] + slaves, "cable"
    # software / single
    for c in cams:
        c.configure_software_trigger()
    if len(cams) > 1:
        print("WARNING: software sync -- multiple cameras are triggered in "
              "software and are NOT hardware-synchronized. Use --sync cable "
              "with M8 sync cables for simultaneous exposure.", flush=True)
    return cams, "software"


def auto_stagger_ms(cams, margin=1.21, efficiency=0.94):
    """Per-camera transmission stagger (ms) for cameras sharing one link.

    Several cameras that send their frames at the same moment collide in the
    switch: on a 1 GbE uplink about half the packets were lost and every
    frame arrived incomplete, at any DeviceLinkThroughputLimit (Jetson rig,
    2026-10-19). Holding camera i's frame back by i x (one frame's wire
    time) sends them one after another while the exposures stay
    simultaneous. One frame's wire time is PayloadSize over the camera's
    link speed. For a full-res 12-bit frame at 1 GbE the measured edge was
    55 ms through a jumbo-frame switch but 60 ms on a no-jumbo PoE path
    (1500-byte packets), so the margin puts the default a step above both:
    65 ms.

    The camera only knows its own (1 GbE) link, not the host's uplink, so
    this assumes the shared-1 GbE worst case. Behind a 10 GbE uplink pass
    --stagger-ms 0 for full speed. 0 for a single camera.
    """
    if len(cams) < 2:
        return 0
    wire_s = 0.0
    for c in cams:
        try:
            payload = float(c._get("PayloadSize"))
        except Exception:
            payload = 2048 * 1536 * 2
        try:
            link = float(c._get("DeviceLinkSpeed"))   # bytes/s
        except Exception:
            link = 125e6
        wire_s = max(wire_s, payload / (link * efficiency))
    return int(math.ceil(wire_s * margin * 1000))


def configure_stagger(ordered_cams, stagger_ms):
    """GevSCFTD: camera i holds each finished frame i x stagger_ms before
    sending it. Set on every camera, 0 included, so a delay left behind by
    another program cannot slow or skew this run."""
    for i, c in enumerate(ordered_cams):
        c._try_set("GevSCFTD", int(round(i * stagger_ms * 1e6)))


def configure_link_pacing(cams, packet_delay_ns=0):
    """Send each frame at full link speed, whatever pacing was left behind.

    Chloros paces an array by giving every camera an inter-packet delay
    (GevSCPD) and, to split bandwidth, a link throughput limit -- and both
    stay on the camera after Chloros disconnects. Inherited, they throttled
    this script about 5x (Jetson rig, 2026-10-20: one camera 2.66 fps at
    Chloros's 405454 ns packet delay, 12.67 fps without it). Cameras sharing
    a link are kept apart by the transmission stagger (configure_stagger),
    not by pacing, so the throughput limit goes off -- its factory default
    -- and the packet delay is set to ``packet_delay_ns``, clamped to the
    camera's range. The limit goes off first: with it on, the camera refuses
    the GevSCPD write.

    Returns one note per camera whose pacing this changed.
    """
    notes = []
    for c in cams:
        changed = []
        try:
            old_mode = str(c._get("DeviceLinkThroughputLimitMode"))
        except Exception:
            old_mode = None                     # firmware without the node
        if old_mode is not None:
            c._try_set("DeviceLinkThroughputLimitMode", "Off")
            if old_mode == "On":
                changed.append("throughput limit On -> Off")
        try:
            node = c.dev.nodemap.get_node("GevSCPD")
            old_delay = int(node.value)
        except Exception:
            continue                            # no packet delay to set
        delay = int(packet_delay_ns)
        try:
            delay = max(int(node.min), min(int(node.max), delay))
        except Exception:
            pass
        c._try_set("GevSCPD", delay)
        if old_delay != delay:
            changed.insert(0, f"packet delay {old_delay} -> {delay} ns")
        if changed:
            notes.append(f"{c.serial}: " + ", ".join(changed))
    return notes


def _fire(ordered_cams, mode, arm_s, timeout_ms):
    """Trigger one shot; return the cameras that should now deliver a frame.

    A trigger whose control packet is lost raises (SC_ERR_TIMEOUT on a
    congested link). That costs the shot, not the run -- but the command
    may still have reached the camera, so its frame is drained rather than
    left to be grabbed as the next shot's.
    """
    if mode == "cable":
        # Every slave exposes on the master's pulse, so each must be
        # armed before the master fires or it misses the edge.
        for c in ordered_cams[1:]:
            c.wait_armed(arm_s)
        try:
            ordered_cams[0].trigger(arm_s)           # master drives the array
            return list(ordered_cams)
        except Exception as e:
            print(f"  ! {ordered_cams[0].serial}: trigger failed ({e}); shot "
                  f"skipped", file=sys.stderr, flush=True)
            for c in ordered_cams:
                c.discard_pending(timeout_ms)
            return []
    fired = []
    for c in ordered_cams:
        try:
            c.trigger(arm_s)
            fired.append(c)
        except Exception as e:
            print(f"  ! {c.serial}: trigger failed ({e}); frame skipped",
                  file=sys.stderr, flush=True)
            c.discard_pending(timeout_ms)
    return fired


def capture_loop(ordered_cams, mode, args, stop):
    """Capture until frames / duration / Ctrl-C. Returns {"shots", "saved",
    "lost"}: lost counts frames that were not saved -- incomplete, timed
    out, or never triggered."""
    out_dir = args.output_dir or "."
    os.makedirs(out_dir, exist_ok=True)
    for c in ordered_cams:
        c.start()
    t0 = time.monotonic()
    seq = 1
    saved = 0
    lost = 0
    arm_s = args.timeout_ms / 1000.0
    try:
        while not stop.is_set():
            shot_time = datetime.now(timezone.utc)
            fired = _fire(ordered_cams, mode, arm_s, args.timeout_ms)
            for c in ordered_cams:
                if c not in fired:
                    lost += 1
                    continue
                try:
                    arr, info = c.grab_raw(timeout_ms=args.timeout_ms)
                except IncompleteFrame as e:
                    print(f"  ! {c.serial}: shot {seq} incomplete ({e}); not "
                          f"saved", file=sys.stderr, flush=True)
                    lost += 1
                    continue
                except Exception as e:
                    print(f"  ! {c.serial}: grab failed ({e})", file=sys.stderr,
                          flush=True)
                    lost += 1
                    continue
                exp_s, iso = c.frame_exposure_iso(info)
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
        if lost:
            print(f"  ! {lost} frame(s) NOT saved (incomplete, timed out or "
                  f"not triggered). Several cameras on one 1 GbE link need "
                  f"--stagger-ms; see the README.", file=sys.stderr, flush=True)
    return {"shots": seq - 1, "saved": saved, "lost": lost}


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
    p.add_argument("--model",
                   help="model string to fall back to for a camera whose "
                        "DeviceUserID is empty (e.g. LATT-M3C-L41-FRGN, after "
                        "a factory reset). Cameras that report their own model "
                        "keep it -- this is a fallback, not an override, so a "
                        "mixed array cannot be mislabelled.")
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
    p.add_argument("--stagger-ms", type=float, default=None,
                   help="hold camera i's frame back i x this many ms before "
                        "sending it, so frames cross a shared link one at a "
                        "time while exposures stay simultaneous. Default: "
                        "one frame's wire time at 1 GbE plus margin (65 ms full-res "
                        "12-bit) with 2+ cameras -- needed on a shared 1 GbE "
                        "switch/uplink; pass 0 behind a 10 GbE uplink for "
                        "full speed")
    p.add_argument("--deglitch-us", type=float, default=TRIGGER_DEGLITCH_US,
                   help="cable sync: slave trigger-input deglitch in "
                        f"microseconds (default {TRIGGER_DEGLITCH_US:g}, as "
                        "Chloros uses; 0 leaves the camera's own setting)")
    p.add_argument("--packet-delay-ns", type=int, default=0,
                   help="delay each camera puts between the packets of a "
                        "frame (GevSCPD, ns). Default 0: full link speed -- "
                        "the transmission stagger keeps cameras apart. Written "
                        "on every run, with the link throughput limit turned "
                        "off, so pacing another program left on a camera "
                        "(Chloros leaves its array pacing) cannot throttle "
                        "this one")
    p.add_argument("--output-dir", help="where to write TIFFs (default: .)")
    args = p.parse_args(argv)

    system = _load_system()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    wanted = [s.strip() for s in args.serials.split(",")] if args.serials else None
    cams = open_cameras(system, wanted, args.model)
    print(f"Found {len(cams)} LATTICE camera(s):")
    for c in cams:
        print(f"  {c.serial}  {c.model}  ({'mono' if c.is_mono else 'color'})")
    for c in cams:
        c.configure_raw(exposure_us=args.exposure_us)

    ordered, mode = assign_roles(cams, args.sync, args.master,
                                 deglitch_us=args.deglitch_us)
    print(f"Sync mode: {mode}"
          + (f" (master {ordered[0].serial})" if mode == "cable" else ""))
    notes = configure_link_pacing(ordered, args.packet_delay_ns)
    if notes:
        print("Cleared link pacing another program left on the camera(s):")
        for n in notes:
            print(f"  {n}")
    stagger = (args.stagger_ms if args.stagger_ms is not None
               else auto_stagger_ms(ordered))
    configure_stagger(ordered, stagger)
    if len(ordered) > 1:
        print(f"Transmission stagger: {stagger:g} ms per camera"
              + (" (auto, for a shared 1 GbE link; --stagger-ms 0 behind a "
                 "10 GbE uplink)" if args.stagger_ms is None else ""))
    print("Recording RAW. Chloros will calibrate by serial at import. Ctrl-C to stop.",
          flush=True)
    try:
        result = capture_loop(ordered, mode, args, stop)
    finally:
        try:
            system.destroy_device()
        except Exception:
            pass
    # Non-zero when any frame was lost, so a script driving this one can
    # tell a complete capture from a partial one.
    return 1 if result["lost"] else 0


if __name__ == "__main__":
    sys.exit(main())
