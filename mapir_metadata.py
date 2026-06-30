"""
mapir_metadata.py  --  the Chloros ingest contract for DIY capture.

This module writes raw LATTICE camera images and DAQ light-sensor recordings
with EXACTLY the metadata MAPIR Chloros reads on import, so data captured by
your own flight computer (not the MAPIR hub) processes correctly.

Design rules (do not break these -- they are the whole point):
  * NO processing. Pixels are written as the raw Bayer mosaic (M3C) or raw
    mono frame (M3M); spectra are written as raw sensor counts. No debayer,
    no calibration, no indices.
  * NO calibration is applied or required here. Chloros fetches each device's
    factory calibration FROM THE CLOUD at import time, keyed by the SERIAL
    NUMBER stamped below. Your only job is to stamp the right serial + model.
  * Pure Python. Depends only on numpy + Pillow (TIFF/EXIF) + stdlib sqlite3.
    No MAPIR package, no exiftool. Runs on Windows + Linux x86_64/arm64
    (Jetson, Raspberry Pi).

Two outputs:
  * write_lattice_raw_tiff(...) -> a raw .tiff a Chloros project will detect
    as LATTICE, group correctly, and calibrate by serial.
  * DaqWriter(...)              -> a .daq SQLite file Chloros matches to the
    imagery by timestamp and calibrates by serial.

Verified by validate_contract.py, which round-trips the output through
verbatim copies of the actual Chloros reader functions.
"""

import io
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# EXIF tag codes (see EXIF/TIFF/DNG specs). Kept as named constants so the
# write calls read clearly.
# ---------------------------------------------------------------------------
_TAG_MAKE = 0x010F                 # 271  IFD0
_TAG_MODEL = 0x0110                # 272  IFD0   (LATTICE detection key)
_TAG_CAMERA_SERIAL = 0xC62F        # 50735 IFD0 (DNG CameraSerialNumber; cal key)
_TAG_EXIF_IFD = 0x8769             # 34665 pointer to the EXIF sub-IFD
_TAG_EXPOSURE_TIME = 0x829A        # 33434 EXIF sub-IFD (RATIONAL, seconds)
_TAG_ISO = 0x8827                  # 34855 EXIF sub-IFD (SHORT)
_TAG_DATETIME_ORIGINAL = 0x9003    # 36867 EXIF sub-IFD
_TAG_SUBSEC_ORIGINAL = 0x9291      # 37521 EXIF sub-IFD (microseconds, as text)


# ===========================================================================
# LATTICE camera frames
# ===========================================================================

def lattice_capture_filename(serial, seq, when=None):
    """Return the canonical LATTICE capture basename + ``.tiff``.

    Chloros groups a multi-camera trigger by the part of the filename AFTER
    the serial, so every camera in one hardware-synced shot MUST share the
    same ``seq``. Pattern (parsed by project.py):

        capture_<serial>_<seq>_<YYYYMMDD>_<HHMMSS>_<subsec>.tiff

    Parameters
    ----------
    serial : str|int   camera serial (digits only in the filename)
    seq    : int       trigger sequence; SAME value across all cameras of one
                       synchronized capture
    when   : datetime  capture time (UTC recommended); defaults to now
    """
    if when is None:
        when = datetime.now(timezone.utc)
    serial_digits = re.sub(r"\D", "", str(serial)) or "0"
    stamp = when.strftime("%Y%m%d_%H%M%S")
    subsec = f"{when.microsecond:06d}"
    return f"capture_{serial_digits}_{int(seq):04d}_{stamp}_{subsec}.tiff"


