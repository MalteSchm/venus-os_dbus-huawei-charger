# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure decision logic for the Huawei charger driver (no dbus/CAN, fully testable)."""

from collections import namedtuple

MIN_CURRENT_OFF = 0.5  # below this the output is switched off
MIN_CURRENT_ON = 1.0  # needed to (re)start; hysteresis, the PSU soft start takes ~8 s

Decision = namedtuple("Decision", "enable voltage current reason")


def to_float(x):
    """Convert to a finite float, or None. Accepts numbers, bools and numeric text."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if x != x or x in (float("inf"), float("-inf")) else x


_num = to_float


def compute_limits(mode_on, want_v, want_i, vmin, vmax, imax, bat, psu_current,
                   was_enabled=False, watchdog_ok=True):
    """
    mode_on      : /Mode == 1
    want_v/want_i: manual setpoints (/Link/ChargeVoltage, /Link/ChargeCurrent)
    bat          : None or dict with ccl, cvl, allow, current (values of the battery service)
    psu_current  : measured output current of the Huawei PSU (A) or None
    Fail-safe: missing or invalid data -> switch off.
    """
    off = lambda why: Decision(False, None, 0.0, why)
    if not mode_on:
        return off("Mode off")
    if not watchdog_ok:
        return off("Heartbeat missing")
    if bat is None:
        return off("no battery service")
    ccl, cvl, cur = _num(bat.get("ccl")), _num(bat.get("cvl")), _num(bat.get("current"))
    if ccl is None or cvl is None:
        return off("BMS limits missing")
    if cur is None:
        return off("no battery current")
    if ccl <= 0:
        return off("BMS charge current limit is 0")
    raw_allow = bat.get("allow")
    if raw_allow is not None:
        allow = _num(raw_allow)  # also "0"/"1" as text or bool; unreadable counts as "no"
        if allow is None or allow <= 0:
            return off("BMS does not allow charging")
    wv, wi = _num(want_v), _num(want_i)
    if wv is None or wi is None:
        return off("setpoints missing")
    voltage = min(wv, cvl, vmax)
    if voltage < vmin:
        return off("voltage limit below %.1f V" % vmin)
    other = cur - (_num(psu_current) or 0.0)  # charge current delivered by other sources (Multiplus)
    allowed = ccl - other
    current = max(0.0, min(wi, allowed, imax))
    threshold = MIN_CURRENT_OFF if was_enabled else MIN_CURRENT_ON
    if current < threshold:
        return off("no current headroom (%.1f A)" % current)
    if current >= min(wi, imax) - 1e-9:
        why = "setpoint"
    else:
        why = "BMS limit"
    return Decision(True, voltage, current, why)


def format_status_line(v, status):
    """One-line summary of the PSU measurements. v maps names to floats or None."""

    def f(key, spec, unit=""):
        x = v.get(key)
        return "-" if x is None else format(x, spec) + unit

    return (
        "out {ov} {oc} (limit {ol}) {op} | in {iv} {ic} {ip} {fr} | eff {eff} | T in/out {ti}/{to} | {status}"
    ).format(
        ov=f("output_voltage", ".2f", " V"), oc=f("output_current", ".2f", " A"),
        ol=f("output_current_max", ".2f", " A"), op=f("output_power", ".0f", " W"),
        iv=f("input_voltage", ".0f", " V"), ic=f("input_current", ".2f", " A"),
        ip=f("input_power", ".0f", " W"), fr=f("input_frequency", ".1f", " Hz"),
        eff=f("efficiency", ".0f", " %"), ti=f("input_temperature", ".0f", " C"),
        to=f("output_temperature", ".0f", " C"), status=status,
    )


PSU_SETTING_KEYS = ("offline_voltage", "offline_current", "fan_online_full_speed", "fan_offline_full_speed")


def _parse_bool(raw):
    low = raw.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise ValueError(raw)


CONFIG_DEFAULTS = {
    "interface": "can1",  # explicit name; "auto" = the interface served by the configured driver
    "driver": "gs_usb",  # kernel driver the interface must use (safety check); empty = no check
    "vmin": 48.0,
    "vmax": 55.2,
    "imax": 30.0,
    "deviceinstance": 290,
    "heartbeat_timeout": 0.0,  # seconds, 0 = watchdog off
    "status_log_interval": 60.0,  # seconds between status lines in the log, 0 = off
    # Optional PSU settings: None = not configured = never written
    "offline_voltage": None,
    "offline_current": None,
    "fan_online_full_speed": None,
    "fan_offline_full_speed": None,
}


def load_config(path):
    """Read [huawei] from config.ini; missing file/keys -> defaults. Invalid values -> ValueError."""
    import configparser

    cfg = dict(CONFIG_DEFAULTS)
    cp = configparser.ConfigParser()
    if path and cp.read(path):
        if cp.has_section("huawei"):
            for key, default in CONFIG_DEFAULTS.items():
                if cp.has_option("huawei", key):
                    raw = cp.get("huawei", key).strip()
                    try:
                        if default is None:  # optional PSU setting; empty = not configured
                            cfg[key] = None if raw == "" else (_parse_bool(raw) if key.startswith("fan_") else float(raw))
                        else:
                            cfg[key] = int(float(raw)) if isinstance(default, int) else type(default)(raw)
                    except ValueError:
                        raise ValueError("config.ini: invalid value for %s: %r" % (key, raw))
    return cfg
