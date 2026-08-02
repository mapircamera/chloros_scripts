#!/usr/bin/env python3
"""
daq_cal.py -- apply a MAPIR DAQ factory calibration entirely offline, using
the bundle the DAQ-E already carries in its own flash.

No AWS. No Chloros. No account. The DAQ-E stores its factory calibration
bundle (and, from firmware v1.6.0, the cap / geometry profiles that go with
it) on an onboard LittleFS partition and serves them over the JSON control
channel. This module pulls them down and reproduces the same radiometric
transform Chloros applies at import, so a user with nothing but this repo and
an ethernet cable gets calibrated spectral irradiance in W/m^2/nm.

    from daq_cal import DeviceCalibration
    cal = DeviceCalibration.from_device("192.168.1.50")
    watts = cal.apply(raw_counts, integration_time_ms=50)

DAQ-U / DAQ-M have no onboard store (no filesystem, no ethernet), so this
module also accepts a bundle handed to it from anywhere else:

    cal = DeviceCalibration.from_bundle(json.load(open("bundle.json")))

Correspondence with Chloros
---------------------------
The transform below is a deliberate mirror of chloros's
``daq/calibration_apply.py`` -- specifically ``DAQCalibration.effective_dark``
and ``DAQCalibration.apply``. Both operate on float32 through the same
sequence of operations, so for the same inputs this module and Chloros agree
to the bit. If you change one, change the other; the tests in
``selftest.py`` compare against captured reference vectors.

The chain is::

    dark_eff(t) = rate + offset_per_ms / t        (per-unit decomposition)
                | dark_mean * ((1-f) + f/t)       (fleet-fraction fallback)
                | dark_mean                       (legacy fixed scalar)

    out = (raw - dark_eff(t)) * gain
    out = out * pi                                (unless baked into gain)
    out = max(out, 0)
    out = out * cap_correction                    (unless bare / as_recorded)

Why pi: the factory gain is anchored to integrating-sphere *wall radiance*,
so the radiance -> hemispherical-irradiance conversion (E = pi * L) has to be
restored at apply time. Bundles regenerated with it already folded into the
gain set ``irradiance_geometry_factor_applied`` and we skip it, so it is
never applied twice.

Why the zero clamp: in bands with no light (NIR under fluorescents, deep blue
indoors) the value is pure dark-subtraction residue, and gain * pi turns each
residual raw count into a few mW/m^2/nm -- enough to draw a visibly negative
tail. Clamped once, before the cap step, because cap factors are positive so
the sign is already decided at that point.
"""

from __future__ import annotations

import json
import math
import socket
import threading
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

__all__ = [
    "DeviceCalibration",
    "CalibrationError",
    "DaqEControlClient",
    "CAP_ID_NONE",
    "CAP_ID_AS_RECORDED",
]


class CalibrationError(RuntimeError):
    """Bundle / profile is missing, malformed, or for the wrong sensor."""


# Radiance -> hemispherical irradiance. Mirrors
# chloros daq/calibration_apply.py::_RADIANCE_TO_IRRADIANCE_HEMISPHERICAL.
_RADIANCE_TO_IRRADIANCE_HEMISPHERICAL = float(np.pi)

# "Bare sensor." NOT automatically a no-op: since the 2026-07-13 geometry
# adjudication the bare state may carry a measured geometry correction
# (correction = G_bare(lambda) / pi), because the bare diffuser over-reads
# directional light by roughly 2x while the sunshine cap is near-ideal. If
# the device carries a 'none' profile we apply it; if it carries none, bare
# stays uncorrected -- bit-identical to pre-adjudication behaviour.
CAP_ID_NONE = "none"

# "Whatever was already baked in." Marks a recording whose stored spectra are
# bare-EQUIVALENT values from a physically capped sensor: pi alone is right,
# and neither a cap curve nor the bare geometry profile may be applied.
CAP_ID_AS_RECORDED = "as_recorded"

