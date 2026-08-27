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
>
> And Chloros **hands the result back**: every `.daq` you import is written out
> again, calibrated, as a `.daq` *and* a `.csv` of spectral irradiance — see
> [Light sensor only](#light-sensor-only--getting-a-calibrated-csv-out-of-chloros).
> That works for **DAQ-U, DAQ-M and DAQ-E alike**, and needs no camera.

## What's here

| File | Purpose |
|------|---------|
| `capture_lattice.py` | Control + raw capture from **LATTICE cameras** (M3C/M3M), with hardware-cable multi-camera sync |
| `record_daq.py` | Record raw spectra from a **DAQ-U / DAQ-M / DAQ-E** to a Chloros-compatible `.daq` |
| `daq_stream.py` | Listen to **any number** of DAQ-E / DAQ-E-S sensors over multicast (raw or calibrated) |
| `daq_cal.py` | Apply a **DAQ-E**'s onboard factory calibration offline — no cloud, no Chloros |
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
| `--model STR` | model to fall back to when a camera's `DeviceUserID` is empty (factory reset) | read from the camera |
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

## Light sensor only — getting a calibrated `.csv` out of Chloros

Plenty of setups are **just a light sensor**: no camera, no imagery, no
reflectance — you want spectral irradiance, PPFD or lux over time, calibrated.
That is a first-class workflow, and a project with no images in it is a valid
project. Capture raw in the field, then let Chloros calibrate:

```bash
python record_daq.py u --port COM7 --duration 3600
```

Then take the `.daq` files to any one of:

- **Desktop app** — make a project, drag the `.daq` files in, press **Process**.
  Nothing camera-specific to configure.
- **CLI** — `chloros-cli process ./my_daq_files -o ./my_project`
- **SDK** — see below.

Chloros fetches each sensor's factory calibration **by serial** (local cache
first, then the MAPIR cloud), applies it, and writes two products per recording
into a `Light Sensor` folder inside the project:

```
<project>/
└── Light Sensor/
    ├── u_20260826_143012_calibrated.daq   # reprocessable archive
    └── u_20260826_143012_calibrated.csv   # W/m²/nm + photometrics
```

The `.csv` is one row per reading:

| Columns | |
|---|---|
| `timestamp_utc`, `timestamp_ns` | absolute UTC epoch of the reading |
| `integration_time_ms`, `calibrated` | acquisition + per-frame calibration flag |
| `total_power_W_m2` | integrated irradiance |
| `photopic_lux`, `scotopic_lux` | photometric illuminance |
| `ppfd_umol_m2_s`, `ppfd_blue`, `ppfd_green`, `ppfd_red` | PAR photon flux, total and split |
| `peak_wavelength_nm` | spectral peak |
| `340.0` … `1010.0` | the full spectrum, W/m²/nm, 135 points at 5 nm |

The `.daq` beside it is the same SQLite format these scripts write, now carrying
calibrated spectra and declaring the bundle that produced them — so re-importing
it does **not** calibrate it a second time.

From the SDK:

```python
import chloros_sdk

with chloros_sdk.ChlorosLocal() as cl:
    cl.create_project("DAQ-U_2026-08-26")
    cl.import_images("./my_daq_files")     # .daq files; no imagery needed
    result = cl.export_light_sensor()

for rec in result["exported"]:
    print(rec["csv"])
for rec in result["skipped"]:
    print("skipped", rec["source"], "--", rec["reason"])
```

> The **CLI and SDK need a paid Chloros+ plan** (enforced server-side). The
> desktop app route does not — it works on the free tier.

> **A recording whose calibration can't be fetched is skipped, not faked.** If
> you are offline, or that serial has no calibration on file, Chloros reports
> the recording under `skipped` **with the reason** and writes nothing for it —
> rather than emitting a file named `*_calibrated.csv` that holds raw counts.
> Reconnect, re-run, and it completes.

Your original raw `.daq` is never modified; the products are written alongside
it. Keep the raw one — it is the master, and stays re-calibratable against a
future coefficient revision.

## DAQ-E data channels — what exists, and what these scripts use

A DAQ-E on firmware **1.7.0+** emits two spectral streams on separate multicast
groups: **raw** counts (always) and **calibrated** W/m²/nm (once the device
carries coefficients — a DAQ-E-S always does). Older firmware emits raw only.

Raw is always the reprocessable one: it is the sensor's firmware output byte
for byte, so a recording made from it can be re-calibrated later against a
revised bundle. Prefer it for anything you intend to keep.

