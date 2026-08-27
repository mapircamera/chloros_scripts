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

Timezone contract (so reflectance matching works on ANY processing host):
naive wall-clock stamps are ambiguous, so both outputs DECLARE their zone --
the TIFF via EXIF OffsetTimeOriginal ("+00:00" for the default UTC capture
time) and the .daq via als_meta.utc_offset_minutes (v1.23; 0 = UTC). This is
the same convention the MAPIR CM5 hub stamps; without it Chloros would parse
the naive stamps in the processing host's local zone and the operator would
have to configure a manual 'Light sensor timezone offset'.

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
_TAG_OFFSET_ORIGINAL = 0x9011      # 36881 EXIF sub-IFD ("+00:00"; tz of
                                   #       DateTimeOriginal -- see below)
_TAG_SUBSEC_ORIGINAL = 0x9291      # 37521 EXIF sub-IFD (microseconds, as text)


def _utc_offset_string(when):
    """EXIF ``OffsetTime*`` string ("+HH:MM") for *when*'s timezone.

    Timezone-aware datetimes report their own offset (the default UTC
    capture time -> "+00:00"); a naive datetime is taken as HOST-LOCAL time
    (Python's convention for a bare ``datetime.now()``) and reports the
    host zone at that instant, DST included.
    """
    off = (when if when.tzinfo is not None else when.astimezone()).utcoffset()
    total = int(round((off.total_seconds() if off is not None else 0) / 60.0))
    sign = "-" if total < 0 else "+"
    return f"{sign}{abs(total) // 60:02d}:{abs(total) % 60:02d}"


def utc_offset_minutes(when=None):
    """Signed minutes east of UTC for *when* (default: host-local now).

    This is the value ``DaqWriter`` stamps into ``als_meta.utc_offset_minutes``
    -- the ONE field Chloros checks to know what timezone a recording
    system's naive wall-clock stamps (filenames, EXIF DateTime) are in.
    These scripts stamp UTC everywhere, so their recordings carry 0.
    """
    when = when or datetime.now().astimezone()
    off = (when if when.tzinfo is not None else when.astimezone()).utcoffset()
    return int(round((off.total_seconds() if off is not None else 0) / 60.0))


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
        DateTimeOriginal + SubSecTimeOriginal, PLUS OffsetTimeOriginal
        declaring the timestamp's timezone ("+00:00" for the default UTC).
        Chloros reads the declaration, so DAQ<->image reflectance matching
        works on any processing host with no manual 'Light sensor timezone
        offset' -- the same contract the MAPIR CM5 hub stamps (fw >= 1.4.1).
        A NAIVE `when` is taken as host-local time and declared as such.

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
    # Declare the timestamp's timezone (EXIF 2.31). DateTimeOriginal above is
    # a NAIVE wall-clock string; without this tag Chloros must assume a zone
    # when matching the image to a .daq by time (host-local by default --
    # wrong by the UTC offset for these UTC-stamped captures on most hosts).
    # "+00:00" here makes the image side self-describing.
    sub[_TAG_OFFSET_ORIGINAL] = _utc_offset_string(when)
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
    cap_applied INTEGER,
    cap_id_source TEXT,
    utc_offset_minutes INTEGER)"""

# Who decided ``cap_id``. The values are Chloros's (mip/daq_dls.py reads this
# column and warns on the assumed ones), so do not invent new ones:
#
#   'operator'     the caller stated it (--cap-id). A human looked.
#   'device'       read back from the unit's own profile store.
#   'model'        forced by the hardware -- a DAQ-E-S has no removable cap.
#   'auto_default' ASSUMED: nobody said, so the fleet default was used.
#
# The distinction is the whole point. A capped-vs-bare mistake is 20-30x in
# downwelling and no downstream check can catch it, so a file has to be able
# to say "this was a guess" -- that is what lets Chloros warn, and what lets
# an operator override it later instead of trusting a number nobody verified.
_CAP_ID_SOURCES = ("operator", "device", "model", "auto_default")

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
    integration_time INTEGER,
    device_ts_ns INTEGER,
    device_ts_ptp INTEGER,
    imu_trailer_version INTEGER,
    imu_flags INTEGER,
    imu_cal_applied INTEGER,
    imu_sample_age_ms INTEGER,
    imu_x_mg REAL,
    imu_y_mg REAL,
    imu_z_mg REAL,
    imu_mag_mg REAL,
    imu_tilt_deg REAL,
    imu_roll_deg REAL,
    imu_pitch_deg REAL,
    imu_temp_raw INTEGER)"""

