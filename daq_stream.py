#!/usr/bin/env python3
"""
daq_stream.py -- listen to any number of MAPIR DAQ-E / DAQ-E-S sensors at once.

The DAQ-E publishes spectra to UDP multicast, so unlike the raw TCP bridge
(one client at a time, which is what record_daq.py uses) any number of
consumers can read the same sensor simultaneously -- and any number of sensors
can share one group.

    # everything on the network, raw
    python daq_stream.py

    # calibrated irradiance, if the units carry coefficients
    python daq_stream.py --calibrated

    # one specific unit, to CSV
    python daq_stream.py --serial 11-22-33-44-55 --csv out.csv

Two groups:

    raw         239.10.10.10:5002    sensor counts, always emitted
    calibrated  239.10.10.11:5003    W/m^2/nm, emitted when the device has
                                     coefficients (a DAQ-E-S always does)

Telling sensors apart
---------------------
Datagram v2 (firmware 1.7.0+) carries the sender's MAC, sensor serial, model
and per-frame integration time, so frames are self-describing and this script
demultiplexes on identity.

Older firmware emits v1, which carries none of that. Two v1 units on one group
are separable only by UDP source address -- and a receiver that doesn't filter
reads a ~50/50 blend of both while looking healthy (that is not hypothetical;
it happened on hardware on 2026-07-14). This script therefore keys v1 frames on
source IP and says so, but the real fix is to update the firmware.

Timestamps
----------
``timestamp_us`` is latched on the ESP32 as the sensor's last byte arrives, so
it excludes network and OS jitter. When flags bit1 and bit2 are both set the
clock is PTP-disciplined and frames from different sensors are directly
comparable -- typically ~50 us on this hardware. That is what makes multi-sensor
and sensor-to-LATTICE alignment meaningful; without PTP the stamps are only as
good as each device's own boot clock.
"""

from __future__ import annotations

import argparse
import csv
import socket
import struct
import sys
import time
from typing import Dict, Optional

DEFAULT_RAW_GROUP = "239.10.10.10"
DEFAULT_RAW_PORT = 5002
DEFAULT_CAL_GROUP = "239.10.10.11"
DEFAULT_CAL_PORT = 5003

MAGIC = b"\xda\x0e"
V1, V2 = 0x01, 0x02
V1_HEADER, V2_HEADER = 18, 32
FLAG_SATURATED = 0x01
FLAG_ABS_TS = 0x02
FLAG_PTP = 0x04
FLAG_CALIBRATED = 0x08
# Firmware >= 1.8.0 appends a 22-byte IMU trailer AFTER the crc, announced by
# this bit. It rides outside the crc precisely so a bad trailer cannot cost a
# good spectrum -- which also means a reader must tolerate the extra bytes.
FLAG_IMU = 0x10
IMU_TRAILER_LEN = 22
IMU_TRAILER_VERSION = 0x01

# Standalone attitude stream (firmware >= 1.12) -- its own group, its own
# rate. The trailer above rides on spectral datagrams, so attitude was only
# ever delivered at the SPECTRAL rate (~2.5 Hz) while the accelerometer
# sampled at 50 Hz and 19 of every 20 samples were thrown away. This is
# those samples.
DEFAULT_IMU_GROUP = "239.10.10.12"
DEFAULT_IMU_PORT = 5004
IMU_DGRAM_VERSION = 0x03
IMU_DGRAM_LEN = 58

# imu_flags bits (firmware main.cpp)
IMU_TF_FRESH = 0x01
IMU_TF_ANGLES_VALID = 0x02
IMU_TF_CAL_APPLIED = 0x04
IMU_TF_HEALTHY = 0x08
IMU_TF_TEMP_VALID = 0x10
IMU_TF_TARE_SET = 0x20
IMU_TF_TARE_EXCEEDED = 0x40
IMU_TF_TEMPCO_APPLIED = 0x80

# "the firmware wrote nothing meaningful here"
CDEG_U16_INVALID = 0xFFFF
CDEG_I16_INVALID = 0x7FFF
TEMP_I16_INVALID = -0x8000