def write_lattice_raw_tiff(path, pixels, *, model, serial,
                           exposure_s, iso=100, when=None):
    """Write one raw LATTICE frame + the metadata Chloros needs.

    Parameters
    ----------
    path : str
        Output path. Use lattice_capture_filename() for the basename so
        multi-camera grouping works.
    pixels : np.ndarray
        Raw sensor frame, uint16, 2-D (H, W):
          * M3C (color): the RAW BAYER MOSAIC (single channel, RGGB). Do NOT
            debayer -- Chloros debayers on import.
          * M3M (mono):  the single-channel mono frame.
        Values are the sensor's native bit depth (e.g. 12-bit, 0..4095),
        stored left in a uint16 container.
    model : str
        Full model string, e.g. "LATT-M3C-L41-FRGN" or "LATT-M3M-L41-F850".
        MUST start with "LATT-" (Chloros's LATTICE detection key). Read it
        from the camera: GenICam DeviceUserID gives "M3C-L41-FRGN"; prepend
        "LATT-".
    serial : str|int
        Camera serial (GenICam DeviceSerialNumber). THIS is the calibration
        fetch key -- Chloros pulls this camera's factory cal from the cloud
        by this exact serial. Get it right.
    exposure_s : float
        Exposure time in seconds (GenICam ExposureTime is microseconds:
        pass ExposureTime / 1e6).
    iso : int
        ISO equivalent. Chloros derives analog gain as 20*log10(ISO/100).
        Use 100 for 0 dB gain (the common scientific-capture case).
    when : datetime
        Capture time (UTC recommended). Defaults to now. Stamped as
        DateTimeOriginal + SubSecTimeOriginal.

    Note on compression: the TIFF is written UNCOMPRESSED. Pillow's in-TIFF
    DEFLATE/LZW paths go through libtiff, which cannot co-write the EXIF
    sub-IFD this contract needs (libtiff drops tag 34665), so we keep pixels
    uncompressed to guarantee the metadata survives on every platform. A full
    M3C/M3M frame is ~6.3 MB; compress at rest (zip / filesystem compression)
    if storage is tight. Chloros reads compressed or not.
    """
    arr = np.asarray(pixels)
    if arr.ndim != 2:
        raise ValueError(
            f"LATTICE raw frame must be 2-D (H, W) Bayer/mono, got shape "
            f"{arr.shape}. Do not debayer or stack channels.")
    if arr.dtype != np.uint16:
        arr = arr.astype(np.uint16)
    if not str(model).upper().startswith("LATT-"):
        raise ValueError(
            f"model must start with 'LATT-' (got {model!r}); Chloros uses "
            f"that prefix to detect LATTICE images.")
    if when is None:
        when = datetime.now(timezone.utc)

    img = Image.fromarray(arr)  # mode 'I;16'

    exif = Image.Exif()
    exif[_TAG_MAKE] = "MAPIR"
    exif[_TAG_MODEL] = str(model)
    exif[_TAG_CAMERA_SERIAL] = str(serial)
    sub = exif.get_ifd(_TAG_EXIF_IFD)
    sub[_TAG_EXPOSURE_TIME] = float(exposure_s)
    sub[_TAG_ISO] = int(iso)
    sub[_TAG_DATETIME_ORIGINAL] = when.strftime("%Y:%m:%d %H:%M:%S")
    sub[_TAG_SUBSEC_ORIGINAL] = f"{when.microsecond:06d}"

    # Uncompressed only -- see the compression note in the docstring. The
    # EXIF sub-IFD (exposure/ISO/timestamps) only survives Pillow's
    # uncompressed encoder; any compression routes through libtiff and drops
    # tag 34665.
    img.save(path, format="TIFF", exif=exif.tobytes())
    return path


# ===========================================================================
# DAQ light-sensor recordings (.daq)
# ===========================================================================
#
# A .daq is a SQLite database with two tables:
#   als_meta  -- one row: device identity + calibration provenance
#   als_log   -- one row per spectrum reading
#
# Chloros import (mip/daq_dls.py) reads als_meta to learn the device kind +
# serial + whether calibration was applied, then per reading reads
# precise_timestamp / spectral_data / is_saturated / integration_time from
# als_log (event_type = 3). Because we record RAW counts with
# calibration_applied = 0, Chloros fetches this sensor's factory cal by serial
# and applies it offline -- exactly like the live MAPIR recorder when no
# bundle is cached.

_ALS_META_DDL = """CREATE TABLE als_meta(
    version TEXT,
    product_model TEXT,
    product_serial TEXT,
    device_name TEXT,
    calibration_applied INTEGER,
    calibration_bundle_sha TEXT,
    calibration_completed_utc TEXT,
    cap_id TEXT,
    cap_applied INTEGER)"""

