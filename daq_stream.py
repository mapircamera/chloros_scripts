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


def _open_daq(args, frame):
    """Open the .daq once the first frame has told us what to declare.

    The stream's OWN calibrated flag decides ``calibration_applied`` -- not
    which group was joined. That flag is the only thing standing between a
    calibrated recording and being calibrated a second time at import, which
    would square the correction silently, so it is taken from the data rather
    than from an argument.
    """
    from mapir_metadata import DaqWriter

    calibrated = bool(frame["calibrated"])
    model = args.serial_model or frame.get("model") or "daq-e"
    serial = frame.get("sensor_serial")
    if not serial:
        print("! --daq needs the sensor serial, which v1 frames do not carry "
              "(firmware < 1.7.0). Nothing recorded; update the unit or use "
              "record_daq.py over the raw TCP channel.",
              file=sys.stderr, flush=True)
        return None, None
    kwargs = {}
    if calibrated:
        # The device folded in its own bundle. We do not have that bundle's
        # sha from the wire -- the datagram carries no provenance -- so say
        # so explicitly rather than inventing one. DaqWriter demands a sha
        # for a calibrated recording precisely so this cannot pass silently.
        kwargs = dict(
            calibration_applied=True,
            calibration_bundle_sha="device:" + (serial or "unknown"),
            calibration_completed_utc="",
            cap_id=args.cap_id,
            cap_id_source="device",
            cap_applied=(args.cap_id not in (None, "", "as_recorded")),
        )
    w = DaqWriter(args.daq, product_model=model, product_serial=serial,
                  device_name=frame.get("mac") or "", **kwargs)
    print(f"  recording {'CALIBRATED' if calibrated else 'RAW'} frames to "
          f"{args.daq}", flush=True)
    return w, args.daq


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
                        "it a second time.")
    p.add_argument("--imu", action="store_true",
                   help="listen to the standalone attitude stream "
                        "(239.10.10.12:5004, firmware 1.12+) instead of "
                        "spectra -- full accelerometer rate rather than the "
                        "~2.5 Hz the spectral trailer delivers. CSV only.")
    p.add_argument("--serial-model", metavar="MODEL",
                   help="with --daq: override the product_model "
                        "stamped (daq-e / daq-e-s). Default: whatever "
                        "the frames report, which v2 always does.")
    p.add_argument("--cap-id", metavar="CAP_ID", default="as_recorded",
                   help="with --daq on a CALIBRATED stream: which cap the "
                        "device folded in, for the file to declare. Read it "
                        "off the unit with daq_cal.py. Default "
                        "'as_recorded' -- the device applied whatever it "
                        "holds and this script is not guessing which.")
    p.add_argument("--count", type=int, default=0, help="stop after N frames")
    p.add_argument("--duration", type=float, default=0.0,
                   help="stop after N seconds")
    p.add_argument("--quiet", action="store_true",
                   help="suppress per-frame lines; print the summary only")
    args = p.parse_args(argv)

    if args.imu and args.daq:
        raise SystemExit(
            "--imu cannot be written to a .daq: an attitude sample is not a "
            "spectrum, and Chloros only reads als_log rows that HAVE "
            "spectral_data. Record attitude to --csv, or record spectra with "
            "--daq -- a DAQ-E-S folds its attitude into those frames as a "
            "trailer, and that DOES land in the .daq's imu_* columns.")
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
                print(f"! WARNING: {group}:{port} is the {want} group but "
                      f"this frame is flagged {got} "
                      f"({f['sensor_serial'] or addr[0]}). The CSV records "
                      f"each frame's own flag; treat the units with "
                      f"suspicion and check the unit's firmware.",
                      file=sys.stderr, flush=True)

            key = f["device_key"]
            st = devices.get(key)
            if st is None:
                st = devices[key] = DeviceState(key)
                note = ""
                if f["version"] == V1 and not args.imu:
                    note = ("  [legacy v1: no identity in the frame, keyed on "
                            "source IP. Update to fw 1.7.0+ for reliable "
                            "multi-sensor separation.]")
                print(f"+ sensor {st.label() or key} from {addr[0]}{note}",
                      flush=True)
            st.update(f)

            if csv_out is not None and args.imu:
                csv_out.writerow([_csv_cell(f.get(c)) for c in _IMU_CSV_COLUMNS])
            elif csv_out is not None and f["spectrum"] is not None:
                _imu = f.get("imu") or {}
                csv_out.writerow(
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

            # --- .daq ---------------------------------------------------
            # Opened on the FIRST frame, not up front: the model and serial
            # it has to declare come off the wire, and a v1 unit reports
            # neither. Opening early would mean inventing them.
            if args.daq and f["spectrum"] is not None:
                if daq_writer is None:
                    daq_writer, daq_path = _open_daq(args, f)
                if daq_writer is not None:
                    daq_writer.write(
                        f["spectrum"], f["saturated"],
                        f["integration_time_ms"] or 0,
                        # The sensor latches timestamp_us as its own last
                        # byte arrives, so it beats host arrival time -- but
                        # only when it is an absolute epoch. A free-running
                        # boot counter would look absolute and drag the file
                        # onto a nonsense axis, so hand it over only when the
                        # frame says it is real and let DaqWriter refuse the
                        # rest.
                        timestamp_ns=(f["timestamp_us"] * 1000
                                      if f["absolute_time"]
                                      else time.time_ns()),
                        device_timestamp_ns=(f["timestamp_us"] * 1000
                                             if f["absolute_time"] else None),
                        device_ts_ptp=f["ptp_synced"],
                        imu=f.get("imu"))

            if not args.quiet and st.frames % 20 == 1:
                peak = max(f["spectrum"]) if f["spectrum"] else float("nan")
                print(f"  {st.label():34} seq={f['seq']:<8} "
                      f"t={f['integration_time_ms'] or '?':>4}ms  peak={peak:.4g}",
                      flush=True)

            n += 1
            if args.count and n >= args.count:
                break
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        if csv_fh is not None:
            csv_fh.close()
        if daq_writer is not None:
            daq_writer.close()

    print(f"\n{len(devices)} sensor(s), {n} frame(s)"
          + (f", {bad} malformed datagram(s)" if bad else ""))
    for st in devices.values():
        loss = (100.0 * st.dropped / (st.frames + st.dropped)
                if (st.frames + st.dropped) else 0.0)
        print(f"  {st.label():34} frames={st.frames:<7} "
              f"dropped={st.dropped} ({loss:.2f}%)")
    if args.csv:
        print(f"\nCSV: {args.csv}")
    if daq_path:
        print(f"DAQ: {daq_path}  ({daq_writer.record_count} reading(s), "
              f"cap={daq_writer.cap_id})")
    elif args.daq:
        print("DAQ: nothing written -- no spectral frames arrived.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