def _crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def parse_imu_trailer(data):
    """Decode the 22-byte IMU trailer, or None if absent/corrupt.

    ``data`` is the trailer alone. A garbled one degrades to "no IMU on this
    frame" rather than poisoning a tilt record -- which is the whole reason it
    rides outside the spectral crc.

    ``x/y/z_mg`` are the DIFFUSER frame when ``cal_applied`` is set and the raw
    board frame otherwise: the firmware bias-subtracts, scales and rotates
    before filling the trailer. Do not assume the axes mean the same thing on
    every unit.

    Field for field the same decode as Chloros's
    ``daq/sensors/e.py:parse_imu_trailer``; selftest cross-checks the two
    against each other, because two decoders of one wire format that disagree
    are worse than one decoder.
    """
    if len(data) < IMU_TRAILER_LEN or data[0] != IMU_TRAILER_VERSION:
        return None
    if struct.unpack_from("<H", data, 20)[0] != _crc16_ccitt(data[:20]):
        return None
    fl = data[1]
    tilt = struct.unpack_from("<H", data, 10)[0]
    roll = struct.unpack_from("<h", data, 12)[0]
    pitch = struct.unpack_from("<h", data, 14)[0]
    return {
        "trailer_version": data[0],
        "flags": fl,
        "fresh": bool(fl & IMU_TF_FRESH),
        "angles_valid": bool(fl & IMU_TF_ANGLES_VALID),
        "cal_applied": bool(fl & IMU_TF_CAL_APPLIED),
        "healthy": bool(fl & IMU_TF_HEALTHY),
        "temp_valid": bool(fl & IMU_TF_TEMP_VALID),
        "tempco_applied": bool(fl & IMU_TF_TEMPCO_APPLIED),
        "tare_set": bool(fl & IMU_TF_TARE_SET),
        "tare_exceeded": bool(fl & IMU_TF_TARE_EXCEEDED),
        "sample_age_ms": struct.unpack_from("<H", data, 2)[0],
        "x_mg": struct.unpack_from("<h", data, 4)[0],
        "y_mg": struct.unpack_from("<h", data, 6)[0],
        "z_mg": struct.unpack_from("<h", data, 8)[0],
        "tilt_deg": None if tilt == CDEG_U16_INVALID else tilt / 100.0,
        "roll_deg": None if roll == CDEG_I16_INVALID else roll / 100.0,
        "pitch_deg": None if pitch == CDEG_I16_INVALID else pitch / 100.0,
        "mag_mg": struct.unpack_from("<H", data, 16)[0],
        # int8, uncalibrated die-temp TREND. The part specifies 1 LSB/degC of
        # CHANGE with no absolute reference, so only differences mean
        # anything -- never present this as a thermometer reading.
        "temp_raw": (struct.unpack_from("<b", data, 18)[0]
                     if fl & IMU_TF_TEMP_VALID else None),
    }


def parse_imu_datagram(data):
    """Decode a v3 attitude datagram from the standalone IMU group.

    Shaped as a SUPERSET of :func:`parse_imu_trailer` -- same key names, same
    units, same None-for-undefined convention -- so a consumer that handles a
    trailer handles one of these unchanged. The extra keys are the ones the
    trailer has no room for: ``raw_x/y/z_mg`` and ``tare_angle_deg``.

    Length is an EXACT match, unlike the spectral parser's lower bound: this
    datagram has no payload_len and nothing is appended after its crc, so
    anything longer is not one. If that ever changes the crc offset has to
    come from a length field -- do not simply relax the comparison.

    NOTE the timestamp keys are ``timestamp_us`` / ``absolute_time``, matching
    :func:`parse_datagram` in this module rather than Chloros's ``ts_us`` /
    ``absolute_ts``. Everything else is name-for-name identical (selftest
    cross-checks it); the trailer carries no timestamp at all, so these two
    names had no prior art to follow and the local convention won.
    """
    if len(data) != IMU_DGRAM_LEN or data[0:2] != MAGIC:
        return None
    if data[2] != IMU_DGRAM_VERSION:
        return None
    if struct.unpack_from("<H", data, 56)[0] != _crc16_ccitt(data[:56]):
        return None

    flags = data[3]
    fl = data[28]
    angles_ok = bool(fl & IMU_TF_ANGLES_VALID)
    tilt_c, roll_c, pitch_c, mag, tare_c = struct.unpack_from("<HhhHH", data, 44)
    temp = struct.unpack_from("<h", data, 54)[0]
    raw = struct.unpack_from("<hhh", data, 32)
    cor = struct.unpack_from("<hhh", data, 38)
    serial = "-".join("%02X" % b for b in data[22:27])
    return {
        # Its own version namespace: v3 is the attitude datagram, not a
        # newer spectral one. Carried so the shared per-device bookkeeping
        # (DeviceState.update) reads one shape whichever stream it is fed.
        "version": IMU_DGRAM_VERSION,
        "seq": struct.unpack_from("<I", data, 4)[0],
        "timestamp_us": struct.unpack_from("<Q", data, 8)[0],
        "absolute_time": bool(flags & FLAG_ABS_TS),
        "ptp_synced": bool(flags & FLAG_PTP),
        "mac": ":".join("%02x" % b for b in data[16:22]),
        "sensor_serial": serial,
        "model": "daq-e-s" if data[27] == 1 else "daq-e",
        "trailer_version": IMU_TRAILER_VERSION,
        "flags": fl,
        "fresh": bool(fl & IMU_TF_FRESH),
        "angles_valid": angles_ok,
        "cal_applied": bool(fl & IMU_TF_CAL_APPLIED),
        "healthy": bool(fl & IMU_TF_HEALTHY),
        "temp_valid": bool(fl & IMU_TF_TEMP_VALID),
        "tempco_applied": bool(fl & IMU_TF_TEMPCO_APPLIED),
        "tare_set": bool(fl & IMU_TF_TARE_SET),
        "tare_exceeded": bool(fl & IMU_TF_TARE_EXCEEDED),
        "sample_age_ms": struct.unpack_from("<H", data, 30)[0],
        "x_mg": cor[0], "y_mg": cor[1], "z_mg": cor[2],
        # Gated on the flag AND the sentinel. The flag is the firmware's
        # verdict, the sentinel is what it actually wrote; trusting one
        # without the other is how 655.35 degrees ends up in an average.
        "tilt_deg": (tilt_c / 100.0
                     if angles_ok and tilt_c != CDEG_U16_INVALID else None),
        "roll_deg": (roll_c / 100.0
                     if angles_ok and roll_c != CDEG_I16_INVALID else None),
        "pitch_deg": (pitch_c / 100.0
                      if angles_ok and pitch_c != CDEG_I16_INVALID else None),
        "mag_mg": mag,
        "temp_raw": (temp if (fl & IMU_TF_TEMP_VALID)
                     and temp != TEMP_I16_INVALID else None),
        "raw_x_mg": raw[0], "raw_y_mg": raw[1], "raw_z_mg": raw[2],
        "tare_angle_deg": (tare_c / 100.0
                           if tare_c != CDEG_U16_INVALID else None),
        "device_key": serial,
    }


