#!/usr/bin/env python3
"""
record_daq.py -- record RAW spectra from a MAPIR DAQ light sensor (U / M / E)
to a Chloros-compatible .daq file, with NO MAPIR SDK and NO calibration.

This is a self-contained reference: it speaks the DAQ spectral-sensor wire
protocol directly over the three transports:

    DAQ-U : USB serial      (pyserial)
    DAQ-M : Bluetooth LE     (bleak, Nordic UART Service)
    DAQ-E : Ethernet         (stdlib sockets: JSON control + raw TCP)

It records the sensor's RAW firmware-output spectrum (no calibration applied)
and stamps the .daq so Chloros fetches this sensor's factory calibration from
the cloud BY SERIAL and applies it at import. You capture; Chloros calibrates.

Usage
-----
    python record_daq.py u --port COM7
    python record_daq.py u --port /dev/ttyUSB0 --integration-time 50 --frames 100
    python record_daq.py m --mac AA:BB:CC:DD:EE:FF
    python record_daq.py e --host 192.168.1.50 --duration 300

Press Ctrl-C to stop. Output defaults to ./<kind>_<timestamp>.daq.

Protocol summary (NSP32-style, all transports share it)
-------------------------------------------------------
  * Command:  03 BB <cmd> <user> [payload...] <checksum>
  * Response: 03 BB <cmd> <user> [payload...] <checksum>
  * checksum = ((~sum(bytes_before_checksum)) + 1) & 0xFF  (modular two's-comp;
    a valid packet has sum(whole_packet) & 0xFF == 0)
  * All multi-byte fields little-endian.
"""

import argparse
import io
import os
import signal
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timezone

from mapir_metadata import DaqWriter

# ---------------------------------------------------------------------------
# Wire protocol  (shared by all transports)
# ---------------------------------------------------------------------------
PFX0, PFX1 = 0x03, 0xBB
CMD_HELLO, CMD_STANDBY = 0x01, 0x04
CMD_GET_ID, CMD_GET_WL = 0x06, 0x24
CMD_ACQ, CMD_GET_SPEC = 0x26, 0x28

# Total length (bytes) of each RESPONSE packet, keyed by its command code.
# 135 spectral points: GetWavelength = 8 + 135*2 + 1 = 279;
# GetSpectrum = 12 + 135*4 + 12 + 1 = 565.
RET_LEN = {CMD_HELLO: 5, CMD_STANDBY: 5, CMD_GET_ID: 10,
           CMD_GET_WL: 279, CMD_ACQ: 5, CMD_GET_SPEC: 565}


def _checksum(data):
    """Modular two's-complement checksum placed as the last packet byte."""
    return ((~sum(data)) + 1) & 0xFF


def _checksum_ok(packet):
    return (sum(packet) & 0xFF) == 0


def _frame(code, user=0, payload=b""):
    body = bytearray([PFX0, PFX1, code & 0xFF, user & 0xFF])
    body += bytes(payload)
    body.append(_checksum(body))
    return bytes(body)


def cmd_hello():   return _frame(CMD_HELLO)
def cmd_standby(): return _frame(CMD_STANDBY)
def cmd_get_id():  return _frame(CMD_GET_ID)
def cmd_get_wl():  return _frame(CMD_GET_WL)


def cmd_acquire(integration_ms, frame_avg, enable_ae):
    """AcqSpectrum with 'active return' = 1: the sensor sends the GetSpectrum
    response automatically once the exposure completes."""
    payload = bytes([
        integration_ms & 0xFF, (integration_ms >> 8) & 0xFF,
        frame_avg & 0xFF, 1 if enable_ae else 0, 1])
    return _frame(CMD_ACQ, 0, payload)


def parse_sensor_id(packet):
    """GetSensorId response -> 'XX-XX-XX-XX-XX' (the calibration fetch key)."""
    return "-".join("%02X" % b for b in packet[4:9])


def parse_spectrum(packet):
    """GetSpectrum response -> (spectrum_floats, integration_ms, is_saturated).

    spectrum is the RAW firmware-output float array (pre-calibration) -- exactly
    what we store; Chloros applies the cloud bundle to it at import.
    """
    integration_ms = struct.unpack_from("<H", packet, 4)[0]
    is_saturated = packet[6] == 1
    n = struct.unpack_from("<I", packet, 8)[0]
    spectrum = struct.unpack_from("<%df" % n, packet, 12)
    return list(spectrum), integration_ms, is_saturated