# Columns beyond the five a minimal recorder fills. Names and meanings are
# Chloros's (daq/sensors/base.py DAQULogger.write) -- a .daq is one format, so
# a column that means something different here than there is worse than no
# column. NOTE Chloros's import path does not currently READ the imu_* set;
# its own recorder writes them as provenance and so do we.
_IMU_COLUMNS = (
    "imu_trailer_version", "imu_flags", "imu_cal_applied", "imu_sample_age_ms",
    "imu_x_mg", "imu_y_mg", "imu_z_mg", "imu_mag_mg",
    "imu_tilt_deg", "imu_roll_deg", "imu_pitch_deg", "imu_temp_raw")

_VALID_KINDS = ("daq-u", "daq-m", "daq-e", "daq-e-s")

# The cap correction Chloros will apply when a recording does not say
# otherwise, per device model.
#
# On DAQ-U / DAQ-M / DAQ-E the sunshine cosine corrector is REMOVABLE, and
# nothing on the sensor can sense whether it is fitted. The default here is an
# ASSUMPTION, chosen because it is how >90% of MAPIR users fly -- the same
# assumption Chloros's own recorder makes (backend_server.py
# /api/daq/connect), so a raw recording written here and one written by
# Chloros describe identical hardware identically.
#
# The assumption is recorded as such: cap_id_source='auto_default' when it was
# assumed, 'operator' when the caller stated it. That distinction is what
# makes it UNDOABLE -- Chloros warns on an assumed cap and an operator can
# override it per project, which is impossible if the file cannot say whether
# anyone ever looked at the sensor. Never stamp 'operator' for a guess.
#
# Getting it wrong is large: mean 30.6x on DAQ-U, 23.1x on DAQ-M, 11.0x on
# DAQ-E. A recording that declares 'none' on a capped sensor is not
# "uncorrected", it is wrong by that factor -- and on a DAQ-E worse still,
# because 'none' is an ACTIVE bare-geometry profile of ~0.49x rather than a
# no-op, making the round trip ~22.6x.
#
# The DAQ-E-S is the one exception, and the only model whose diffuser is
# genuinely permanent: it was on the unit when the factory bundle was
# measured, so the correction is already inside the gain and NO per-wavelength
# profile may apply on top. That is not an assumption, so it is stamped
# 'model' -- Chloros resolves the same model to 'as_recorded' and overrides
# any cap asked for.
_DEFAULT_CAP_BY_KIND = {
    "daq-u": "sunshine_cosine",
    "daq-m": "sunshine_cosine",
    "daq-e": "sunshine_cosine",
    "daq-e-s": "as_recorded",
}