def parse_datagram(data: bytes, src_ip: str = "") -> Optional[dict]:
    """Decode a v1 or v2 spectral datagram. None if it isn't a valid one.

    Both versions return the same shape; v2-only fields are None for v1.
    ``device_key`` is what to group by: the sensor serial when the frame
    carries one, else the source IP (the v1 fallback).
    """
    if len(data) < V1_HEADER + 2 or data[:2] != MAGIC:
        return None
    version = data[2]

    if version == V1:
        hdr = V1_HEADER
        plen = struct.unpack_from("<H", data, 16)[0]
        mac = serial = model = integration = None
    elif version == V2:
        hdr = V2_HEADER
        if len(data) < hdr + 2:
            return None
        plen = struct.unpack_from("<H", data, 30)[0]
        mac = ":".join("%02x" % b for b in data[16:22])
        serial = "-".join("%02X" % b for b in data[22:27])
        model = "daq-e-s" if data[27] == 1 else "daq-e"
        integration = struct.unpack_from("<H", data, 28)[0]
    else:
        return None

    # LOWER BOUND, not equality. An equality test here silently rejects
    # EVERY frame from a DAQ-E-S (and any DAQ-E on firmware >= 1.8.0 with the
    # IMU trailer on): those frames are 22 bytes longer than the spectral
    # datagram, so they fall through to the malformed counter and the sensor
    # looks absent while streaming perfectly. Chloros hit exactly this and
    # fixed it on 2026-08-18 (daq/sensors/e.py) -- the same rule is required
    # here, and anything appended after the crc in future must stay
    # compatible with it.
    if hdr + plen + 2 > len(data):
        return None
    if struct.unpack_from("<H", data, hdr + plen)[0] != _crc16_ccitt(data[:hdr + plen]):
        return None

    flags = data[3]
    payload = data[hdr:hdr + plen]
    calibrated = bool(flags & FLAG_CALIBRATED)

    # Presence only. The trailer carries the unit's attitude (tilt / roll /
    # pitch), which decides whether a cosine-corrected downwelling reading is
    # trustworthy -- but decoding it here would be a second copy of a layout
    # that must not drift, so this script reports that it is there and leaves
    # the decode to Chloros. Layout in PROTOCOL.md. A TRUNCATED trailer reads
    # as absent rather than invalidating the frame: it sits outside the crc
    # exactly so a bad one cannot cost a good spectrum.
    imu = None
    if flags & FLAG_IMU and len(data) >= hdr + plen + 2 + IMU_TRAILER_LEN:
        _start = hdr + plen + 2
        imu = parse_imu_trailer(data[_start:_start + IMU_TRAILER_LEN])
    has_imu = imu is not None

    spectrum = None
    if calibrated:
        spectrum = list(struct.unpack("<%df" % (plen // 4), payload))
    elif plen >= 12:
        n = struct.unpack_from("<I", payload, 8)[0]
        if 12 + n * 4 <= plen:
            spectrum = list(struct.unpack_from("<%df" % n, payload, 12))
            if integration is None:
                integration = struct.unpack_from("<H", payload, 4)[0]

    return {
        "version": version,
        "seq": struct.unpack_from("<I", data, 4)[0],
        "timestamp_us": struct.unpack_from("<Q", data, 8)[0],
        "absolute_time": bool(flags & FLAG_ABS_TS),
        "ptp_synced": bool(flags & FLAG_PTP),
        "saturated": bool(flags & FLAG_SATURATED),
        "calibrated": calibrated,
        "has_imu": has_imu,
        "imu": imu,
        "mac": mac,
        "sensor_serial": serial,
        "model": model,
        "integration_time_ms": integration,
        "spectrum": spectrum,
        "src_ip": src_ip,
        "device_key": serial or src_ip or "unknown",
    }


def open_multicast(group: str, port: int, iface_ip: str = "",
                   timeout: float = 1.0) -> socket.socket:
    """Join ``group`` and return a socket ready for recvfrom.

    ``iface_ip`` pins the join to one NIC. Worth setting on a machine with
    several interfaces -- the OS default route is frequently not the one the
    sensors are on, and the symptom is simply no frames.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    # Larger receive buffer: at 20 Hz across several sensors a stalled reader
    # otherwise drops frames the network delivered perfectly well.
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    except OSError:
        pass
    s.bind(("", port))
    mreq = struct.pack("4s4s", socket.inet_aton(group),
                       socket.inet_aton(iface_ip or "0.0.0.0"))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.settimeout(timeout)
    return s


class DeviceState:
    """Per-sensor bookkeeping. Sequence numbers are per (device, stream)."""

    def __init__(self, key: str):
        self.key = key
        self.frames = 0
        self.dropped = 0
        self.last_seq: Optional[int] = None
        self.serial: Optional[str] = None
        self.mac: Optional[str] = None
        self.model: Optional[str] = None
        self.version: Optional[int] = None
        self.ptp = False
        self.first_seen = time.monotonic()

    def update(self, f: dict) -> None:
        if self.last_seq is not None:
            gap = (f["seq"] - self.last_seq - 1) & 0xFFFFFFFF
            if 0 < gap < 1_000_000:
                self.dropped += gap
        self.last_seq = f["seq"]
        self.frames += 1
        self.serial = f["sensor_serial"] or self.serial
        self.mac = f["mac"] or self.mac
        self.model = f["model"] or self.model
        self.version = f["version"]
        self.ptp = f["ptp_synced"]

    def label(self) -> str:
        bits = [self.serial or self.key]
        if self.model:
            bits.append(self.model)
        if self.version == V1:
            bits.append("v1/legacy")
        if self.ptp:
            bits.append("ptp")
        return " ".join(bits)


# Attitude CSV columns, in order. Same names the parsers emit, so a reader of
# one is a reader of the other.
_IMU_CSV_COLUMNS = (
    "sensor_serial", "model", "seq", "timestamp_us", "ptp_synced",
    "sample_age_ms", "fresh", "healthy", "angles_valid", "cal_applied",
    "tempco_applied", "tare_set", "tare_exceeded",
    "tilt_deg", "roll_deg", "pitch_deg", "tare_angle_deg",
    "x_mg", "y_mg", "z_mg", "mag_mg",
    "raw_x_mg", "raw_y_mg", "raw_z_mg", "temp_raw",
)


def _csv_cell(v):
    """Empty for undefined, never 0.

    An angle the firmware did not compute is NOT level, and writing 0.0 for it
    puts a fabricated horizontal into any average taken over the column.
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return int(v)
    return v


def _query_unit(host, *, port=5001, token="", want_status=False,
                timeout=3.0):
    """Ask a DAQ-E over its control channel (TCP 5001) for the name its
    calibration is filed under, and -- for a calibrated stream -- its status,
    which says which cap profile it folds in.

    ``hello`` needs no token; ``status`` does on a provisioned unit.
    Returns ``{"name": str, "status": dict or None}``; raises on failure.
    """
    from daq_cal import DaqEControlClient   # numpy; only --daq needs this
    with DaqEControlClient(host, port=port, timeout=timeout,
                           token=token) as c:
        name = str(c.cmd({"cmd": "hello"}).get("name") or "").strip()
        if not name:
            raise RuntimeError("its hello carried no name")
        status = None
        if want_status:
            status = c.cmd({"cmd": "status"})
            if not status.get("ok", True):
                raise PermissionError(status.get("code")
                                      or status.get("error") or "refused")
    return {"name": name, "status": status}


def _open_daq(args, frame, src_ip, say=print):
    """Decide the .daq once the first frame has told us what to declare.

    The stream's OWN calibrated flag decides ``calibration_applied`` -- not
    which group was joined. That flag is the only thing standing between a
    calibrated recording and being calibrated a second time at import, which
    would square the correction silently, so it is taken from the data rather
    than from an argument.

    The serial stamped is the unit's NAME (its ``hello`` name, e.g.
    daq-e-54b774), the key Chloros fetches a DAQ-E's calibration under and
    the one record_daq.py stamps -- not the spectrometer serial the frame
    carries, under which Chloros found no bundle and skipped the recording
    (Jetson rig, 2026-10-16).

    Returns (make, path): make() creates the DaqWriter. main() runs it on its
    writer thread, because creating the file is a disk flush, and that must
    not happen in the receive loop. (None, None) when the name, or for a
    calibrated stream the cap, cannot be had; nothing is guessed.
    """
    import functools
    from mapir_metadata import DaqWriter

    # An attitude-only recording carries no spectra at all, so
    # calibration_applied has nothing to describe -- 0 is the honest answer
    # and Chloros's spectral readers never look at the file anyway.
    calibrated = bool(frame.get("calibrated")) and not args.imu
    model = args.serial_model or frame.get("model") or "daq-e"
    serial = frame.get("sensor_serial")
    if not serial:
        say("! --daq needs the sensor serial, which v1 frames do not carry "
            "(firmware < 1.7.0). Nothing recorded; update the unit or use "
            "record_daq.py over the raw TCP channel.",
            file=sys.stderr, flush=True)
        return None, None

    need_status = calibrated and not args.cap_id
    name, status = args.unit_name, None
    if not name or need_status:
        try:
            got = _query_unit(src_ip, port=args.control_port,
                              token=args.token, want_status=need_status)
        except Exception as e:  # noqa: BLE001 -- any failure: say so, stop
            if need_status and not args.unit_name:
                what = "its unit name and cap profile"
            elif need_status:
                what = "which cap profile it folds into the calibrated stream"
            else:
                what = "its unit name"
            say(f"! --daq: could not ask {src_ip} for {what} "
                f"({type(e).__name__}: {e}). Nothing is recorded to "
                f"{args.daq} rather than a guess. Chloros files a DAQ-E's "
                f"calibration under its name; pass --unit-name (the "
                f"'sensor' line of `daq_cal.py {src_ip}`)"
                + (", and --cap-id (its 'cap' line) or --token for a "
                     "provisioned unit," if need_status else "")
                + " to record without asking. The CSV is unaffected.",
                file=sys.stderr, flush=True)
            return None, None
        name = name or got["name"]
        status = got.get("status")

    kwargs = {}
    if calibrated:
        if args.cap_id:
            # The operator says which profile the device folded in.
            cap, cap_source = args.cap_id, "operator"
        elif status.get("profiles_present") and status.get("cap_id"):
            # What the unit itself reports applying. The calibrated stream
            # matches daq_cal + that profile to float rounding on hardware.
            cap, cap_source = status["cap_id"], "device"
        else:
            # No profile aboard: the device applied none.
            cap, cap_source = "as_recorded", "device"
        # The device folded in its own bundle. We do not have that bundle's
        # sha from the wire -- the datagram carries no provenance -- so say
        # so explicitly rather than inventing one. DaqWriter demands a sha
        # for a calibrated recording precisely so this cannot pass silently.
        kwargs = dict(
            calibration_applied=True,
            calibration_bundle_sha="device:" + name,
            calibration_completed_utc="",
            cap_id=cap,
            cap_id_source=cap_source,
            # as_recorded is "no per-wavelength profile": stamping it applied
            # would make Chloros refuse a later operator override.
            cap_applied=(cap != "as_recorded"),
        )
    elif args.cap_id and not args.imu:
        # Raw spectra: nothing applied; this DECLARES the cap for Chloros.
        kwargs = dict(cap_id=args.cap_id)
    # commit_every=0: main()'s write-behind thread does the commits.
    make = functools.partial(
        DaqWriter, args.daq, product_model=model, product_serial=name,
        device_name=frame.get("mac") or "", commit_every=0, **kwargs)
    _what = ("ATTITUDE" if args.imu else
             "CALIBRATED" if calibrated else "RAW")
    say(f"  recording {_what} frames to {args.daq}", flush=True)
    return make, args.daq


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Listen to any number of DAQ-E / DAQ-E-S sensors over "
                    "multicast.")
    p.add_argument("--calibrated", action="store_true",
                   help="listen to the calibrated stream (W/m^2/nm) instead "
                        "of raw counts. Only units carrying coefficients emit "
                        "it; a DAQ-E-S always does.")
    p.add_argument("--group", default=None, help="override the multicast group")
    p.add_argument("--port", type=int, default=None, help="override the port")
    p.add_argument("--iface", default="",
                   help="local interface IP to join on (set this on a "
                        "multi-NIC host)")
    p.add_argument("--serial", action="append", default=[],
                   help="only show this sensor serial; repeatable")
    p.add_argument("--csv", help="append every frame to this CSV")
    p.add_argument("--daq", metavar="PATH",
                   help="also record to a Chloros-compatible .daq. The file "
                        "declares what the stream actually carried: a "
                        "calibrated stream is stamped calibration_applied=1 "
                        "so Chloros imports it as-is instead of calibrating "
                        "it a second time. With --imu the samples are written "
                        "as event_type=4 attitude rows (no spectrum), at the "
                        "thinned to --imu-rate.")
    p.add_argument("--imu", action="store_true",
                   help="record the standalone attitude stream "
                        "(239.10.10.12:5004, firmware 1.12+) instead of "
                        "spectra. The accelerometer samples far faster than "
                        "spectra arrive, so this is the only way to get "
                        "attitude at better than the ~2.5 Hz the per-frame "
                        "trailer allows. Writes --csv and/or --daq; see "
                        "--imu-rate for how much of it is kept.")
    p.add_argument("--imu-rate", metavar="HZ", type=float, default=5.0,
                   help="with --imu: attitude samples per second to KEEP "
                        "(default 5). The device streams at ~50 Hz, which is "
                        "more than most work needs and ten times the rows in "
                        "both outputs, so samples are thinned to this rate BY "
                        "THEIR OWN TIMESTAMPS. Raise it for vibration or "
                        "fast-attitude work; 0 keeps every sample sent.")
    p.add_argument("--serial-model", metavar="MODEL",
                   help="with --daq: override the product_model "
                        "stamped (daq-e / daq-e-s). Default: whatever "
                        "the frames report, which v2 always does.")
    p.add_argument("--cap-id", metavar="CAP_ID", default=None,
                   help="with --daq: on a CALIBRATED stream, which cap "
                        "profile the device folded in; on a raw stream, which "
                        "cap is fitted, for Chloros to apply. Default: on a "
                        "calibrated stream, asked of the unit (its status "
                        "names the profile it applies); on a raw stream, the "
                        "cap the model ships wearing, recorded as assumed.")
    p.add_argument("--unit-name", metavar="NAME",
                   help="with --daq: the unit name its calibration is filed "
                        "under (daq-e-XXXXXX, the 'sensor' line of "
                        "daq_cal.py). Default: asked of the unit over its "
                        "control channel, once.")
    p.add_argument("--control-port", type=int, default=5001,
                   help="DAQ-E JSON control TCP port, for --daq (default 5001)")
    p.add_argument("--token", default="",
                   help="DAQ-E control-channel token, for --daq on a "
                        "provisioned unit (its status needs it)")
    p.add_argument("--count", type=int, default=0, help="stop after N frames")
    p.add_argument("--duration", type=float, default=0.0,
                   help="stop after N seconds")
    p.add_argument("--quiet", action="store_true",
                   help="suppress per-frame lines; print the summary only")
    args = p.parse_args(argv)

    if args.imu and args.calibrated:
        raise SystemExit("--imu and --calibrated are different streams; "
                         "pick one.")

    group = args.group or (DEFAULT_IMU_GROUP if args.imu else
                           DEFAULT_CAL_GROUP if args.calibrated
                           else DEFAULT_RAW_GROUP)
    port = args.port or (DEFAULT_IMU_PORT if args.imu else
                         DEFAULT_CAL_PORT if args.calibrated
                         else DEFAULT_RAW_PORT)
    wanted = {s.strip().upper() for s in args.serial}

    sock = open_multicast(group, port, args.iface)
    kind = ("ATTITUDE (deg / mg)" if args.imu else
            "CALIBRATED (W/m^2/nm)" if args.calibrated else "RAW counts")
    print(f"Listening on {group}:{port} -- {kind}")
    if wanted:
        print(f"  filtering to: {', '.join(sorted(wanted))}")
    print("  Ctrl-C to stop.\n", flush=True)

    devices: Dict[str, DeviceState] = {}
    csv_fh = csv_out = None
    if args.csv:
        csv_fh = open(args.csv, "w", newline="", encoding="utf-8")
        csv_out = csv.writer(csv_fh)
        # Say which stream this came from, in the file. The two groups carry
        # numbers that differ by the whole calibration -- counts vs W/m^2/nm,
        # four orders of magnitude apart -- and a CSV that does not record
        # which one it holds is indistinguishable from the other on
        # inspection. The per-row `calibrated` column below is the
        # authoritative one (it is the frame's own flag, not this argument),
        # but a reader opening the file wants to know at the top.
        _units = ("attitude (deg / mg)" if args.imu else
                  "calibrated W/m^2/nm" if args.calibrated else "raw counts")
        csv_out.writerow([f"# MAPIR DAQ-E multicast {group}:{port} -- {_units}"])
        if args.imu:
            csv_out.writerow(_IMU_CSV_COLUMNS)
        else:
            csv_out.writerow(["device", "serial", "model", "seq",
                              "timestamp_us", "ptp", "integration_ms",
                              "saturated", "calibrated", "units", "imu",
                              "tilt_deg", "roll_deg", "pitch_deg",
                              "n_points", "spectrum..."])

    daq_writer = None
    daq_path = None
    daq_key = None          # the unit this .daq belongs to
    daq_gave_up = False     # _open_daq refused once: do not ask every frame
    daq_others = set()      # other units heard, already warned about
    # Attitude thinning. Gated on each sample's OWN timestamp rather than
    # a keep-every-Nth counter: the device's rate is nominal, not
    # guaranteed, so a counter would silently change the output rate
    # whenever the device's drifted. 0 (or negative) keeps everything.
    imu_min_gap_us = (1e6 / args.imu_rate) if args.imu_rate > 0 else 0.0
    imu_last_us = None
    imu_dropped = 0

    # One frame's rows, CSV and .daq, written on a thread of their own. On a
    # disk the cameras are writing to, one .daq commit took 37 s and a plain
    # write 3 s (Jetson rig, 2026-10-19). Done in this loop, the socket would
    # overflow and frames without a device clock would be stamped late.
    def _open(make):
        nonlocal daq_writer
        daq_writer = make()

    def _store(csv_row, daq_call):
        if csv_row is not None:
            csv_out.writerow(csv_row)
        if daq_call is not None:
            method, a, kw = daq_call
            getattr(daq_writer, method)(*a, **kw)

    def _commit():
        if daq_writer is not None:
            daq_writer.commit()
        if csv_fh is not None:
            csv_fh.flush()

    def _close_files():
        try:
            if daq_writer is not None:
                daq_writer.close()
        finally:
            if csv_fh is not None:
                csv_fh.close()

    files = None
    say = print
    if args.csv or args.daq:
        # numpy + Pillow; plain listening does not need them.
        from mapir_metadata import PrintBehind, WriteBehind
        files = WriteBehind(commit=_commit, close=_close_files,
                            name="stream-writer")
        # The loop's own lines too: stdout in a file on that disk blocks
        # the same way.
        say = PrintBehind()
    stalled = 0             # frames dropped in the current overflow

    n = 0
    t0 = time.monotonic()
    bad = 0
    mislabelled_warned = False
    try:
        while True:
            if args.duration and (time.monotonic() - t0) >= args.duration:
                break
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            # Host arrival time, for frames without a device clock: taken
            # here, before anything below (opening the .daq is a disk flush)
            # can delay it.
            arrival_ns = time.time_ns()
            f = (parse_imu_datagram(data) if args.imu
                 else parse_datagram(data, addr[0]))
            if f is None:
                bad += 1
                continue
            if args.imu:
                f.setdefault("src_ip", addr[0])
            if wanted and (f["sensor_serial"] or "").upper() not in wanted:
                continue

            # The two groups are supposed to be exclusive, so a frame whose
            # own FLAG_CALIBRATED bit disagrees with the group it arrived on
            # means the firmware is emitting on the wrong one. That is a
            # 4-orders-of-magnitude error if it goes unnoticed -- counts read
            # as W/m^2/nm or vice versa -- so say it, once, loudly. Chloros
            # makes the same check on its own raw subscription and drops the
            # frame outright (daq/sensors/e.py); this script keeps it, because
            # the per-row `calibrated` column records what actually arrived
            # rather than what the group implies.
            if (not args.imu and f["calibrated"] != args.calibrated
                    and not mislabelled_warned):
                mislabelled_warned = True
                want = "calibrated" if args.calibrated else "raw"
                got = "calibrated" if f["calibrated"] else "raw"
                say(f"! WARNING: {group}:{port} is the {want} group but "
                    f"this frame is flagged {got} "
                    f"({f['sensor_serial'] or addr[0]}). The CSV records "
                    f"each frame's own flag; treat the units with "
                    f"suspicion and check the unit's firmware.",
                    file=sys.stderr, flush=True)

            # Thin attitude to the requested rate before anything writes
            # it. Standalone stream only -- the per-frame trailer already
            # arrives at the spectral rate (~2.5 Hz) and costs no extra
            # rows, being columns on a row that exists anyway.
            if args.imu and imu_min_gap_us:
                _t = f["timestamp_us"]
                if (imu_last_us is not None
                        and (_t - imu_last_us) < imu_min_gap_us):
                    imu_dropped += 1
                    continue
                imu_last_us = _t

            key = f["device_key"]
            st = devices.get(key)
            if st is None:
                st = devices[key] = DeviceState(key)
                note = ""
                if f["version"] == V1 and not args.imu:
                    note = ("  [legacy v1: no identity in the frame, keyed on "
                            "source IP. Update to fw 1.7.0+ for reliable "
                            "multi-sensor separation.]")
                say(f"+ sensor {st.label() or key} from {addr[0]}{note}",
                    flush=True)
            st.update(f)

            csv_row = None
            if csv_out is not None and args.imu:
                csv_row = [_csv_cell(f.get(c)) for c in _IMU_CSV_COLUMNS]
            elif csv_out is not None and f["spectrum"] is not None:
                _imu = f.get("imu") or {}
                csv_row = (
                    [key, f["sensor_serial"] or "", f["model"] or "",
                     f["seq"], f["timestamp_us"], int(f["ptp_synced"]),
                     f["integration_time_ms"] or "", int(f["saturated"]),
                     # Per FRAME, from its own flag bit -- not from which
                     # group we happened to join. If a unit ever emits the
                     # wrong thing on a group, the file still describes what
                     # actually arrived.
                     int(f["calibrated"]),
                     "W/m^2/nm" if f["calibrated"] else "counts",
                     int(f["has_imu"]),
                     _csv_cell(_imu.get("tilt_deg")),
                     _csv_cell(_imu.get("roll_deg")),
                     _csv_cell(_imu.get("pitch_deg")),
                     len(f["spectrum"])]
                    + ["%.6g" % v for v in f["spectrum"]])

            # --- .daq: which frames go in ----------------------------------
            # Opened on the FIRST frame, not up front: the model and serial
            # it has to declare come off the wire, and a v1 unit reports
            # neither. Opening early would mean inventing them. A .daq is ONE
            # unit's recording -- it carries one serial and one calibration
            # -- so frames from any other unit on the group stay out of it.
            to_daq = False
            if (args.daq and not daq_gave_up
                    and (args.imu or f["spectrum"] is not None)):
                if daq_path is None:
                    make, daq_path = _open_daq(args, f, addr[0], say)
                    daq_key = key
                    daq_gave_up = make is None
                    # Queued ahead of every row it will hold.
                    if make is not None and not files.submit(_open, make):
                        say(f"! --daq: the disk is too far behind to even "
                            f"create {daq_path}; nothing recorded to it",
                            file=sys.stderr, flush=True)
                        daq_path, daq_gave_up = None, True
                if daq_path is not None:
                    if key == daq_key:
                        to_daq = True
                    elif key not in daq_others:
                        daq_others.add(key)
                        say(f"! --daq records one unit per file: "
                            f"{daq_path} holds {daq_key}; frames from "
                            f"{key} ({addr[0]}) are not in it. Use "
                            f"--serial to pick the unit, or run one "
                            f"daq_stream.py per unit.",
                            file=sys.stderr, flush=True)

            # Built here, so the frame keeps its arrival stamp however long
            # the disk then takes over it.
            daq_call = None
            # --- .daq: attitude ------------------------------------------
            if to_daq and args.imu:
                daq_call = ("write_attitude", (f,), dict(
                    timestamp_ns=(f["timestamp_us"] * 1000
                                  if f["absolute_time"]
                                  else arrival_ns),
                    device_timestamp_ns=(f["timestamp_us"] * 1000
                                         if f["absolute_time"] else None),
                    device_ts_ptp=f["ptp_synced"]))

            # --- .daq: spectra -------------------------------------------
            if to_daq and not args.imu:
                daq_call = ("write", (
                    f["spectrum"], f["saturated"],
                    f["integration_time_ms"] or 0), dict(
                    # The sensor latches timestamp_us as its own last
                    # byte arrives, so it beats host arrival time -- but
                    # only when it is an absolute epoch. A free-running
                    # boot counter would look absolute and drag the file
                    # onto a nonsense axis, so hand it over only when the
                    # frame says it is real and let DaqWriter refuse the
                    # rest.
                    timestamp_ns=(f["timestamp_us"] * 1000
                                  if f["absolute_time"]
                                  else arrival_ns),
                    device_timestamp_ns=(f["timestamp_us"] * 1000
                                         if f["absolute_time"] else None),
                    device_ts_ptp=f["ptp_synced"],
                    imu=f.get("imu")))

            if csv_row is not None or daq_call is not None:
                if files.submit(_store, csv_row, daq_call):
                    if stalled:
                        say(f"  disk caught up: {stalled} frame(s) were "
                            f"lost while it was stalled", file=sys.stderr,
                            flush=True)
                        stalled = 0
                else:
                    if not stalled:
                        say(f"! the disk is {files.backlog} frames "
                            f"behind; new frames are being DROPPED until "
                            f"it catches up", file=sys.stderr, flush=True)
                    stalled += 1

            if not args.quiet and st.frames % 20 == 1:
                peak = max(f["spectrum"]) if f["spectrum"] else float("nan")
                say(f"  {st.label():34} seq={f['seq']:<8} "
                    f"t={f['integration_time_ms'] or '?':>4}ms  peak={peak:.4g}",
                    flush=True)

            n += 1
            if args.count and n >= args.count:
                break
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        if say is not print:
            say.close()
        if files is not None:
            if files.backlog:
                print(f"  writing {files.backlog} queued frame(s) to disk ...",
                      flush=True)
            files.close()

    print(f"\n{len(devices)} sensor(s), {n} frame(s)"
          + (f", {bad} malformed datagram(s)" if bad else "")
          + (f", {files.dropped} DROPPED waiting for the disk"
             if files is not None and files.dropped else "")
          + (f", {imu_dropped} sample(s) thinned to {args.imu_rate:g} Hz"
             if imu_dropped else ""))
    for st in devices.values():
        loss = (100.0 * st.dropped / (st.frames + st.dropped)
                if (st.frames + st.dropped) else 0.0)
        print(f"  {st.label():34} frames={st.frames:<7} "
              f"dropped={st.dropped} ({loss:.2f}%)")
    if args.csv:
        print(f"\nCSV: {args.csv}")
    if daq_path:
        _n = (daq_writer.attitude_count if args.imu
              else daq_writer.record_count)
        _kind = "attitude sample(s)" if args.imu else "reading(s)"
        print(f"DAQ: {daq_path}  ({_n} {_kind}"
              + ("" if args.imu else f", cap={daq_writer.cap_id}") + ")")
    elif args.daq and daq_gave_up:
        print("DAQ: nothing written -- see the message above.")
    elif args.daq:
        print("DAQ: nothing written -- no %s arrived."
              % ("attitude samples" if args.imu else "spectral frames"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
