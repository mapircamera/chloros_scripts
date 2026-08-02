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


def _crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


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

    if hdr + plen + 2 != len(data):
        return None
    if struct.unpack_from("<H", data, hdr + plen)[0] != _crc16_ccitt(data[:hdr + plen]):
        return None

    flags = data[3]
    payload = data[hdr:hdr + plen]
    calibrated = bool(flags & FLAG_CALIBRATED)

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
    p.add_argument("--count", type=int, default=0, help="stop after N frames")
    p.add_argument("--duration", type=float, default=0.0,
                   help="stop after N seconds")
    p.add_argument("--quiet", action="store_true",
                   help="suppress per-frame lines; print the summary only")
    args = p.parse_args(argv)

    group = args.group or (DEFAULT_CAL_GROUP if args.calibrated
                           else DEFAULT_RAW_GROUP)
    port = args.port or (DEFAULT_CAL_PORT if args.calibrated
                         else DEFAULT_RAW_PORT)
    wanted = {s.strip().upper() for s in args.serial}

    sock = open_multicast(group, port, args.iface)
    kind = "CALIBRATED (W/m^2/nm)" if args.calibrated else "RAW counts"
    print(f"Listening on {group}:{port} -- {kind}")
    if wanted:
        print(f"  filtering to: {', '.join(sorted(wanted))}")
    print("  Ctrl-C to stop.\n", flush=True)

    devices: Dict[str, DeviceState] = {}
    csv_fh = csv_out = None
    if args.csv:
        csv_fh = open(args.csv, "w", newline="", encoding="utf-8")
        csv_out = csv.writer(csv_fh)
        csv_out.writerow(["device", "serial", "model", "seq", "timestamp_us",
                          "ptp", "integration_ms", "saturated", "n_points",
                          "spectrum..."])

    n = 0
    t0 = time.monotonic()
    bad = 0
    try:
        while True:
            if args.duration and (time.monotonic() - t0) >= args.duration:
                break
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            f = parse_datagram(data, addr[0])
            if f is None:
                bad += 1
                continue
            if wanted and (f["sensor_serial"] or "").upper() not in wanted:
                continue

            key = f["device_key"]
            st = devices.get(key)
            if st is None:
                st = devices[key] = DeviceState(key)
                note = ""
                if f["version"] == V1:
                    note = ("  [legacy v1: no identity in the frame, keyed on "
                            "source IP. Update to fw 1.7.0+ for reliable "
                            "multi-sensor separation.]")
                print(f"+ sensor {st.label() or key} from {addr[0]}{note}",
                      flush=True)
            st.update(f)

            if csv_out is not None and f["spectrum"] is not None:
                csv_out.writerow(
                    [key, f["sensor_serial"] or "", f["model"] or "",
                     f["seq"], f["timestamp_us"], int(f["ptp_synced"]),
                     f["integration_time_ms"] or "", int(f["saturated"]),
                     len(f["spectrum"])]
                    + ["%.6g" % v for v in f["spectrum"]])

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

    print(f"\n{len(devices)} sensor(s), {n} frame(s)"
          + (f", {bad} malformed datagram(s)" if bad else ""))
    for st in devices.values():
        loss = (100.0 * st.dropped / (st.frames + st.dropped)
                if (st.frames + st.dropped) else 0.0)
        print(f"  {st.label():34} frames={st.frames:<7} "
              f"dropped={st.dropped} ({loss:.2f}%)")
    if args.csv:
        print(f"\nCSV: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