| Channel | Wire | Content | These scripts |
|---------|------|---------|---------------|
| Raw, unicast | TCP `5000` | raw counts, **one client at a time** | ✅ `record_daq.py` |
| Raw, multicast | UDP `239.10.10.10:5002` | raw counts, any number of listeners | ✅ `daq_stream.py` |
| Calibrated, multicast | UDP `239.10.10.11:5003` | W/m²/nm, when the device carries coefficients | ✅ `daq_stream.py --calibrated` |
| IMU, multicast | UDP `239.10.10.12:5004` | attitude at its own rate, independent of spectra (fw 1.8.0+) | ➖ not read by these scripts |
| Control | TCP `5001` | JSON: config, status, bundle/profile/cert | ✅ `daq_cal.py` |

Full datagram layout in [`PROTOCOL.md`](https://github.com/mapircamera/ESP32/blob/main/PROTOCOL.md).

**What you can record, and to what.**

| Stream | `.daq` | `.csv` | How |
|---|:---:|:---:|---|
| Raw spectra | ✅ | ✅ | `record_daq.py <k> --csv` (any model) or `daq_stream.py --daq --csv` (DAQ-E) |
| Calibrated spectra | ✅ | ✅ | `daq_stream.py --calibrated --daq --csv`, or `record_daq.py e --calibrate bake`/`csv` |
| Attitude (IMU) | ✅ | ✅ | `daq_stream.py --imu --daq --csv`; the per-frame trailer also lands in a spectral `.daq`'s `imu_*` columns automatically |

A calibrated recording is **stamped as calibrated** (`calibration_applied = 1`)
from the frame's own flag bit, not from which group you joined — so Chloros
imports it as-is instead of applying its bundle a second time.

Attitude arrives two ways, and both are recorded:

- **On each spectral frame**, as a 22-byte trailer. It lands in that reading's
  own `imu_*` columns, so the tilt sits beside the irradiance it qualifies. Free
  — no extra rows — but limited to the ~2.5 Hz spectra arrive at.
- **On its own stream**, at the accelerometer's rate (~50 Hz). `--imu` records
  it as `event_type = 4` rows carrying no spectrum. Chloros's spectral readers
  never see them: `mip/daq_dls` predicates on `event_type = 3 AND spectral_data
  IS NOT NULL`, and `mip/als.py` skips rows whose blob will not decode. Nothing
  in Chloros reads them back *yet* — they are recorded so the data exists on the
  same clock and in the same file as the spectra, instead of stranded in a
  sidecar.

**`--imu-rate` decides how much of that you keep, and it defaults to 5 Hz.**
50 Hz is ten times the rows in both outputs and more than most work needs, so
samples are thinned by their own timestamps (not by a frame counter, which
would drift whenever the device's actual rate did). Raise it for vibration or
fast-attitude work; `--imu-rate 0` keeps every sample.

```bash
# the default: 5 Hz, small files
python daq_stream.py --imu --daq attitude.daq --csv attitude.csv

# everything the device sends
python daq_stream.py --imu --imu-rate 0 --daq attitude.daq
```

Measured on a 50 Hz stream: 5 Hz keeps a tenth of the samples and about a tenth
of the CSV bytes.

Raw remains the reprocessable master either way: a coefficient revision reaches
every raw recording you kept and none of the device-calibrated ones.

**The IMU trailer.** On firmware **1.8.0+** — which is every DAQ-E-S — a
spectral frame can carry a 22-byte attitude trailer appended *after* the CRC,
announced by flags bit 4. It sits outside the CRC deliberately, so a corrupt
trailer can never cost you a good spectrum; the corollary is that **a reader
must tolerate the extra bytes**. A reader that checks for an exact datagram
length instead rejects every frame such a unit sends, counts them as malformed,
and shows the sensor as absent while it streams perfectly. `daq_stream.py`
accepts them and reports the trailer's presence in the `imu` CSV column; it
does not decode the attitude itself — that layout lives in
[`PROTOCOL.md`](https://github.com/mapircamera/ESP32/blob/main/PROTOCOL.md),
and Chloros decodes it.

**Chloros reads the raw stream, not the calibrated one.** It subscribes only to
`239.10.10.10:5002` and applies the bundle *host-side*, from its own cloud
cache — and it drops a calibrated frame arriving on the raw group as a firmware
bug rather than feeding W/m²/nm into a path expecting counts. Two consequences
worth knowing:

- The **calibrated stream is for third-party consumers** — anything that can't
  carry the calibration machinery, which is what `daq_stream.py --calibrated`
  is for. It is not the path Chloros processes, so a difference between it and
  a Chloros product is a stale on-device profile, not a Chloros bug.
- Chloros always re-derives from raw, which is why raw is **the reprocessable
  one**: a coefficient revision reaches every raw recording you kept, and none
  of the device-calibrated ones.

## Many sensors at once — `daq_stream.py`

```bash
python daq_stream.py                         # every sensor on the network
python daq_stream.py --calibrated            # W/m²/nm straight off the device
python daq_stream.py --serial 11-22-33-44-55 --csv out.csv
```

Multicast, so any number of consumers can read the same sensor and any number
of sensors can share a group. The TCP raw channel `record_daq.py` uses is
exclusive — one client — so this is the path for multi-sensor work and for
running alongside Chloros.

**Sensor separation.** Datagram v2 (firmware 1.7.0+) carries the sender's MAC,
serial, model and per-frame integration time, so frames are self-describing and
the script demultiplexes on identity.

Older firmware emits v1, which carries none of that: two v1 units on one group
are separable only by UDP source address, and a receiver that doesn't filter
reads a **~50/50 blend of both while looking perfectly healthy**. That is not
hypothetical — it happened on hardware on 2026-07-14. `daq_stream.py` keys v1
frames on source IP and prints a warning, but the real fix is updating the
firmware.

**Timestamps.** `timestamp_us` is latched on the ESP32 as the sensor's last
byte arrives, so it excludes network and OS jitter. When a frame reports PTP
sync, clocks across sensors are disciplined to a common grandmaster (~50 µs on
this hardware) and frames from different units are directly comparable — that's
what makes multi-sensor and sensor-to-LATTICE alignment meaningful.

**Every frame says which stream it came from.** Raw counts and calibrated
W/m²/nm differ by roughly four orders of magnitude, and nothing about the
numbers themselves announces which you are holding. So the CSV carries a
`calibrated` column, a `units` column and an `imu` column per row — taken from the **frame's own
flag bit**, not from the group the script joined — plus a `#` provenance line
naming the group and units at the top. If a frame's flag ever contradicts its
group (a firmware bug: the two groups are meant to be exclusive), the script
warns on stderr and records what actually arrived.

**Two meanings of "calibrated".** The `--calibrated` stream above is computed
*by the device*. `--calibrate` in `record_daq.py` (below) applies the bundle
*locally in Python*. Both run the same arithmetic — the device runs a
pre-folded version of it — so use whichever fits: the stream needs no
calibration machinery on your side, the local path works on any firmware.

They agree **provided the profile document on the unit is current**. The device
folds in whatever bundle and cap profile Chloros last pushed to it, so a unit
carrying a stale or wrong-cap document emits a plausible number that is out by
the whole geometry correction — up to ~11× for a sunshine cap. `daq_cal.py
<host>` prints exactly what is aboard; see [Caps](#caps).

## Calibrated output with no cloud and no Chloros — DAQ-E only

A DAQ-E carries its own factory calibration bundle in flash. `--calibrate`
pulls it down over ethernet and applies it locally, so you get spectral
irradiance in **W/m²/nm** without an account, an internet connection, or a
Chloros install.

> This is the **air-gapped** route, and it is DAQ-E only — the bundle has to be
> on the device. If you have Chloros, you do not need it: importing a raw `.daq`
> produces the same calibrated `.csv` for **any** DAQ model, including DAQ-U and
> DAQ-M, which have no onboard bundle to read. See
> [Light sensor only](#light-sensor-only--getting-a-calibrated-csv-out-of-chloros).

```bash
# .daq stays RAW; calibrated irradiance goes to a sibling .csv
python record_daq.py e --host 192.168.1.50 --calibrate csv

# just look at what the device is carrying (records nothing)
python daq_cal.py 192.168.1.50
```

| `--calibrate` | `.daq` contents | When to use |
|---------------|-----------------|-------------|
| `off` (default) | raw counts | **Recommended.** Chloros calibrates at import and writes the `.csv` for you |
| `csv` | raw counts, **plus** a calibrated `.csv` | you need numbers in the field, with no Chloros and no internet |
| `bake` | calibrated W/m²/nm, stamped with the bundle SHA | the recording must stand completely alone |

Prefer a raw `.daq` over `bake` in every case where you have a choice. A raw
recording can be re-calibrated later if a coefficient revision lands; a baked
one is frozen at whatever the bundle said the day you recorded, and no amount
of reprocessing gets that back.

`csv` and `bake` both still import fine. Chloros reads a baked recording as-is
rather than calibrating it twice, and re-exports it into `Light Sensor/`
alongside everything else — so a baked file loses the ability to be
*re-calibrated*, not the ability to be *used*.

### Caps

A cosine corrector or FOV cone has its own per-wavelength correction, and even
a **bare** DAQ-E has a geometry correction (the bare diffuser over-reads
directional light ~2×, while a sunshine cap is near-ideal). None of that is in
the calibration bundle — it lives in Chloros and is versioned separately.

**Every recording declares a cap, and says who decided it.** On DAQ-U /
DAQ-M / DAQ-E the sunshine corrector is **removable**, and nothing on the
sensor can sense whether it is fitted — so somebody has to say. `record_daq.py`
assumes `sunshine_cosine`, because that is how the large majority of units fly
and it is the same assumption Chloros makes; a DAQ-E-S gets `as_recorded`
instead, and that one is not an assumption (its diffuser is genuinely permanent
and was on the unit when its factory bundle was measured, so no profile may
apply on top).

Crucially the file records **which of those it was**, in
`als_meta.cap_id_source`:

| value | meaning |
|---|---|
| `auto_default` | **assumed** — nobody said, the fleet default was used |
| `operator` | you stated it with `--cap-id` |
| `device` | read back from the unit's own profile store |
| `model` | settled by the hardware (DAQ-E-S) |

That distinction is what makes an assumed cap **undoable**. Chloros warns on
`auto_default` — naming the file — and an operator can override the cap per
project and get the corrected number back, because a raw recording has not had
the cap multiplied in yet. A file that could not say whether anyone checked
would leave a 20-30× error looking exactly like a verified one.

The cap is recorded as *provenance* — `cap_applied = 0` — and Chloros applies
it at import.

Pass `--cap-id none` **only** for a sensor you have physically stripped.
Declaring bare on a capped unit is not "uncorrected", it is wrong by the whole
correction, and nothing downstream can detect it:

| Model | `sunshine_cosine` | declaring `none` instead |
|-------|------------------|--------------------------|
| DAQ-U | ×30.6 | **~30× low** |
| DAQ-M | ×23.1 | **~23× low** |
| DAQ-E | ×11.0 | **~22.6× low** — on a DAQ-E `none` is an *active* ×0.49 bare-geometry profile, not a no-op |

`record_daq.py` prints the cap it declared on every run, and warns when that is
`none`.

#### Setting the cap on a DAQ-E

A DAQ-E stores one resolved cap profile and folds it into the calibrated
stream it publishes, so that store is what decides whether an offline consumer
of that stream gets the truth. You can write it:

```bash
# see what the unit is currently applying
python daq_cal.py 192.168.1.50

# save the document it holds (the curve, verbatim)
python daq_cal.py 192.168.1.50 --save-profiles aboard.json

# apply NO per-wavelength profile at all -- needs no curve
python daq_cal.py 192.168.1.50 --set-cap as_recorded

# fit a different cap: supply that cap's curve, which Chloros ships
python daq_cal.py 192.168.1.50 --set-cap fov_45        --cap-profile /path/to/chloros/daq/cap_profiles/e/fov_45.json
```

The curve has to come from somewhere — the device applies what it is given and
carries no library to look one up in, so **every id except `as_recorded` needs
its `--cap-profile` JSON**. Chloros ships them at
`daq/cap_profiles/<kind>/<cap_id>.json`; copy the one you need. Asking for a
real cap without its curve is refused rather than written as bare.

Requires firmware **1.6.0+** (`set_profiles`). The document written is
byte-identical to the one Chloros builds for the same cap, so setting a cap
here does not make Chloros rewrite the unit's flash on its next connect.

> **Chloros still has the last word.** It re-pushes its own resolved cap
> whenever it connects and finds a different document. If the cap must survive,
> set it in Chloros as well — this is for units that never meet a Chloros
> install, or for checking what one is carrying.

The **profile curves themselves** are authored in Chloros, not here. `daq_cal.py
<host>` prints which cap is aboard.

| Situation | Result |
|-----------|--------|
| Device carries the right profile | correct output, matches Chloros exactly |
| Device carries **no** profile | **bare-uncorrected** — ~2× off bare, ~11× off under a sunshine cap |
| Physical cap changed | re-push from Chloros; the scripts cannot substitute another cap's curve |

`--cap-id` means two different things depending on whether the run reads the
device:

- **With `--calibrate` (DAQ-E)** it is an override of the profile the device
  carries, and the device carries exactly one — so the only accepted values are
  `as_recorded` (skip every per-wavelength profile) or that same cap. Naming a
  different one is **refused**: there is no local copy of its curve, and
  applying the stored one under another name is silently wrong by the
  sunshine-vs-bare factor. Validated at startup, not mid-recording.
- **Without `--calibrate`** (every raw recording, and the only mode DAQ-U /
  DAQ-M have) nothing is applied locally, so `--cap-id` simply *declares* what
  is fitted, for Chloros to apply at import. Any id Chloros knows for that
  device kind is accepted — `sunshine_cosine` (the default), `none`,
  `fov_15`/`45`/`90`, and `fov_30`/`60` on DAQ-U.

#### What each mode stamps

The two flags Chloros reads are `calibration_applied` (are these W/m²/nm or
counts?) and `cap_applied` (is a cap correction already multiplied in?).
Between them they make every combination unambiguous, so nothing is ever
calibrated or capped twice:

| Run | `calibration_applied` | `cap_id` | `cap_applied` | Chloros applies |
|-----|:---:|---|:---:|---|
| `record_daq.py u/m/e` (default) | 0 | `sunshine_cosine` | 0 | bundle **+** that cap |
| ... on a DAQ-E-S | 0 | `as_recorded` | 0 | bundle only, no profile |
| ... `--cap-id none` (stripped) | 0 | `none` | 0 | bundle **+** bare geometry (DAQ-E) or nothing (U/M) |
| `--calibrate csv` | 0 | the device's cap | 0 | bundle + that cap — and the sibling `.csv` already has both |
| `--calibrate bake` | 1 | the device's cap | 1 | **nothing** — imported as-is |
| `--calibrate bake --cap-id as_recorded` | 1 | `as_recorded` | 0 | nothing; a later cap override can still be applied |

The last row is why `cap_applied` follows the cap actually handed to the
calibrator rather than merely whether the device had profiles: `as_recorded`
skips every profile, so stamping it as applied would claim a correction that
is not in the data — and Chloros would then *refuse* a later operator override
(it cannot undo a curve that was never applied) instead of correcting the file.

Use `--require-profiles` to refuse outright rather than log a plausible wrong
number when a cap is fitted but no profile is aboard:

```bash
python record_daq.py e --host 192.168.1.50 --calibrate csv --require-profiles
```

None of this applies to a **DAQ-E-S** — the one model whose diffuser really is
permanent. It was on the unit when its factory bundle was measured — the correction is
already inside the gain. Chloros resolves that unit to `as_recorded` (no
per-wavelength profile of any kind) and **overrides** any cap you ask for,
because there is no cap choice to respect on optics that do not come off.
Putting `sunshine_cosine` on top double-counts the diffuser: measured at ~11×,
which integrates to 6× the solar constant above the atmosphere — physically
impossible, and the tell that it has happened.

Use `daq_cal.py <host>` to print exactly which correction chain a unit will
run — dark model, whether π is applied at runtime or baked into the gain, and
which cap profile (if any) is aboard. Programmatically:

```python
from daq_cal import DeviceCalibration
cal = DeviceCalibration.from_device("192.168.1.50")
print(cal.describe())
watts = cal.apply(raw_counts, integration_time_ms=50)
```

Always pass this frame's own `integration_time_ms` — auto-exposure moves it
between 1 and 500 ms and the dark model is a function of it.

## How Chloros uses your files

- **Serial number is the key.** Each `.daq` (and each LATTICE TIFF) carries the
  device serial. Chloros looks up that exact device's factory calibration in the
  cloud and applies it at import. Get the device powered and discoverable so the
  scripts can read its real serial.
- **Reflectance needs downwelling + a synced clock.** Chloros matches a DAQ
  recording to imagery **by timestamp**. Record a DAQ during the flight and keep
  the host clock reasonably accurate (the scripts stamp absolute UTC time). With
  no DAQ you still get radiance, not reflectance.
- **You get the calibration back, not just its effect.** Importing a `.daq`
  writes `<project>/Light Sensor/<name>_calibrated.daq` and `.csv` — the
  calibrated spectra as a file, rather than only as an intermediate on the way
  to reflectance. No camera required; a light-sensor-only project is a valid
  project. See
  [Light sensor only](#light-sensor-only--getting-a-calibrated-csv-out-of-chloros).
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
  The one exception is `--calibrate bake`, which sets `calibration_applied = 1`
  and carries the bundle SHA — Chloros then imports those spectra as-is instead
  of calibrating them a second time. That flag is the *only* thing standing
  between a calibrated file and being calibrated twice, so if you adapt
  `mapir_metadata.py` to write your own calibrated recordings, set it — and set
  `calibration_bundle_sha` with it, which `DaqWriter` enforces.
- **Your raw file is never modified.** Chloros writes derived products
  alongside it and leaves the recording alone, so the raw `.daq` remains the
  master copy. Archive that one.

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