# ---------------------------------------------------------------------------
# Stream framing  (serial + DAQ-E raw TCP: read one packet from a byte stream)
# ---------------------------------------------------------------------------
def read_stream_packet(read1, deadline):
    """Read+validate one protocol packet using read1() -> 1 byte (or b'').

    read1 should block up to its own short timeout and return b'' on timeout so
    we can honor the overall deadline. Returns the full packet bytes, or None on
    timeout / framing failure.
    """
    # sync on the 03 BB prefix
    prev = 0
    while True:
        if time.monotonic() > deadline:
            return None
        b = read1()
        if not b:
            continue
        if prev == PFX0 and b[0] == PFX1:
            break
        prev = b[0]
    # next byte is the command code -> tells us the total length
    cmd_b = _read_n(read1, 1, deadline)
    if not cmd_b:
        return None
    cmd = cmd_b[0]
    total = RET_LEN.get(cmd)
    if not total:
        return None  # unknown response; resync next call
    rest = _read_n(read1, total - 3, deadline)  # already consumed 03 BB cmd
    if rest is None or len(rest) != total - 3:
        return None
    packet = bytes([PFX0, PFX1, cmd]) + rest
    if not _checksum_ok(packet):
        return None
    return packet


def _read_n(read1, n, deadline):
    out = bytearray()
    while len(out) < n:
        if time.monotonic() > deadline:
            return None
        b = read1()
        if b:
            out += b
    return bytes(out)


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------
class SerialTransport:
    """DAQ-U: 115200 8N1 over a USB serial port (pyserial)."""

    def __init__(self, port):
        self.port = port
        self._ser = None

    def open(self):
        import serial  # pyserial
        self._ser = serial.Serial(
            self.port, baudrate=115200, bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=0.05)
        # drain any leftover bytes from a previous session
        try:
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
        except Exception:
            pass

    def send(self, data):
        self._ser.write(data)

    def read1(self):
        return self._ser.read(1)

    def recv_packet(self, timeout):
        return read_stream_packet(self.read1, time.monotonic() + timeout)

    def close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None


class TcpTransport:
    """DAQ-E raw channel: the same spectral protocol over a TCP socket."""

    def __init__(self, host, port=5000, timeout=2.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock = None
        self._buf = bytearray()

    def open(self):
        self._sock = socket.create_connection((self.host, self.port),
                                              timeout=self.timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.settimeout(0.05)

    def send(self, data):
        self._sock.sendall(data)

    def read1(self):
        if not self._buf:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                return b""
            if not chunk:
                raise ConnectionError("DAQ-E closed the connection")
            self._buf.extend(chunk)
        b = bytes(self._buf[:1])
        del self._buf[:1]
        return b

    def recv_packet(self, timeout):
        return read_stream_packet(self.read1, time.monotonic() + timeout)

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None


class BleTransport:
    """DAQ-M: Nordic UART Service over Bluetooth LE (bleak).

    Runs a private asyncio loop in a background thread so the recorder's main
    loop stays simple and synchronous. The RX-notify callback reassembles
    protocol packets and pushes complete ones onto a queue.
    """

    SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca3e"
    TX_CHAR = "6e400002-b5a3-f393-e0a9-e50e24dcca3e"  # host -> device
    RX_CHAR = "6e400003-b5a3-f393-e0a9-e50e24dcca3e"  # device -> host (notify)

    def __init__(self, mac):
        self.mac = mac
        import queue
        self._pkts = queue.Queue()
        self._rx = bytearray()
        self._client = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()

    # -- background asyncio loop --
    def _run_loop(self):
        import asyncio
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro, timeout=15.0):
        import asyncio
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def open(self):
        from bleak import BleakClient
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        while self._loop is None:
            time.sleep(0.01)
        self._client = BleakClient(self.mac)
        self._submit(self._client.connect(), timeout=30.0)
        self._submit(self._client.start_notify(self.RX_CHAR, self._on_notify))

    def _on_notify(self, _sender, data):
        # reassemble packets by the length-from-command-code rule
        self._rx += bytes(data)
        while len(self._rx) >= 3:
            cmd = self._rx[2]
            total = RET_LEN.get(cmd)
            if total is None:
                # unknown/garbage lead byte: drop one and resync
                del self._rx[0]
                continue
            if len(self._rx) < total:
                break
            pkt = bytes(self._rx[:total])
            del self._rx[:total]
            if _checksum_ok(pkt):
                self._pkts.put(pkt)

    def send(self, data):
        self._submit(self._client.write_gatt_char(self.TX_CHAR, data,
                                                  response=False))

    def recv_packet(self, timeout):
        import queue
        try:
            return self._pkts.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        try:
            if self._client is not None:
                self._submit(self._client.disconnect(), timeout=10.0)
        except Exception:
            pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)