def _as_int(v):
    """int(v), or None. NaN and non-numerics become None, never 0."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else int(f)


def _as_float(v):
    """float(v), or None. A NaN angle means "undefined" in some drivers and
    is absent in others; a column that means "the angle was undefined" should
    not depend on which."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


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
    product_model : str   one of 'daq-u' / 'daq-m' / 'daq-e' / 'daq-e-s'
                          (the device kind; Chloros maps it to the right cal
                          bundle family). Record a DAQ-E-S as itself, not as
                          'daq-e': both use the kind-'e' bundle, but their cap
                          treatment is OPPOSITE -- a DAQ-E-S carries its
                          diffuser inside its own gain and must get no
                          per-wavelength profile, while a plain DAQ-E needs
                          the sunshine curve (~11x). A file that cannot tell
                          them apart cannot be processed correctly.
    product_serial : str  the sensor's serial/id. THE CALIBRATION FETCH KEY.
    device_name : str     free-text label (optional).
    cap_id : str          Which cap correction Chloros should apply at
                          import. Recorded as provenance only
                          (``cap_applied=0``); Chloros applies it.

                          **Default (None) resolves to the model's shipped
                          state** -- ``sunshine_cosine`` for DAQ-U / DAQ-M /
                          DAQ-E, ``as_recorded`` for DAQ-E-S -- because MAPIR
                          ships the sunshine cosine corrector permanently
                          installed, and Chloros's own recorder defaults to
                          the same thing. Pass ``'none'`` ONLY for a sensor
                          you have physically stripped: on a capped unit that
                          declaration is wrong by the whole correction (mean
                          30.6x on DAQ-U, 23.1x on DAQ-M, and ~22.6x on a
                          DAQ-E, where 'none' is an ACTIVE ~0.49x
                          bare-geometry profile rather than a no-op).
                          ``'as_recorded'`` means "apply no per-wavelength
                          profile at all".
    cap_id_source : str   WHO decided ``cap_id`` -- ``'operator'`` (a human
                          stated it), ``'device'`` (read from the unit's
                          profile store), ``'model'`` (the hardware settles
                          it, i.e. DAQ-E-S), or ``'auto_default'`` (assumed,
                          nobody said). Defaults to ``'operator'`` when
                          ``cap_id`` is given and ``'auto_default'`` when it
                          is not, which is almost always what you want.

                          This is what makes an assumed cap UNDOABLE rather
                          than merely wrong: Chloros warns on 'auto_default'
                          and lets the operator override it per project. Never
                          claim 'operator' for a guess -- that is the one
                          value that tells a later reader not to re-examine
                          it.
    tz_offset_minutes : int
                          Timezone provenance (als_meta v1.23): the UTC
                          offset, in signed minutes, of the NAIVE wall-clock
                          stamps your capture system writes (image/daq
                          filenames, EXIF DateTime). It is the ONE field
                          Chloros checks to line the .daq up with imagery on
                          any processing host -- no manual 'Light sensor
                          timezone offset' setting. These scripts stamp UTC
                          everywhere, so the default is 0 (same convention as
                          the MAPIR CM5 hub). If you adapt them to stamp
                          LOCAL time instead, pass
                          ``utc_offset_minutes()`` (this module) so the
                          declaration stays truthful. The spectrum timestamps
                          themselves (``timestamp_ns``) are absolute epochs
                          and are NOT affected by this value.
    calibration_applied : bool
                          True when the spectra handed to :meth:`write` are
                          already calibrated W/m^2/nm rather than raw counts.
                          Chloros then imports them unchanged instead of
                          applying a bundle. Requires
                          ``calibration_bundle_sha``. Default False (raw),
                          which is what you want unless the recording must be
                          readable with no calibration source at all --
                          raw files stay reprocessable against future bundle
                          revisions, baked ones do not.
    calibration_bundle_sha : str
                          SHA-256 of the bundle that produced the calibrated
                          values. Mandatory when ``calibration_applied``;
                          it is the only record of which chain ran.
    calibration_completed_utc : str
                          The bundle's own ``completed_utc``, carried through
                          for audit.
    cap_applied : bool    True when a cap / geometry per-wavelength profile
                          was folded into the written spectra. Only meaningful
                          alongside ``calibration_applied``.
    """

    def __init__(self, path, *, product_model, product_serial,
                 device_name="", cap_id=None, cap_id_source=None,
                 tz_offset_minutes=0,
                 calibration_applied=False, calibration_bundle_sha="",
                 calibration_completed_utc="", cap_applied=False):
        kind = str(product_model).strip().lower()
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"product_model must be one of {_VALID_KINDS}, got "
                f"{product_model!r}")
        # None (the default) means "whatever this model ships wearing".
        # Resolved here rather than defaulted to 'none' in the signature: a
        # bare declaration on a capped sensor is a 20-30x error that nothing
        # downstream can detect, and every unit MAPIR ships is capped.
        # cap_id and its provenance resolve together: leaving cap_id to the
        # default IS the assumption, so it can only ever be 'auto_default'
        # (or 'model' where the hardware settles it). A caller that states a
        # cap without saying where it came from is taken at its word --
        # 'operator' -- because stating one is itself an act of declaring.
        if cap_id is None:
            cap_id = _DEFAULT_CAP_BY_KIND[kind]
            if cap_id_source is None:
                cap_id_source = "model" if kind == "daq-e-s" else "auto_default"
        elif cap_id_source is None:
            cap_id_source = "operator"
        if cap_id_source not in _CAP_ID_SOURCES:
            raise ValueError(
                f"cap_id_source must be one of {_CAP_ID_SOURCES}, got "
                f"{cap_id_source!r} -- these are the values Chloros reads; "
                f"an unknown one makes the provenance unreadable.")
        # Readable back off the writer so a caller can PRINT what it is about
        # to declare. The cap is the one field an operator can get wrong from
        # the outside -- the sensor cannot sense what is screwed onto it -- so
        # a recorder that resolves a default silently is hiding the single
        # most consequential thing in the file.
        self._cap_id = cap_id
        if not str(product_serial).strip():
            raise ValueError(
                "product_serial is required -- it is the calibration fetch "
                "key. Read it from the sensor (get_sensor_id).")
        self._path = path
        # Each DaqWriter starts a fresh recording. If the path already exists,
        # replace it -- the tables can't be created over an existing database
        # (you'd get "table als_meta already exists"). record_daq's default
        # filename is timestamped, so this only matters if you reuse --output.
        if os.path.exists(path):
            os.remove(path)
        self._conn = sqlite3.connect(path)
        cur = self._conn.cursor()
        cur.execute(_ALS_META_DDL)
        cur.execute(_ALS_LOG_DDL)
        # Raw recording (the default): empty calibration sha ->
        # calibration_applied = 0 so Chloros calibrates at import by serial.
        # cap recorded but not applied.
        #
        # When the caller has already applied a bundle -- record_daq's
        # ``--calibrate bake``, which uses the bundle stored on the DAQ-E
        # itself -- these flip to 1 and carry the bundle sha, so Chloros
        # imports the spectra as-is instead of calibrating them a second
        # time. Baked files are NOT reprocessable against a future bundle
        # revision; prefer raw unless the recording has to stand alone.
        #
        # v1.23: utc_offset_minutes declares the timezone of the capture
        # system's naive wall-clock stamps (0 = UTC, this project's
        # convention) so Chloros needs no manual timezone setting.
        if calibration_applied and not str(calibration_bundle_sha).strip():
            raise ValueError(
                "calibration_applied=True requires calibration_bundle_sha -- "
                "an applied calibration with no provenance cannot be audited "
                "or reprocessed.")
        cur.execute(
            "INSERT INTO als_meta (version, product_model, product_serial, "
            "device_name, calibration_applied, calibration_bundle_sha, "
            "calibration_completed_utc, cap_id, cap_applied, cap_id_source, "
            "utc_offset_minutes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            # 1.25: cap_id_source. Matches the hub's schema number for the
            # same column, so two files claiming one version have one shape.
            ("1.27", kind, str(product_serial).strip(), str(device_name),
             int(bool(calibration_applied)),
             str(calibration_bundle_sha or ""),
             str(calibration_completed_utc or ""),
             cap_id, int(bool(cap_applied)), cap_id_source,
             int(tz_offset_minutes)))
        self._conn.commit()
        self._count = 0

    def write(self, spectrum, is_saturated, integration_time_ms,
              timestamp_ns=None, *, device_timestamp_ns=None,
              device_ts_ptp=None, imu=None):
        """Append one spectrum reading.

        spectrum : sequence/np.ndarray of raw sensor counts (the sensor's
                   firmware-output spectrum, BEFORE any calibration) -- or,
                   when the writer was opened with ``calibration_applied``,
                   calibrated spectral irradiance in W/m^2/nm.
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
        # The sensor's own clock, normalised at this ONE choke point so no
        # caller can produce a row whose flag and stamp disagree. A pre-2000
        # value is a boot counter or unscaled microseconds, not an epoch:
        # recording it would look absolute to a reader and drag the whole file
        # onto a nonsense time axis, so refuse it and keep NULL.
        dev_ts = dev_ptp = None
        if device_timestamp_ns:
            _v = int(device_timestamp_ns)
            if _v > 946684800_000_000_000:            # 2000-01-01 in ns
                dev_ts = _v
                dev_ptp = 1 if device_ts_ptp else 0

        # Attitude, same rule: the caller hands over a trailer dict, this
        # decides what a column may contain. No default-to-zero anywhere -- a
        # missing angle is NULL, never 0.0, because 0 degrees means PERFECTLY
        # LEVEL and that is the single most dangerous value to invent for a
        # column whose whole purpose is deciding whether a cosine correction
        # held.
        if isinstance(imu, dict):
            imu_row = (
                _as_int(imu.get("trailer_version")),
                _as_int(imu.get("flags")),
                1 if imu.get("cal_applied") else 0,
                _as_int(imu.get("sample_age_ms")),
                _as_float(imu.get("x_mg")), _as_float(imu.get("y_mg")),
                _as_float(imu.get("z_mg")), _as_float(imu.get("mag_mg")),
                _as_float(imu.get("tilt_deg")), _as_float(imu.get("roll_deg")),
                _as_float(imu.get("pitch_deg")), _as_int(imu.get("temp_raw")),
            )
        else:
            # Twelve NULLs, imu_cal_applied among them: with no trailer the
            # question "were these axes corrected" has no answer, and 0 would
            # assert the wrong one.
            imu_row = (None,) * 12

        self._conn.execute(
            "INSERT INTO als_log (event_type, precise_timestamp, "
            "spectral_data, is_saturated, integration_time, "
            "device_ts_ns, device_ts_ptp, " + ", ".join(_IMU_COLUMNS) + ") "
            "VALUES (?,?,?,?,?, ?,?, ?,?,?,?,?,?,?,?,?,?,?,?)",
            (3, int(timestamp_ns), _spectrum_to_blob(spectrum),
             int(bool(is_saturated)), int(integration_time_ms),
             dev_ts, dev_ptp) + imu_row)
        self._count += 1
        if self._count % 10 == 0:
            self._conn.commit()

    @property
    def cap_id(self):
        """The cap id actually stamped, after the model default resolved."""
        return self._cap_id

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