# Full als_log schema (matches the MAPIR recorder). A DIY raw recorder only
# fills event_type / precise_timestamp / spectral_data / is_saturated /
# integration_time; the photometric columns stay NULL (Chloros recomputes
# them from the calibrated spectrum at import).
_ALS_LOG_DDL = """CREATE TABLE als_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_on TIMESTAMP DATETIME DEFAULT(STRFTIME('%Y-%m-%d %H:%M:%f','NOW')) NOT NULL,
    precise_timestamp INTEGER,
    event_type INTEGER NOT NULL,
    spectral_data BLOB,
    is_saturated INTEGER,
    integration_time INTEGER)"""

_VALID_KINDS = ("daq-u", "daq-m", "daq-e")


def _spectrum_to_blob(spectrum):
    """Serialize a spectrum to the BLOB Chloros expects: raw bytes of
    ``np.save`` for a float32 array (import does ``np.load(BytesIO(blob))``)."""
    buf = io.BytesIO()
    np.save(buf, np.asarray(spectrum, dtype=np.float32))
    return buf.getvalue()


class DaqWriter:
    """Write a Chloros-compatible ``.daq`` of RAW spectral counts.

    Usage::

        w = DaqWriter("flight.daq", product_model="daq-u",
                      product_serial=sensor_serial)
        # for each reading streamed off the sensor:
        w.write(spectrum_counts, is_saturated, integration_time_ms,
                timestamp_ns=time.time_ns())
        w.close()

    Parameters
    ----------
    product_model : str   one of 'daq-u' / 'daq-m' / 'daq-e' (the device kind;
                          Chloros maps it to the right cal bundle family).
    product_serial : str  the sensor's serial/id. THE CALIBRATION FETCH KEY.
    device_name : str     free-text label (optional).
    cap_id : str          MAPIR cap-correction profile id if a cosine
                          corrector / FOV cone is fitted, else 'none' for a
                          bare sensor. Recorded raw (cap_applied=0); Chloros
                          applies it at import. Leave 'none' unless MAPIR told
                          you a specific cap id.
    """

    def __init__(self, path, *, product_model, product_serial,
                 device_name="", cap_id="none"):
        kind = str(product_model).strip().lower()
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"product_model must be one of {_VALID_KINDS}, got "
                f"{product_model!r}")
        if not str(product_serial).strip():
            raise ValueError(
                "product_serial is required -- it is the calibration fetch "
                "key. Read it from the sensor (get_sensor_id).")
        self._path = path
        self._conn = sqlite3.connect(path)
        cur = self._conn.cursor()
        cur.execute(_ALS_META_DDL)
        cur.execute(_ALS_LOG_DDL)
        # Raw recording: empty calibration sha -> calibration_applied = 0 so
        # Chloros calibrates at import by serial. cap recorded but not applied.
        cur.execute(
            "INSERT INTO als_meta (version, product_model, product_serial, "
            "device_name, calibration_applied, calibration_bundle_sha, "
            "calibration_completed_utc, cap_id, cap_applied) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("1.22", kind, str(product_serial).strip(), str(device_name),
             0, "", "", (cap_id or "none"), 0))
        self._conn.commit()
        self._count = 0

    def write(self, spectrum, is_saturated, integration_time_ms,
              timestamp_ns=None):
        """Append one raw spectrum reading.

        spectrum : sequence/np.ndarray of raw sensor counts (the sensor's
                   firmware-output spectrum, BEFORE any calibration).
        is_saturated : bool
        integration_time_ms : int  the integration time used for this frame
                   (Chloros needs it for the integration-aware dark model).
        timestamp_ns : int  ABSOLUTE wall-clock nanoseconds since the Unix
                   epoch (time.time_ns()). Chloros matches the DAQ to imagery
                   by absolute time, so do NOT use a monotonic clock. Keep the
                   host clock reasonably accurate during the flight.
        """
        if self._conn is None:
            raise RuntimeError("DaqWriter is closed")
        if timestamp_ns is None:
            timestamp_ns = time.time_ns()
        self._conn.execute(
            "INSERT INTO als_log (event_type, precise_timestamp, "
            "spectral_data, is_saturated, integration_time) VALUES (?,?,?,?,?)",
            (3, int(timestamp_ns), _spectrum_to_blob(spectrum),
             int(bool(is_saturated)), int(integration_time_ms)))
        self._count += 1
        if self._count % 10 == 0:
            self._conn.commit()

    @property
    def record_count(self):
        return self._count

    def close(self):
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