class DaqEControl:
    """DAQ-E JSON control channel (TCP 5001). Used only to read the sensor's
    name (its calibration serial) and firmware at connect."""

    def __init__(self, host, port=5001, timeout=3.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.settimeout(timeout)
        self._lock = threading.Lock()

    def cmd(self, obj):
        import json
        with self._lock:
            self._sock.sendall((json.dumps(obj) + "\n").encode())
            buf = bytearray()
            while True:
                ch = self._sock.recv(1)
                if not ch or ch == b"\n":
                    break
                buf += ch
            return json.loads(buf.decode())

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Sensor: connect handshake + acquire loop (transport-agnostic)
# ---------------------------------------------------------------------------
class DaqSensor:
    """Drives one sensor through a transport. The acquire loop sends
    AcqSpectrum (active-return) and waits for the GetSpectrum frame."""

    def __init__(self, kind, transport, *, integration_ms, frame_avg,
                 enable_ae):
        self.kind = kind                  # 'daq-u' / 'daq-m' / 'daq-e'
        self.t = transport
        self.integration_ms = integration_ms
        self.frame_avg = frame_avg
        self.enable_ae = enable_ae
        self.sensor_id = None
        self.fw = None

    def _exchange(self, command, want_cmd, timeout):
        """Send a command and return the next response with code want_cmd,
        skipping any intervening ACK packets (e.g. AcqSpectrum's 5-byte ACK)."""
        self.t.send(command)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pkt = self.t.recv_packet(max(0.05, deadline - time.monotonic()))
            if pkt is not None and pkt[2] == want_cmd:
                return pkt
        return None

    def connect(self):
        # DAQ-E: the serial number is the control-channel device name.
        if self.kind == "daq-e":
            self._control = self.t_control
            hello = self._control.cmd({"cmd": "hello"})
            self.fw = hello.get("fw")
            self.sensor_id = hello.get("name") or self.t.host
        self.t.open()
        # Wake the sensor.
        self._exchange(cmd_hello(), CMD_HELLO, timeout=5.0)
        # DAQ-U / DAQ-M: serial number comes from GetSensorId.
        if self.kind in ("daq-u", "daq-m"):
            pkt = self._exchange(cmd_get_id(), CMD_GET_ID, timeout=5.0)
            if pkt is None:
                raise RuntimeError(f"{self.kind}: no GetSensorId response "
                                   f"(is the sensor connected/awake?)")
            self.sensor_id = parse_sensor_id(pkt)
        # Liveness: confirm the wavelength grid responds (not stored; Chloros
        # gets the grid from the calibration bundle at import).
        self._exchange(cmd_get_wl(), CMD_GET_WL, timeout=5.0)
        if not self.sensor_id:
            raise RuntimeError(f"{self.kind}: could not read a serial number")
        return self.sensor_id

    def read_spectrum(self):
        """Trigger one acquisition and return
        (spectrum_floats, integration_ms, is_saturated). Raw counts, no cal."""
        # exposure can take up to integration_time * frame_avg + overhead
        timeout = 2.0 + (self.integration_ms * max(self.frame_avg, 1)) / 1000.0 * 2
        pkt = self._exchange(
            cmd_acquire(self.integration_ms, self.frame_avg, self.enable_ae),
            CMD_GET_SPEC, timeout=timeout)
        if pkt is None:
            raise TimeoutError("no spectrum returned within timeout")
        return parse_spectrum(pkt)

    def standby_and_close(self):
        # Graceful shutdown: ask the sensor to stand by, then close the link.
        try:
            self.t.send(cmd_standby())
            time.sleep(0.1)
        except Exception:
            pass
        try:
            self.t.close()
        except Exception:
            pass
        if self.kind == "daq-e":
            try:
                self.t_control.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CLI / record loop
# ---------------------------------------------------------------------------
def build_sensor(args):
    kind = {"u": "daq-u", "m": "daq-m", "e": "daq-e"}[args.device]
    if kind == "daq-u":
        if not args.port:
            raise SystemExit("DAQ-U needs --port (e.g. COM7 or /dev/ttyUSB0)")
        transport = SerialTransport(args.port)
    elif kind == "daq-m":
        if not args.mac:
            raise SystemExit("DAQ-M needs --mac (the sensor's BLE address)")
        transport = BleTransport(args.mac)
    else:  # daq-e
        if not args.host:
            raise SystemExit("DAQ-E needs --host (the sensor's IP address)")
        transport = TcpTransport(args.host, port=args.raw_port)
    sensor = DaqSensor(kind, transport, integration_ms=args.integration_time,
                       frame_avg=args.frame_avg, enable_ae=not args.no_ae)
    if kind == "daq-e":
        sensor.t_control = DaqEControl(args.host, port=args.control_port)
    return sensor


def default_output(kind):
    # UTC, matching every other naive wall-clock these scripts write (the
    # capture TIFF filenames/EXIF and the .daq's stamped utc_offset_minutes=0
    # declaration). A local-time name here would misdescribe the recording's
    # declared timezone -- the exact mixed-clock bug the CM5 hub once had.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{kind}_{stamp}.daq"


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Record RAW spectra from a MAPIR DAQ-U/M/E to a "
                    "Chloros-compatible .daq (no SDK, no calibration).")
    p.add_argument("device", choices=["u", "m", "e"],
                   help="which DAQ: u=USB serial, m=Bluetooth LE, e=Ethernet")
    p.add_argument("--port", help="DAQ-U serial port (COM7, /dev/ttyUSB0)")
    p.add_argument("--mac", help="DAQ-M BLE address (AA:BB:CC:DD:EE:FF)")
    p.add_argument("--host", help="DAQ-E IP address")
    p.add_argument("--control-port", type=int, default=5001,
                   help="DAQ-E JSON control TCP port (default 5001)")
    p.add_argument("--raw-port", type=int, default=5000,
                   help="DAQ-E raw spectral TCP port (default 5000)")
    p.add_argument("--integration-time", type=int, default=32,
                   help="integration time in ms (default 32)")
    p.add_argument("--frame-avg", type=int, default=3,
                   help="frames to average per reading (default 3)")
    p.add_argument("--no-ae", action="store_true",
                   help="disable auto-exposure (use fixed --integration-time)")
    p.add_argument("--frames", type=int, default=0,
                   help="stop after N readings (0 = until Ctrl-C)")
    p.add_argument("--duration", type=float, default=0.0,
                   help="stop after N seconds (0 = until Ctrl-C)")
    p.add_argument("--output", help="output .daq path (default <kind>_<ts>.daq)")
    p.add_argument("--device-name", default="", help="free-text label")
    args = p.parse_args(argv)

    kind = {"u": "daq-u", "m": "daq-m", "e": "daq-e"}[args.device]
    sensor = build_sensor(args)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    print(f"Connecting to {kind} ...", flush=True)
    serial_id = sensor.connect()
    print(f"  serial (cal key): {serial_id}"
          + (f"   fw: {sensor.fw}" if sensor.fw else ""), flush=True)

    out = args.output or default_output(kind)
    writer = DaqWriter(out, product_model=kind, product_serial=serial_id,
                       device_name=args.device_name)
    print(f"Recording RAW to: {os.path.abspath(out)}")
    print("  (Chloros will fetch this sensor's calibration by serial at import)")
    print("  Ctrl-C to stop.", flush=True)

    t0 = time.monotonic()
    n = 0
    try:
        while not stop.is_set():
            try:
                spec, inttime, sat = sensor.read_spectrum()
            except TimeoutError as e:
                print(f"  ! {e}", file=sys.stderr, flush=True)
                continue
            writer.write(spec, is_saturated=sat, integration_time_ms=inttime,
                         timestamp_ns=time.time_ns())
            n += 1
            if n % 10 == 0:
                print(f"  {n} readings ...", flush=True)
            if args.frames and n >= args.frames:
                break
            if args.duration and (time.monotonic() - t0) >= args.duration:
                break
    finally:
        writer.close()
        sensor.standby_and_close()
        print(f"Stopped. Wrote {n} readings to {os.path.abspath(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