# Profile documents this module understands. The firmware stores the document
# verbatim and does not parse it, so this version gate is the only contract.
PROFILES_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Control channel
# ---------------------------------------------------------------------------
class DaqEControlClient:
    """Minimal DAQ-E JSON control-channel client (TCP 5001), stdlib only.

    ``record_daq.py`` has its own trimmed copy for the connect handshake; this
    one exists so ``daq_cal`` is usable standalone, and because the bundle and
    profile responses are large enough (5-30 KB) to need a real line reader
    rather than the byte-at-a-time loop that is fine for a status blob.
    """

    def __init__(self, host: str, port: int = 5001, timeout: float = 5.0,
                 token: str = ""):
        self.host = host
        self.port = port
        self._token = token
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.settimeout(timeout)
        self._buf = bytearray()
        self._lock = threading.Lock()

    def cmd(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        """Send one command, return the parsed response dict."""
        if self._token and obj.get("cmd") != "hello":
            obj = dict(obj, token=self._token)
        payload = (json.dumps(obj) + "\n").encode()
        with self._lock:
            self._sock.sendall(payload)
            # Responses are newline-delimited. get_calibration can return
            # ~30 KB on one line, so read in blocks rather than per byte.
            while b"\n" not in self._buf:
                chunk = self._sock.recv(65536)
                if not chunk:
                    raise ConnectionError(
                        "DAQ-E closed the control connection mid-response")
                self._buf.extend(chunk)
            line, _, rest = bytes(self._buf).partition(b"\n")
            self._buf = bytearray(rest)
        return json.loads(line.decode("utf-8"))

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "DaqEControlClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _require_ok(resp: Dict[str, Any], what: str) -> Dict[str, Any]:
    if not resp.get("ok"):
        code = resp.get("code", "")
        err = resp.get("error", "unknown error")
        raise CalibrationError(f"{what} failed: {err}"
                               + (f" [{code}]" if code else ""))
    return resp


# ---------------------------------------------------------------------------
# Cap / geometry profile
# ---------------------------------------------------------------------------
class CapProfile:
    """A per-wavelength multiplicative correction for a fitted cap, or for the
    bare-diffuser geometry.

    Mirrors chloros ``daq/calibration_apply.py::CapProfile``. ``is_noop`` is
    true only for the *synthesised* fallback (nothing installed) -- a loaded
    'none' profile is a real correction and must be applied.
    """

    def __init__(self, *, cap_id: str, correction: Optional[np.ndarray],
                 wavelength_nm: Optional[List[float]] = None):
        self._cap_id = cap_id
        self._correction = correction
        self._wavelength_nm = list(wavelength_nm or [])

    @property
    def cap_id(self) -> str:
        return self._cap_id

    @property
    def is_noop(self) -> bool:
        return self._correction is None

    @property
    def correction(self) -> Optional[np.ndarray]:
        return self._correction

    @classmethod
    def noop(cls, cap_id: str = CAP_ID_NONE) -> "CapProfile":
        return cls(cap_id=cap_id, correction=None)

    @classmethod
    def from_json(cls, data: Dict[str, Any], *,
                  expect_cap_id: Optional[str] = None) -> "CapProfile":
        cap_id = str(data.get("cap_id") or expect_cap_id or CAP_ID_NONE)
        if (expect_cap_id and data.get("cap_id")
                and data["cap_id"] != expect_cap_id):
            raise CalibrationError(
                f"cap profile id mismatch: document declares "
                f"{data['cap_id']!r}, expected {expect_cap_id!r}")

        corr_list = data.get("correction_mean")
        if not isinstance(corr_list, list) or not corr_list:
            raise CalibrationError(
                f"cap profile {cap_id!r} is missing correction_mean")
        corr = np.asarray(corr_list, dtype=np.float32)
        # Bins the fleet aggregator marked NaN (below the noise floor on some
        # contributing run) pass through uncorrected rather than zeroing the
        # spectrum.
        corr = np.where(np.isfinite(corr), corr, 1.0).astype(np.float32)

        wl = data.get("wavelength_grid_nm") or []
        if not isinstance(wl, list) or len(wl) != len(corr):
            raise CalibrationError(
                f"cap profile {cap_id!r}: wavelength grid length "
                f"{len(wl)} does not match correction length {len(corr)}")
        return cls(cap_id=cap_id, correction=corr, wavelength_nm=wl)


# ---------------------------------------------------------------------------
# The calibration transform
# ---------------------------------------------------------------------------
class DeviceCalibration:
    """Radiometric transform built from a DAQ factory bundle.

    Construct with :meth:`from_device` (pulls everything off a DAQ-E over
    ethernet) or :meth:`from_bundle` (any bundle dict, e.g. one saved to disk
    or read from a DAQ-U cache).
    """

    def __init__(self, *, gain: np.ndarray, wavelength_nm: List[float],
                 dark_mean: float, dark_rate: Optional[float] = None,
                 dark_offset_per_ms: Optional[float] = None,
                 dark_offset_fraction: Optional[float] = None,
                 irradiance_geometry_corrected: bool = False,
                 cap_profile: Optional[CapProfile] = None,
                 cap_id: str = CAP_ID_NONE,
                 bundle_sha: str = "", completed_utc: str = "",
                 device_kind: str = "", sensor_id: str = "",
                 profiles_source: str = "none"):
        self._gain = gain.astype(np.float32, copy=False)
        self._wavelength_nm = list(wavelength_nm)
        self._dark = float(dark_mean)
        self._dark_rate = dark_rate
        self._dark_offset_per_ms = dark_offset_per_ms
        self._dark_integration_aware = (dark_rate is not None
                                        and dark_offset_per_ms is not None)
        # The fleet fraction only ever substitutes for a missing per-unit
        # decomposition. When the bundle has its own (better) rate/offset it
        # is ignored outright, matching chloros.
        self._dark_offset_fraction = (
            dark_offset_fraction
            if (dark_offset_fraction is not None
                and not self._dark_integration_aware)
            else None
        )
        self._irradiance_geometry_corrected = bool(
            irradiance_geometry_corrected)
        self._cap_profile = cap_profile or CapProfile.noop()
        self._cap_id = cap_id
        self._bundle_sha = bundle_sha
        self._completed_utc = completed_utc
        self._device_kind = device_kind
        self._sensor_id = sensor_id
        self._profiles_source = profiles_source

    # -- introspection ------------------------------------------------------
    @property
    def wavelength_nm(self) -> List[float]:
        return self._wavelength_nm

    @property
    def n_points(self) -> int:
        return int(self._gain.shape[0])

    @property
    def bundle_sha(self) -> str:
        return self._bundle_sha

    @property
    def completed_utc(self) -> str:
        return self._completed_utc

    @property
    def sensor_id(self) -> str:
        return self._sensor_id

    @property
    def device_kind(self) -> str:
        return self._device_kind

    @property
    def cap_id(self) -> str:
        return self._cap_id

    @property
    def profiles_source(self) -> str:
        """Where the cap/geometry profile came from: ``"device"`` when the
        DAQ-E served one, ``"explicit"`` when the caller supplied it, or
        ``"none"`` when no profile is in play (bare, uncorrected)."""
        return self._profiles_source

    @property
    def dark_model(self) -> str:
        """Which dark model :meth:`effective_dark` uses. Diagnostic -- print
        this when a user reports a discrepancy against Chloros."""
        if self._dark_integration_aware:
            return "rate_plus_offset_over_t"
        if self._dark_offset_fraction is not None:
            return "fleet_fraction"
        return "fixed_scalar"

    def describe(self) -> str:
        """One-paragraph human summary. Worth printing at connect so a user
        can see exactly which correction chain produced their numbers."""
        cap = (f"{self._cap_id} (loaded)" if not self._cap_profile.is_noop
               else f"{self._cap_id} (no profile -- uncorrected)")
        return (
            f"sensor {self._sensor_id or '?'} kind={self._device_kind or '?'}\n"
            f"  bundle    {self._bundle_sha[:16] or '?'}... "
            f"completed {self._completed_utc or '?'}\n"
            f"  points    {self.n_points}  "
            f"({self._wavelength_nm[0]:.0f}-{self._wavelength_nm[-1]:.0f} nm)\n"
            f"  dark      {self.dark_model}\n"
            f"  pi        {'baked into gain' if self._irradiance_geometry_corrected else 'applied at runtime'}\n"
            f"  cap       {cap}  [profiles from: {self._profiles_source}]"
        )

    # -- the transform ------------------------------------------------------
    def effective_dark(self, integration_time_ms: Optional[float] = None
                       ) -> float:
        """Dark value subtracted for this integration time, in raw units.

        Three tiers, in precedence order -- identical to Chloros:

        1. per-unit decomposition: ``rate + offset_per_ms / t``
        2. fleet-fraction fallback: ``dark_mean * ((1-f) + f/t)``
        3. fixed scalar: ``dark_mean``

        The fixed read-offset component shrinks as 1/t in tiers 1 and 2
        because the NSP32 normalises its reported spectrum by integration
        time. Tiers 2 and 3 both reduce to ``dark_mean`` at t = 1 ms, so the
        change is a no-op at the calibration floor and only matters at the
        32-500 ms integrations auto-exposure actually uses in the field.
        """
        valid_t = (integration_time_ms is not None
                   and float(integration_time_ms) > 0.0)
        if self._dark_integration_aware and valid_t:
            return (self._dark_rate
                    + self._dark_offset_per_ms / float(integration_time_ms))
        if self._dark_offset_fraction is not None and valid_t:
            f = self._dark_offset_fraction
            return self._dark * ((1.0 - f) + f / float(integration_time_ms))
        return self._dark

    def apply(self, raw_spectrum: Sequence[float], *,
              integration_time_ms: Optional[float] = None,
              cap_id: Optional[str] = None) -> np.ndarray:
        """Return calibrated spectral irradiance (W/m^2/nm) for one raw frame.

        ``integration_time_ms`` is this frame's integration time -- the sensor
        reports it in every GetSpectrum response, and auto-exposure varies it
        from 1 to 500 ms. Pass it. Omitting it silently falls back to the
        fixed-scalar dark, which over-subtracts at long integrations, biases
        the result low, and clips near-floor bands to zero.

        ``cap_id`` overrides the profile selection for this call. The device
        carries exactly ONE resolved profile -- the cap chloros was told this
        unit has -- so the only overrides that can be honoured are:

        * ``"as_recorded"`` -- skip every per-wavelength profile (pi only).
        * the cap the device actually carries (i.e. a no-op override).

        Naming any OTHER cap raises, rather than applying the stored curve
        under a different name. There is no local copy of another cap's
        correction to substitute, and silently applying the wrong one is a
        ~11x error between a sunshine cap and bare. If the physical cap
        changed, re-push profiles from chloros.
        """
        arr = np.asarray(raw_spectrum, dtype=np.float32)
        if arr.shape != self._gain.shape:
            raise CalibrationError(
                f"raw spectrum length {arr.shape[0]} does not match gain "
                f"length {self._gain.shape[0]} -- this bundle is for a "
                f"different sensor variant")

        dark = self.effective_dark(integration_time_ms)
        out = (arr - dark) * self._gain
        if not self._irradiance_geometry_corrected:
            out = out * _RADIANCE_TO_IRRADIANCE_HEMISPHERICAL
        np.maximum(out, 0.0, out=out)

        effective_cap = self._cap_id if cap_id is None else cap_id
        if effective_cap == CAP_ID_AS_RECORDED:
            return out
        if self._cap_profile.is_noop:
            # Nothing aboard to apply. An override naming a real cap can't be
            # satisfied either, and quietly returning bare-uncorrected values
            # for a capped sensor is the ~11x failure this guard exists for.
            if effective_cap not in (CAP_ID_NONE, self._cap_id):
                raise CalibrationError(
                    f"cap_id={effective_cap!r} requested, but this device "
                    f"carries no cap profile at all. Push profiles from "
                    f"chloros for the fitted cap, or use "
                    f"cap_id='as_recorded'.")
            return out
        if effective_cap != self._cap_profile.cap_id:
            raise CalibrationError(
                f"cap_id={effective_cap!r} requested, but this device carries "
                f"the {self._cap_profile.cap_id!r} profile and there is no "
                f"local copy of any other cap's correction curve. Applying "
                f"the stored curve under a different name would be silently "
                f"wrong (sunshine vs bare is ~11x). Re-push profiles from "
                f"chloros for the cap actually fitted, or use "
                f"cap_id='as_recorded' to skip all per-wavelength profiles.")
        corr = self._cap_profile.correction
        if corr.shape != self._gain.shape:
            raise CalibrationError(
                f"cap profile {self._cap_profile.cap_id!r} length "
                f"{corr.shape[0]} does not match gain length "
                f"{self._gain.shape[0]}")
        return out * corr

    # -- constructors -------------------------------------------------------
    @classmethod
    def from_bundle(cls, bundle: Dict[str, Any], *,
                    profiles: Optional[Dict[str, Any]] = None,
                    bundle_sha: str = "",
                    sensor_id: str = "") -> "DeviceCalibration":
        """Build from a raw bundle dict (+ optional profiles document).

        Bundle field paths mirror chloros
        ``DAQCalibration.load_from_cache_dir`` exactly, including the
        fallbacks for older bundles.
        """
        stages = bundle.get("stages") or {}
        radiometric = stages.get("radiometric") or {}
        dark = stages.get("dark") or {}

        gain_list = radiometric.get("gain_per_wavelength")
        if not isinstance(gain_list, list) or not gain_list:
            raise CalibrationError(
                "bundle is missing stages.radiometric.gain_per_wavelength")
        gain = np.asarray(gain_list, dtype=np.float32)
        if np.any(~np.isfinite(gain)):
            raise CalibrationError(
                "bundle gain_per_wavelength contains non-finite values")

        # Older bundles pre-subtract dark inside gain; absent means zero.
        dark_mean = dark.get("daq_dark_mean_w_per_m2_per_nm")
        if dark_mean is None:
            dark_mean = 0.0
        try:
            dark_mean = float(dark_mean)
        except (TypeError, ValueError) as exc:
            raise CalibrationError(
                f"bundle dark offset is not numeric: {dark_mean!r}") from exc

        # Integration-aware decomposition. Both terms must be present and
        # finite, else we keep the legacy fixed-scalar path bit-for-bit.
        dark_rate = dark.get("daq_dark_rate_w_per_m2_per_nm")
        dark_off = dark.get("daq_dark_offset_per_ms_w_per_m2_per_nm")
        if dark_rate is not None and dark_off is not None:
            try:
                dr, do = float(dark_rate), float(dark_off)
            except (TypeError, ValueError):
                dr = do = None
            if dr is None or not (math.isfinite(dr) and math.isfinite(do)):
                dark_rate = dark_off = None
            else:
                dark_rate, dark_off = dr, do
        else:
            dark_rate = dark_off = None

        wavelength_nm = (
            (stages.get("wavelength_alignment") or {})
            .get("corrected_wavelength_grid_nm")
            or radiometric.get("wavelength_grid_nm")
            or bundle.get("device_factory_wavelength_grid_nm")
        )
        if (not isinstance(wavelength_nm, list)
                or len(wavelength_nm) != len(gain)):
            raise CalibrationError(
                "bundle wavelength grid length does not match gain length")

        run = bundle.get("run") or {}
        device_kind = str(run.get("device_kind") or "")
        sensor = sensor_id or str(run.get("sensor_id") or "")

        # Profiles document (firmware v1.6.0+). Absent -> bare, uncorrected.
        cap_profile: Optional[CapProfile] = None
        cap_id = CAP_ID_NONE
        dark_fraction: Optional[float] = None
        profiles_source = "none"
        if profiles:
            cap_profile, cap_id, dark_fraction = _parse_profiles(profiles)
            profiles_source = str(profiles.get("_source") or "explicit")
            if not device_kind:
                device_kind = str(profiles.get("device_kind") or "")

        return cls(
            gain=gain,
            wavelength_nm=wavelength_nm,
            dark_mean=dark_mean,
            dark_rate=dark_rate,
            dark_offset_per_ms=dark_off,
            dark_offset_fraction=dark_fraction,
            irradiance_geometry_corrected=bool(
                radiometric.get("irradiance_geometry_factor_applied", False)),
            cap_profile=cap_profile,
            cap_id=cap_id,
            bundle_sha=bundle_sha,
            completed_utc=str(bundle.get("completed_utc") or ""),
            device_kind=device_kind,
            sensor_id=sensor,
            profiles_source=profiles_source,
        )

    @classmethod
    def from_device(cls, host: str, *, port: int = 5001, timeout: float = 10.0,
                    token: str = "", require_profiles: bool = False,
                    control: Optional[DaqEControlClient] = None
                    ) -> "DeviceCalibration":
        """Pull the bundle (and profiles, if the firmware has them) off a
        DAQ-E and build the transform. Nothing leaves the LAN.

        ``require_profiles=True`` raises when the device carries no profiles
        document. Use it when you know a cap is fitted: without a profile the
        result is bare-uncorrected, which for a sunshine cap is wrong by
        roughly 30x -- better to fail loudly than log a plausible bad number.

        The returned object records ``bundle_sha`` as reported by the device;
        firmware v1.6.0+ verifies that hash against the stored bytes itself,
        so it is an integrity claim rather than a bare echo.
        """
        owned = control is None
        ctrl = control or DaqEControlClient(host, port=port, timeout=timeout,
                                            token=token)
        try:
            resp = _require_ok(ctrl.cmd({"cmd": "get_calibration"}),
                               "get_calibration")
            body = resp.get("bundle_json") or ""
            if not body:
                raise CalibrationError(
                    "device returned an empty bundle_json")
            try:
                bundle = json.loads(body)
            except json.JSONDecodeError as exc:
                raise CalibrationError(
                    f"on-device bundle is not valid JSON ({exc}) -- "
                    f"re-push it from Chloros") from exc
            bundle_sha = str(resp.get("sha256") or "")

            profiles = _fetch_profiles(ctrl)
            if profiles is None and require_profiles:
                raise CalibrationError(
                    "device carries no cap/geometry profiles "
                    "(require_profiles=True). Connect this DAQ-E to Chloros "
                    "once to push them, or pass require_profiles=False to "
                    "accept bare-uncorrected output.")

            return cls.from_bundle(bundle, profiles=profiles,
                                   bundle_sha=bundle_sha)
        finally:
            if owned:
                ctrl.close()


def _fetch_profiles(ctrl: DaqEControlClient) -> Optional[Dict[str, Any]]:
    """Read the profiles document, or None when this firmware/unit has none.

    ``get_profiles`` is new in firmware v1.6.0. Older firmware answers with
    an ``unknown command`` error, which is not a failure -- it just means
    bare-uncorrected output, same as every pre-v1.6.0 recording.
    """
    try:
        resp = ctrl.cmd({"cmd": "get_profiles"})
    except (OSError, ValueError):
        return None
    if not resp.get("ok"):
        # not_set (nothing pushed yet) and unknown_cmd (old firmware) are
        # both "no profiles", not errors.
        return None
    body = resp.get("profiles_json") or ""
    if not body:
        return None
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CalibrationError(
            f"on-device profiles document is not valid JSON ({exc})") from exc
    doc["_source"] = "device"
    return doc


def _parse_profiles(doc: Dict[str, Any]):
    """(CapProfile|None, cap_id, dark_offset_fraction|None) from a profiles doc."""
    version = doc.get("schema_version")
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = None
    if version is not None and version > PROFILES_SCHEMA_VERSION:
        raise CalibrationError(
            f"profiles document is schema_version {version}; this script "
            f"understands up to {PROFILES_SCHEMA_VERSION}. Update "
            f"chloros_scripts rather than guessing at the newer chain.")

    cap_id = str(doc.get("cap_id") or CAP_ID_NONE)

    cap_doc = doc.get("cap_profile")
    cap_profile: Optional[CapProfile] = None
    if isinstance(cap_doc, dict) and cap_doc:
        cap_profile = CapProfile.from_json(cap_doc, expect_cap_id=cap_id)

    dark_fraction = None
    df = doc.get("dark_fraction")
    if isinstance(df, dict):
        try:
            dark_fraction = float(df.get("offset_fraction"))
        except (TypeError, ValueError):
            dark_fraction = None
        if dark_fraction is not None and not math.isfinite(dark_fraction):
            dark_fraction = None

    return cap_profile, cap_id, dark_fraction


# ---------------------------------------------------------------------------
# CLI -- inspect a device's onboard calibration without recording anything
# ---------------------------------------------------------------------------
def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(
        description="Read a DAQ-E's onboard calibration and show what it "
                    "would do to a spectrum. No cloud, no Chloros.")
    p.add_argument("host", help="DAQ-E IP address or hostname")
    p.add_argument("--control-port", type=int, default=5001)
    p.add_argument("--token", default="",
                   help="control-channel auth token, if the unit has one set")
    p.add_argument("--save-bundle", metavar="PATH",
                   help="also write the raw bundle JSON here")
    p.add_argument("--require-profiles", action="store_true",
                   help="fail if the device has no cap/geometry profiles")
    args = p.parse_args(argv)

    with DaqEControlClient(args.host, port=args.control_port,
                           token=args.token) as ctrl:
        status = ctrl.cmd({"cmd": "status"})
        cal = DeviceCalibration.from_device(
            args.host, control=ctrl, require_profiles=args.require_profiles)
        if args.save_bundle:
            resp = ctrl.cmd({"cmd": "get_calibration"})
            with open(args.save_bundle, "w", encoding="utf-8") as fh:
                fh.write(resp.get("bundle_json", ""))
            print(f"bundle written to {args.save_bundle}")

    print(cal.describe())
    print()
    for t in (1, 50, 500):
        print(f"  dark at {t:>3} ms integration: "
              f"{cal.effective_dark(t):.4f} raw units")
    if status.get("fw"):
        print(f"\n  firmware {status['fw']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
