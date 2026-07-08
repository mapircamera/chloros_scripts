# chloros_scripts

Reference Python scripts for **capturing raw data from MAPIR LATTICE cameras
and DAQ light sensors with your own computer**, then processing it in
[Chloros](https://www.mapir.camera/collections/software/products/chloros) afterward.

These are deliberately small, dependency-light, and heavily commented — meant
to be read, copied, and adapted (including as a reference for AI-assisted
coding) for DIY drone and research setups.

> **The idea:** you capture **raw** data in the field; **Chloros calibrates and
> processes it later**. The scripts apply *no* processing and *no* calibration.
> They stamp each file with the device's **serial number and model**, and on
> import Chloros fetches that exact device's factory calibration from the cloud
> and applies it. Capture is yours; the science is handled at import.

## What's here

| File | Purpose |
|------|---------|
| `capture_lattice.py` | Control + raw capture from **LATTICE cameras** (M3C/M3M), with hardware-cable multi-camera sync |
| `record_daq.py` | Record raw spectra from a **DAQ-U / DAQ-M / DAQ-E** to a Chloros-compatible `.daq` |
| `mapir_metadata.py` | The Chloros ingest contract: writes raw LATTICE TIFFs + the `.daq` SQLite format |
| `selftest.py` | Self-contained checks that the output matches what Chloros reads on import |
| `requirements.txt` | Dependencies |

## Install

Cross-platform: Windows, Linux x86-64, and Linux arm64 (NVIDIA Jetson,
Raspberry Pi).

```bash
python -m pip install -r requirements.txt
```

- **DAQ-U** (USB serial) needs `pyserial`. On Linux add yourself to the
  `dialout` group for serial access: `sudo usermod -aG dialout $USER` (re-login).
- **DAQ-M** (Bluetooth LE) needs `bleak`. On Linux it uses BlueZ
  (`sudo apt install bluez`); Jetson/Raspberry Pi work out of the box.
- **DAQ-E** (Ethernet) needs nothing beyond the standard library.
- Running `selftest.py` additionally needs `tifffile` (`pip install tifffile`)
  — only to *read back* TIFFs the way Chloros does; the capture scripts don't
  need it.

### Cameras / Arena SDK

`capture_lattice.py` talks to the cameras through the **Arena SDK** and its
`arena_api` Python wrapper. `arena_api` is **not** installable from PyPI on its
own — install the native Arena SDK for your platform first (Windows, and Linux
x86-64 / arm64 including Jetson and Raspberry Pi builds), then its Python
package. Put the host NIC on the cameras' subnet and enable jumbo frames if your
switch supports them. `record_daq.py` does **not** need the Arena SDK.

## Hardware requirements

These scripts do **no image processing** — they just move raw data to disk — so
CPU load is low. What matters is the device interface, a bit of RAM, and (for
cameras) write speed. The two scripts have very different needs.

**DAQ recording (`record_daq.py`)** — tiny footprint: a few hundred small
readings per second, parsed and written to SQLite.

| | Recommendation |
|---|---|
| **Minimum** | Python 3.8+, ~256 MB free RAM, and the sensor's interface (USB for DAQ-U, Bluetooth LE for DAQ-M, Ethernet for DAQ-E). A **Raspberry Pi Zero 2 W** handles it. |
| **Ideal** | Any Raspberry Pi 4 / 5 or Jetson — far more than enough. |

**Camera capture (`capture_lattice.py`)** — the demanding one. The Arena SDK
sets a hard floor: it ships only for **64-bit ARM (aarch64) and x86-64** (no
32-bit / ARMv6 / ARMv7 build), and the cameras are wired Gigabit Ethernet.

| | Recommendation |
|---|---|
| **Minimum** | A **64-bit OS** on 64-bit ARM or x86-64, Gigabit Ethernet (onboard, or a USB-to-Gigabit adapter — available for essentially every platform; use a USB 3 port for full bandwidth, since USB 2.0 caps ~480 Mbps), ~1 GB free RAM for one camera (budget ≈70 MB more per additional camera for frame buffers). Board floor: **Raspberry Pi 4 (2 GB+) on 64-bit Raspberry Pi OS / Ubuntu.** A microSD card is fine for a single camera at low frame rates. |
| **Ideal** | **NVIDIA Jetson Orin Nano / NX / AGX** (or an x86-64 mini-PC), 4 GB+ RAM, with an **SSD** (USB 3 / NVMe). Arrays need the SSD: frames are uncompressed (~6.3 MB each), so e.g. 5 cameras at 2 fps is ~60 MB/s of sustained writes a microSD card can't keep up with. |

> **A Raspberry Pi Zero cannot run the cameras** — it's ARMv6, which the Arena
> SDK doesn't support. (A board without onboard Ethernet can always add it with
> a USB-Gigabit adapter, but that doesn't get around the ARMv6 limitation.) The
> smallest practical camera host is a Raspberry Pi 4 on a 64-bit OS; a Jetson is
> the smoothest ARM path and matches what most users already fly. Storage and
> network bandwidth both scale with camera count × frame rate, so step up to a
> Jetson or x86-64 host for larger arrays.

## Usage — `capture_lattice.py` (cameras)

```bash
# single camera, auto-exposure, 50 frames
python capture_lattice.py --frames 50

# multi-camera array, HARDWARE cable sync, pick the master by serial
python capture_lattice.py --sync cable --master 213602328 --interval 1.0

# fixed 5 ms exposure, run until Ctrl-C
python capture_lattice.py --exposure-us 5000
```

| Option | Meaning | Default |
|--------|---------|---------|
| `--sync cable\|software` | `cable` = hardware M8 sync; `software` = single-cam / no-cable | `cable` |
| `--master SERIAL` | master camera for cable sync | lowest serial |
| `--serials A,B,…` | use only these cameras | all connected |
| `--exposure-us N` | fixed exposure (µs) | auto-exposure |
| `--frames N` / `--duration S` / `--interval S` | stop after N shots / S seconds / wait S between shots | until Ctrl-C |
| `--output-dir DIR` | where to write TIFFs | `.` |

**Hardware sync.** With `--sync cable`, the master camera is software-triggered
and drives an `ExposureActive` pulse out on Line2; every slave triggers off that
edge over the **MAPIR M8 sync cable** (pin 2 → Line2). That gives sub-frame,
simultaneous exposure across the array with no PTP — fine for a single cabled
rig. (Syncing cameras that *aren't* cabled together, or measuring the exact
skew, is what PTP is for; that's not implemented here.) Frames are saved
uncompressed (~6.3 MB each) so the required EXIF survives; Chloros debayers and
calibrates on import.

## Usage — `record_daq.py`

```bash
# DAQ-U over USB serial
python record_daq.py u --port COM7              # Windows
python record_daq.py u --port /dev/ttyUSB0      # Linux / Jetson / Pi

# DAQ-M over Bluetooth LE (the sensor's BLE address)
python record_daq.py m --mac AA:BB:CC:DD:EE:FF

# DAQ-E over Ethernet (the sensor's IP)
python record_daq.py e --host 192.168.1.50
```

Common options:

| Option | Meaning | Default |
|--------|---------|---------|
| `--integration-time MS` | integration time per reading (ms) | 32 |
| `--frame-avg N` | frames averaged per reading | 3 |
| `--no-ae` | disable auto-exposure (use fixed integration time) | AE on |
| `--frames N` | stop after N readings | until Ctrl-C |
| `--duration S` | stop after S seconds | until Ctrl-C |
| `--output PATH` | output `.daq` path | `<kind>_<timestamp>.daq` |

Press **Ctrl-C** to stop. The script records continuously; mount the sensor
upward-facing (downwelling) and run it for the whole flight.

## How Chloros uses your files

- **Serial number is the key.** Each `.daq` (and each LATTICE TIFF) carries the
  device serial. Chloros looks up that exact device's factory calibration in the
  cloud and applies it at import. Get the device powered and discoverable so the
  scripts can read its real serial.
- **Reflectance needs downwelling + a synced clock.** Chloros matches a DAQ
  recording to imagery **by timestamp**. Record a DAQ during the flight and keep
  the host clock reasonably accurate (the scripts stamp absolute UTC time). With
  no DAQ you still get radiance, not reflectance.
- **Timezone is declared, not guessed.** Naive wall-clock stamps are ambiguous,
  so the TIFFs carry EXIF `OffsetTimeOriginal = +00:00` and the `.daq` carries
  `als_meta.utc_offset_minutes = 0` (schema v1.23) — the scripts stamp UTC
  everywhere, and say so. Chloros reads the declarations, so image↔DAQ matching
  works on any processing host with **no** 'Light sensor timezone offset'
  setting (the same contract the MAPIR CM5 hub stamps). If you adapt the
  scripts to stamp local time, update both declarations (see
  `mapir_metadata.py`).
- **Raw means raw.** Spectra are the sensor's raw firmware output (no
  calibration); `calibration_applied = 0` tells Chloros to calibrate on import.

## Notes

- **DAQ-E** uses the JSON control channel (TCP 5001) to read the serial and the
  raw spectral channel (TCP 5000) to acquire; timestamps are host wall-clock.
- **Multi-camera sync** is hardware, over the M8 sync cables — the master drives
  the array off one trigger line for sub-frame simultaneous exposure. (Syncing
  cameras that aren't cabled into a single chain is outside the scope of these
  scripts; a single cabled rig doesn't need it.)
- LATTICE TIFFs are written **uncompressed** (~6.3 MB per full-res frame) so the
  required EXIF survives on every platform. Compress at rest if storage is tight.
- `selftest.py` checks that everything these scripts write matches what Chloros
  reads on import, and that the DAQ wire codec and camera configuration are
  correct. Run it any time: `python selftest.py`.

## License & support

MIT licensed (see [LICENSE](LICENSE)); provided as-is, without warranty. For help
with MAPIR hardware or Chloros, contact
[MAPIR support](https://www.mapir.camera/community/contact).
