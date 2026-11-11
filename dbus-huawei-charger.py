#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
dbus-huawei-charger: Huawei R4850 as a com.victronenergy.charger service on Venus OS.

Version 0.1 - tested on a Raspberry Pi Zero 2 W (Venus OS v3.85) and on a
Multiplus-II GX with a real BMS. The decision logic (charger_logic.py) and the CAN
layer (r4850can.py) have unit tests; this dbus glue layer does not.

Control (via dbus paths, e.g. from Node-RED):
  /Mode                1 = on, 4 = off (always starts as 4 = off, not persisted)
  /Link/ChargeCurrent  requested charge current in A (starts at 0)
  /Link/ChargeVoltage  requested upper voltage limit in V (starts at 0 = nothing)
The BMS always has priority: current and voltage are limited by /Info/MaxChargeCurrent
and /Info/MaxChargeVoltage of the battery service; without a valid battery service the
output is switched off. If the driver dies, the PSU falls back to its offline values
(below the battery voltage) after ~60 s.
"""

import argparse
import logging
import os
import signal
import sys
import time

sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))
for p in (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext", "velib_python"),
    "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python",
):
    if os.path.isdir(p):
        sys.path.insert(1, p)

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from vedbus import VeDbusItemImport, VeDbusService

import r4850can
from charger_logic import PSU_SETTING_KEYS, compute_limits, format_status_line, load_config, to_float

VERSION = "0.1"
log = logging.getLogger("dbus-huawei-charger")
MAX_DATA_AGE = 10.0  # seconds; older measurements are published as invalid


class BatteryReader:
    """Reads the limits of the first com.victronenergy.battery.* service."""

    PATHS = {
        "ccl": "/Info/MaxChargeCurrent",
        "cvl": "/Info/MaxChargeVoltage",
        "allow": "/Io/AllowToCharge",
        "current": "/Dc/0/Current",
    }

    def __init__(self, bus):
        self.bus = bus
        self.service = None
        self.items = {}

    def _find(self):
        names = sorted(n for n in self.bus.list_names() if n.startswith("com.victronenergy.battery."))
        return names[0] if names else None

    def read(self):
        try:
            name = self._find()
            if name != self.service:
                self.service = name
                self.items = {}
                if name:
                    log.info("Battery service: %s", name)
            if not name:
                return None
            if not self.items:
                self.items = {
                    # createsignal=True is required: otherwise the cached value stays at its initial state
                    k: VeDbusItemImport(self.bus, name, path, createsignal=True)
                    for k, path in self.PATHS.items()
                }
            out = {}
            for k, item in self.items.items():
                v = item.get_value()
                out[k] = None if v is None or (hasattr(v, "__len__") and len(v) == 0) else v
            return out
        except Exception:  # service vanished or dbus error -> same as "no battery service"
            log.exception("Reading battery service failed")
            self.items = {}
            return None


class Driver:
    def __init__(self, args):
        self.args = args
        self.bus_can = r4850can.CanBus(args.interface)
        self.psu = r4850can.R4850(
            self.bus_can.send, voltage_limits=(args.vmin, args.vmax), max_current=args.imax
        )
        self.dbus = dbus.SystemBus()
        self.battery = BatteryReader(self.dbus)
        self.enabled = False

        # Optional PSU settings from config.ini (offline values, fans): written whenever the PSU
        # becomes reachable. Unconfigured keys are never written.
        for name, value in args.psu_settings.items():
            self.psu.queue_setting(name, value)  # raises ValueError for out-of-range values
        if args.psu_settings:
            log.warning("PSU settings from config.ini: %s", args.psu_settings)
        self._last_bat = None
        self._last_heartbeat = time.monotonic()
        self._last_status_log = 0.0

        name = "com.victronenergy.charger.huawei_r4850_" + args.interface
        s = self.svc = VeDbusService(name, self.dbus, register=False)
        s.add_mandatory_paths(
            processname=__file__, processversion=VERSION, connection="CAN " + args.interface,
            deviceinstance=args.deviceinstance, productid=0xFFFF, productname="Huawei R4850",
            firmwareversion=VERSION, hardwareversion=None, connected=1,
        )
        s.add_path("/Dc/0/Voltage", None, gettextcallback=lambda p, v: "%.2f V" % v)
        s.add_path("/Dc/0/Current", None, gettextcallback=lambda p, v: "%.2f A" % v)
        # Not published: the GUI labels /Dc/0/Temperature "Battery temperature"; the PSU
        # temperatures are available under /Huawei/ instead.
        s.add_path("/Ac/In/L1/I", None, gettextcallback=lambda p, v: "%.2f A" % v)  # shown by the GUI
        s.add_path("/Ac/In/L1/P", None, gettextcallback=lambda p, v: "%.0f W" % v)
        s.add_path("/State", 0)
        s.add_path("/ErrorCode", 0)
        s.add_path("/Mode", 4, writeable=True, onchangecallback=self._on_mode)
        s.add_path("/Link/ChargeCurrent", 0.0, writeable=True, onchangecallback=self._on_number)
        s.add_path("/Link/ChargeVoltage", 0.0, writeable=True, onchangecallback=self._on_number)
        s.add_path("/Huawei/Heartbeat", 0, writeable=True, onchangecallback=self._on_heartbeat)
        s.add_path("/Huawei/Reachable", 0)
        s.add_path("/Huawei/Status", "start")
        s.add_path("/Huawei/OutputPower", None)
        s.add_path("/Huawei/InputPower", None)
        s.add_path("/Huawei/Efficiency", None)
        s.add_path("/Huawei/AcVoltage", None)
        s.add_path("/Huawei/AcCurrent", None)
        s.add_path("/Huawei/AcFrequency", None)
        s.add_path("/Huawei/InputTemperature", None)
        s.add_path("/Huawei/OutputTemperature", None)
        s.add_path("/Huawei/CurrentLimit", None)  # output current limit reported by the PSU
        s.register()

        GLib.io_add_watch(self.bus_can.fileno(), GLib.IO_IN, self._on_can)
        GLib.timeout_add(1000, self._tick)

    # -- Validate dbus writes ----------------------------------------------
    def _on_heartbeat(self, path, value):
        self._last_heartbeat = time.monotonic()
        return True

    def _watchdog_ok(self):
        t = self.args.heartbeat_timeout
        return t <= 0 or (time.monotonic() - self._last_heartbeat) <= t

    @staticmethod
    def _on_mode(path, value):
        log.debug("Write to %s: %r (%s)", path, value, type(value).__name__)
        return to_float(value) in (1.0, 4.0)

    @staticmethod
    def _on_number(path, value):
        log.debug("Write to %s: %r (%s)", path, value, type(value).__name__)
        v = to_float(value)
        return v is not None and 0.0 <= v <= 100.0

    # -- CAN ---------------------------------------------------------------
    def _on_can(self, fd, cond):
        for can_id, data in self.bus_can.recv_all():
            self.psu.handle_frame(can_id, data)
        return True

    # -- Main cycle (1 Hz) -------------------------------------------------
    def _tick(self):
        try:
            self._cycle()
        except Exception:
            log.exception("Error in cycle - switching off to be safe")
            try:
                self.psu.safe_off()
            except Exception:
                pass
        return True

    def _cycle(self):
        s, psu = self.svc, self.psu
        psu.tick()

        # If someone writes text (e.g. the dbus command line tool), normalise it to a
        # number so that other services (GUI, VRM) do not trip over a str.
        for path, conv in (("/Mode", int), ("/Link/ChargeCurrent", float), ("/Link/ChargeVoltage", float)):
            raw = s[path]
            num = to_float(raw)
            if num is not None and not isinstance(raw, (int, float)):
                s[path] = conv(num)

        bat = self.battery.read()
        if bat != self._last_bat:
            log.debug("BMS values: %r", bat)
            self._last_bat = bat
        d = compute_limits(
            mode_on=to_float(s["/Mode"]) == 1.0,
            want_v=s["/Link/ChargeVoltage"], want_i=s["/Link/ChargeCurrent"],
            vmin=self.args.vmin, vmax=self.args.vmax, imax=self.args.imax,
            bat=bat, psu_current=psu.get("output_current", MAX_DATA_AGE),
            was_enabled=self.enabled, watchdog_ok=self._watchdog_ok(),
        )
        self.enabled = d.enable
        if d.enable:
            psu.set_online_current(d.current)
            psu.set_online_voltage(d.voltage)
            psu.set_production(True)
        else:
            psu.set_online_current(0.0)
            psu.set_production(False)

        g = lambda n: psu.get(n, MAX_DATA_AGE)
        s["/Dc/0/Voltage"] = g("output_voltage")
        s["/Dc/0/Current"] = g("output_current")
        s["/Ac/In/L1/I"] = g("input_current")
        s["/Ac/In/L1/P"] = g("input_power")
        s["/Huawei/OutputPower"] = g("output_power")
        s["/Huawei/InputPower"] = g("input_power")
        s["/Huawei/Efficiency"] = g("efficiency")
        s["/Huawei/AcVoltage"] = g("input_voltage")
        s["/Huawei/AcCurrent"] = g("input_current")
        s["/Huawei/AcFrequency"] = g("input_frequency")
        s["/Huawei/InputTemperature"] = g("input_temperature")
        s["/Huawei/OutputTemperature"] = g("output_temperature")
        s["/Huawei/CurrentLimit"] = g("output_current_max")
        s["/Huawei/Reachable"] = 1 if psu.reachable else 0
        i = g("output_current")
        s["/State"] = 252 if (d.enable and i is not None and i > 0.1) else 0  # 252 = external control
        s["/Huawei/Status"] = ("%s: %.1f A / %.1f V" % (d.reason, d.current, d.voltage)) if d.enable else d.reason

        interval = self.args.status_log_interval
        now = time.monotonic()
        if interval > 0 and now - self._last_status_log >= interval:
            self._last_status_log = now
            names = ("output_voltage", "output_current", "output_current_max", "output_power", "input_voltage",
                     "input_current", "input_power", "input_frequency", "efficiency",
                     "input_temperature", "output_temperature")
            status = s["/Huawei/Status"] if psu.reachable else "PSU unreachable"
            log.info("%s", format_status_line({n: g(n) for n in names}, status))

    def shutdown(self):
        try:
            self.psu.safe_off()
        finally:
            self.bus_can.close()


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=os.path.join(here, "config.ini"))
    known, _ = pre.parse_known_args()
    try:
        cfg = load_config(known.config)
    except ValueError as e:
        sys.exit(str(e))

    ap = argparse.ArgumentParser(description="Huawei R4850 as a Victron charger service", parents=[pre])
    ap.add_argument("--interface", default=cfg["interface"],
                    help='CAN interface name, or "auto" to pick the interface served by --driver')
    ap.add_argument("--driver", default=cfg["driver"],
                    help="kernel driver the interface must use, empty = no check (default gs_usb)")
    ap.add_argument("--vmin", type=float, default=cfg["vmin"])
    ap.add_argument("--vmax", type=float, default=cfg["vmax"], help="hard upper voltage limit in V")
    ap.add_argument("--imax", type=float, default=cfg["imax"], help="hard upper current limit in A")
    ap.add_argument("--deviceinstance", type=int, default=cfg["deviceinstance"])
    ap.add_argument("--heartbeat-timeout", dest="heartbeat_timeout", type=float,
                    default=cfg["heartbeat_timeout"],
                    help="seconds without a change of /Huawei/Heartbeat before switching off (0 = off)")
    ap.add_argument("--status-log-interval", dest="status_log_interval", type=float,
                    default=cfg["status_log_interval"],
                    help="seconds between status lines in the log (0 = off)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.psu_settings = {k: cfg[k] for k in PSU_SETTING_KEYS if cfg[k] is not None}

    if args.interface == "auto":
        args.interface = r4850can.find_interface(args.driver or "gs_usb")
        if args.interface is None:
            sys.exit("no CAN interface with driver %s found (adapter not plugged in?)" % (args.driver or "gs_usb"))
        log.info("Using CAN interface %s", args.interface)
    elif args.driver:
        actual = r4850can.interface_driver(args.interface)
        if actual != args.driver:
            sys.exit("CAN interface %s is served by driver %r, expected %r - refusing to start "
                     "(wrong bus? check the numbering of can0/can1)" % (args.interface, actual, args.driver))

    DBusGMainLoop(set_as_default=True)
    try:
        drv = Driver(args)
    except ValueError as e:
        sys.exit("config.ini: %s" % e)
    loop = GLib.MainLoop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, loop.quit)
    try:
        loop.run()
    finally:
        drv.shutdown()


if __name__ == "__main__":
    main()
