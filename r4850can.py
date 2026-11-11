#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
r4850can - CAN protocol layer for Huawei R48xx rectifiers (R4850G2 and similar)

Python standard library only (socket.AF_CAN), no python-can.

Structure:
  * Pure functions/classes without I/O  -> testable (build_*, R4850.handle_frame)
  * R4850   : state machine (read config, poll measurements, hold setpoints and
              refresh them periodically, fail-safe)
  * CanBus  : thin SocketCAN wrapper (non-blocking, usable with GLib/select)
  * main()  : small command line tool for bench tests

Safety principles:
  * Nothing is changed at start-up, only read requests are sent (config and state).
  * There is no function that writes the offline values (0x0101/0x0104), the input
    current limit or the fan. _send_setting() rejects everything except online
    voltage, online current and production on/off.
  * Voltage and current are clamped to configurable limits.
  * "Production on" is only sent when both current and voltage have been set.

Protocol sources: OpenDTU-OnBattery (GPL-2.0-or-later, HardwareInterface.cpp),
beyondlogic.org, Craig Peacock's Huawei_R4850G2_CAN, and own bus captures.
Check licenses and attribution before publishing.
"""

import argparse
import logging
import select
import socket
import struct
import sys
import time

log = logging.getLogger("r4850can")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CAN_EFF_FLAG = 0x80000000
CAN_EFF_MASK = 0x1FFFFFFF
_FRAME_FMT = "=IB3x8s"  # struct can_frame: id, dlc, 3 pad bytes, 8 data bytes
_FRAME_SIZE = struct.calcsize(_FRAME_FMT)  # 16

# Requests from the host: 0x10800000 | (device address 1 << 16) | register, source 0xFE
REQ_STATE = 0x108140FE
REQ_CONFIG = 0x108150FE
REQ_SETTING = 0x108180FE
ZERO8 = bytes(8)

# Replies from the PSU: source 0x7F, the last reply of a series uses 0x7E
RSP_STATE = 0x1081407F
RSP_CONFIG = 0x1081507F
RSP_ACK = 0x1081807E

# Command words (data bytes 0-1). Only the first three below are ever sent;
# 0x0100/0x0103/0x0132 are confirmed by own captures. 0x0101/0x0104 come from
# r4850.py and 0x0109 from a forum post and are only recognised in acknowledgements.
SET_ONLINE_VOLTAGE = 0x0100
SET_OFFLINE_VOLTAGE = 0x0101
SET_ONLINE_CURRENT = 0x0103
SET_OFFLINE_CURRENT = 0x0104
SET_INPUT_CURRENT_LIMIT = 0x0109
SET_PRODUCTION_DISABLE = 0x0132
SET_FAN_ONLINE_FULL_SPEED = 0x0134  # command numbers from OpenDTU-OnBattery HardwareInterface.h
SET_FAN_OFFLINE_FULL_SPEED = 0x0135
# Regular operation: only these are ever sent on their own.
SENDABLE_SETTINGS = frozenset(
    {SET_ONLINE_VOLTAGE, SET_ONLINE_CURRENT, SET_PRODUCTION_DISABLE}
)
# Written ONLY when explicitly requested through queue_setting() (driver: config.ini).
# The offline values are what the PSU uses when it receives no CAN frames.
PERSISTENT_SETTINGS = frozenset(
    {SET_OFFLINE_VOLTAGE, SET_OFFLINE_CURRENT, SET_FAN_ONLINE_FULL_SPEED, SET_FAN_OFFLINE_FULL_SPEED}
)
_PENDING_COMMANDS = {
    "offline_voltage": SET_OFFLINE_VOLTAGE,
    "offline_current": SET_OFFLINE_CURRENT,
    "fan_online_full_speed": SET_FAN_ONLINE_FULL_SPEED,
    "fan_offline_full_speed": SET_FAN_OFFLINE_FULL_SPEED,
}

# Measurement labels (byte 1 of the state frames)
LABELS = {
    0x70: "input_power",
    0x71: "input_frequency",
    0x72: "input_current",
    0x73: "output_power",
    0x74: "efficiency",  # percent
    0x75: "output_voltage",
    0x76: "output_current_max",  # scaled with the current multiplier
    0x78: "input_voltage",
    0x7F: "output_temperature",
    0x80: "input_temperature",
    0x81: "output_current",
}
_ACK_NAMES = {
    SET_ONLINE_VOLTAGE: "online_voltage",
    SET_OFFLINE_VOLTAGE: "offline_voltage",
    SET_ONLINE_CURRENT: "online_current",
    SET_OFFLINE_CURRENT: "offline_current",
    SET_INPUT_CURRENT_LIMIT: "input_current_limit",
}

# Absolute limits of the PSU (online 41.5..58.5 V, 0..60 A according to beyondlogic)
ABS_MIN_VOLTAGE = 41.5
ABS_MAX_VOLTAGE = 58.5
ABS_MAX_CURRENT = 60.0


# ---------------------------------------------------------------------------
# Interface discovery
# ---------------------------------------------------------------------------
def interface_driver(name, sysfs="/sys/class/net"):
    """Kernel driver name serving the given network interface, or None if unknown."""
    import os

    try:
        return os.path.basename(os.readlink(os.path.join(sysfs, name, "device", "driver")))
    except OSError:
        return None


def find_interface(driver="gs_usb", sysfs="/sys/class/net"):
    """Name of the first CAN interface served by the given kernel driver, or None.

    The numbering (can0/can1) is not stable across boots, the kernel driver is.
    """
    import os

    try:
        names = sorted(n for n in os.listdir(sysfs) if n.startswith("can"))
    except OSError:
        return None
    for n in names:
        if interface_driver(n, sysfs) == driver:
            return n
    return None


# ---------------------------------------------------------------------------
# Frame building (pure)
# ---------------------------------------------------------------------------
def _pack(command, flags, value):
    return (
        bytes([(command >> 8) & 0xFF, command & 0xFF, (flags >> 8) & 0xFF, flags & 0xFF])
        + int(value).to_bytes(4, "big")
    )


def build_online_voltage(volts):
    """Online voltage setpoint, value = V * 1024."""
    return _pack(SET_ONLINE_VOLTAGE, 0, int(volts * 1024))


def build_online_current(amps, multiplier):
    """Online current limit, value = A * multiplier (truncated like OpenDTU does)."""
    return _pack(SET_ONLINE_CURRENT, 0, int(amps * multiplier))


def build_offline_voltage(volts):
    """Offline voltage (used when the PSU receives no CAN frames), value = V * 1024."""
    return _pack(SET_OFFLINE_VOLTAGE, 0, int(volts * 1024))


def build_offline_current(amps, multiplier):
    """Offline current limit, value = A * multiplier (truncated)."""
    return _pack(SET_OFFLINE_CURRENT, 0, int(amps * multiplier))


def build_fan(online, full_speed):
    """Fan full speed on (flag 1) or automatic (flag 0), for the online or the offline case."""
    return _pack(SET_FAN_ONLINE_FULL_SPEED if online else SET_FAN_OFFLINE_FULL_SPEED, 1 if full_speed else 0, 0)


def build_production(enabled):
    """Enable the output (flag 0) or disable it (flag 1)."""
    return _pack(SET_PRODUCTION_DISABLE, 0 if enabled else 1, 0)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
class R4850:
    POLL_INTERVAL = 2.0  # config and state requests
    REFRESH_INTERVAL = 10.0  # re-send setpoints (online values are volatile)
    CONFIG_TIMEOUT = 10.0  # no config reply for this long -> PSU counts as unreachable
    SETTING_RETRY_INTERVAL = 3.0  # seconds between attempts to write a queued setting
    SETTING_MAX_TRIES = 5  # attempts per setting and connection before giving up

    def __init__(self, send, voltage_limits=(48.0, 54.0), max_current=10.0, clock=time.monotonic):
        """send(can_id, data) -> bool; clock() -> seconds (monotonic)."""
        vmin, vmax = voltage_limits
        if not (ABS_MIN_VOLTAGE <= vmin <= vmax <= ABS_MAX_VOLTAGE):
            raise ValueError("invalid voltage limits")
        if not (0 < max_current <= ABS_MAX_CURRENT):
            raise ValueError("invalid current limit")
        self._send_fn = send
        self._clock = clock
        self.vmin, self.vmax, self.max_current = float(vmin), float(vmax), float(max_current)

        self.multiplier = None  # 2048 / maximum output current
        self.reachable = False
        self.row = None
        self.slot = None
        self.values = {}  # name -> (value, timestamp)
        self.extra = {}  # unknown labels: label -> (raw value, timestamp)
        self.acks = {}  # name -> (value, timestamp)
        self.ack_errors = 0

        self._want_v = None
        self._want_i = None
        self._want_prod = None
        self._dirty = False
        self._last_config = None
        self._last_poll = None
        self._last_sent = None

        # Settings queued with queue_setting() that still wait for the PSU's acknowledgement
        self._pending = {}
        self._tries = {}
        self._last_try = {}
        self._gave_up = set()
        self._wanted = {}  # all configured settings; rewritten whenever the PSU becomes reachable

    # -- Receiving ---------------------------------------------------------
    def handle_frame(self, can_id, data):
        """Process a received frame. Returns True if it was recognised."""
        data = bytes(data)
        if len(data) != 8:
            return False
        now = self._clock()
        value_id = int.from_bytes(data[0:4], "big")
        value = int.from_bytes(data[4:8], "big")
        if (can_id | 1) == RSP_CONFIG:
            return self._on_config(value_id, value, now)
        if (can_id | 1) == RSP_STATE:
            return self._on_state(can_id, value_id, value, now)
        if can_id == RSP_ACK:
            return self._on_ack(data, value_id, value, now)
        return False

    def _on_config(self, value_id, value, now):
        counter = (value_id >> 16) & 0xFFFF
        if counter == 1:
            max_amps = (value >> 16) & 0xFF  # twice the maximum current, see OpenDTU
            if max_amps:
                self.multiplier = 2048.0 / max_amps
        elif counter == 6:
            self.row = (value_id >> 8) & 0xFF
            self.slot = value_id & 0xFF
            self._last_config = now
            if not self.reachable:
                log.info("PSU reachable")
                self.reachable = True
                self._dirty = True  # re-send setpoints after (re)connect
                # (re)write all configured settings on every (re)connect, 5 attempts each
                self._pending = dict(self._wanted)
                self._tries = {k: 0 for k in self._pending}
                self._last_try.clear()
                self._gave_up.clear()
                self._gave_up.clear()
        return True

    def _on_state(self, can_id, value_id, value, now):
        # Quirks of individual devices, handled the same way as in OpenDTU
        if can_id == 0x1081407E and (value_id & 0x01):
            value_id &= ~0x01
        if can_id == 0x1081407F and (value_id & 0x03):
            value_id &= ~0x03
        if (value_id >> 24) == 0x31:
            value_id &= 0x0FFFFFFF
        if (value_id & 0xFF00FFFF) != 0x01000000:
            return False
        label = (value_id >> 16) & 0xFF
        # Assumption: values are signed (temperatures below 0 degrees)
        raw = value - (1 << 32) if value & 0x80000000 else value
        name = LABELS.get(label)
        if name is None:
            self.extra[label] = (raw, now)
            return True
        if label == 0x76:
            if not self.multiplier:
                return False
            v = raw / self.multiplier
        else:
            v = raw / 1024.0
        if name == "efficiency":
            v *= 100.0
        self.values[name] = (v, now)
        return True

    def _on_ack(self, data, value_id, value, now):
        if data[0] & 0x20:  # error bit according to r4850.py (not used otherwise)
            self.ack_errors += 1
            log.warning("Error acknowledgement from PSU: %s", data.hex())
            return True
        setting = value_id >> 16
        flags = value_id & 0xFFFF
        if setting == SET_PRODUCTION_DISABLE:
            self.acks["production_enabled"] = ((flags & 1) == 0, now)
            return True
        if setting in (SET_FAN_ONLINE_FULL_SPEED, SET_FAN_OFFLINE_FULL_SPEED):
            name = "fan_online_full_speed" if setting == SET_FAN_ONLINE_FULL_SPEED else "fan_offline_full_speed"
            self.acks[name] = ((flags & 1) > 0, now)
            self._check_pending(name, (flags & 1) > 0)
            return True
        name = _ACK_NAMES.get(setting)
        if name is None:
            return False
        divisor = 1024.0
        if setting in (SET_ONLINE_CURRENT, SET_OFFLINE_CURRENT):
            if not self.multiplier:
                return False
            divisor = self.multiplier
        self.acks[name] = (value / divisor, now)
        self._check_pending(name, value / divisor)
        return True

    def _check_pending(self, name, acked):
        """A queued setting counts as applied when the PSU acknowledges the wanted value."""
        if name not in self._pending:
            return
        wanted = self._pending[name]
        if isinstance(wanted, bool):
            ok = acked == wanted
        elif name == "offline_voltage":
            ok = abs(acked - wanted) <= 0.01
        else:
            ok = abs(acked - wanted) <= 0.1  # current resolution is 1/multiplier (~0.05 A)
        if not ok:
            log.warning("PSU acknowledged %s = %s, wanted %s", name, acked, wanted)
            return
        del self._pending[name]
        log.info("PSU setting %s = %s acknowledged", name, wanted)

    # -- Access ------------------------------------------------------------
    def get(self, name, max_age=None):
        item = self.values.get(name)
        if item is None:
            return None
        if max_age is not None and self._clock() - item[1] > max_age:
            return None
        return item[0]

    def data_age(self):
        """Seconds since the last measurement, None if none has arrived yet."""
        if not self.values:
            return None
        return self._clock() - max(ts for _, ts in self.values.values())

    # -- Setpoints ---------------------------------------------------------
    @staticmethod
    def _check_number(x):
        x = float(x)
        if x != x or x in (float("inf"), float("-inf")):
            raise ValueError("invalid number")
        return x

    def set_online_voltage(self, volts):
        v = self._check_number(volts)
        eff = min(max(v, self.vmin), self.vmax)
        if eff != v:
            log.warning("Voltage %.2f V limited to %.2f V", v, eff)
        self._want_v = eff
        self._dirty = True
        return eff

    def set_online_current(self, amps):
        a = self._check_number(amps)
        eff = min(max(a, 0.0), self.max_current)
        if eff != a:
            log.warning("Current %.2f A limited to %.2f A", a, eff)
        self._want_i = eff
        self._dirty = True
        return eff

    def set_production(self, enabled):
        self._want_prod = bool(enabled)
        self._dirty = True

    def queue_setting(self, name, value):
        """Configure a PSU setting (offline voltage/current, fans). It is written every time the PSU
        becomes reachable, with up to SETTING_MAX_TRIES attempts. Values are validated here."""
        if name not in _PENDING_COMMANDS:
            raise ValueError("unknown PSU setting %r" % name)
        if name.startswith("fan_"):
            value = bool(value)
        else:
            value = self._check_number(value)
            if name == "offline_voltage" and not (ABS_MIN_VOLTAGE <= value <= self.vmax):
                raise ValueError("offline_voltage %.2f V outside %.1f..%.1f V" % (value, ABS_MIN_VOLTAGE, self.vmax))
            if name == "offline_current" and not (0.0 <= value <= self.max_current):
                raise ValueError("offline_current %.2f A outside 0..%.1f A" % (value, self.max_current))
        self._wanted[name] = value
        self._pending[name] = value
        self._tries[name] = 0
        self._last_try.pop(name, None)
        self._gave_up.discard(name)

    def safe_off(self):
        """Make the output passive right now: production off, online current 0. Needs no multiplier."""
        self._want_prod = False
        self._want_i = 0.0
        self._send_setting(build_production(False))
        self._send_setting(build_online_current(0.0, 1.0))
        self._last_sent = self._clock()
        self._dirty = False

    # -- Cycle -------------------------------------------------------------
    def tick(self):
        """Call regularly (about once per second)."""
        now = self._clock()
        if self._last_config is not None and now - self._last_config > self.CONFIG_TIMEOUT:
            log.warning("PSU unreachable (no config reply)")
            self.reachable = False
            self.multiplier = None
            self._last_config = None
            self.acks.clear()
        if self._last_poll is None or now - self._last_poll >= self.POLL_INTERVAL:
            self._send(REQ_CONFIG, ZERO8)
            self._send(REQ_STATE, ZERO8)
            self._last_poll = now
        if self.reachable and (
            self._dirty or self._last_sent is None or now - self._last_sent >= self.REFRESH_INTERVAL
        ):
            self._send_setpoints(now)
        if self.reachable and self._pending:
            self._send_pending(now)

    def _send_pending(self, now):
        for name, value in list(self._pending.items()):
            if self._tries.get(name, 0) >= self.SETTING_MAX_TRIES:
                if name not in self._gave_up:
                    self._gave_up.add(name)
                    log.error("PSU did not acknowledge %s = %s after %d attempts", name, value, self.SETTING_MAX_TRIES)
                continue
            last = self._last_try.get(name)
            if last is not None and now - last < self.SETTING_RETRY_INTERVAL:
                continue
            if name == "offline_voltage":
                data = build_offline_voltage(value)
            elif name == "offline_current":
                if not self.multiplier:
                    continue  # needs the current multiplier first
                data = build_offline_current(value, self.multiplier)
            else:
                data = build_fan(name == "fan_online_full_speed", value)
            self._tries[name] = self._tries.get(name, 0) + 1
            self._last_try[name] = now
            log.warning("Writing PSU setting %s = %s (attempt %d/%d)", name, value, self._tries[name], self.SETTING_MAX_TRIES)
            self._send_persistent(data)

    def _send_persistent(self, data):
        command = (data[0] << 8) | data[1]
        if command not in PERSISTENT_SETTINGS:
            raise ValueError("command 0x%04X is not a persistent setting" % command)
        return self._send(REQ_SETTING, data)

    def _current_value_ok(self):
        return self._want_i is None or self._want_i == 0 or bool(self.multiplier)

    def _send_setpoints(self, now):
        if self._want_prod is None and self._want_i is None and self._want_v is None:
            return
        # Switch off first, switch on last
        if self._want_prod is False:
            self._send_setting(build_production(False))
        if self._want_i is not None:
            if not self._current_value_ok():
                return  # without a multiplier send no current (and therefore nothing else)
            mult = self.multiplier or 1.0
            self._send_setting(build_online_current(self._want_i, mult))
        if self._want_v is not None:
            self._send_setting(build_online_voltage(self._want_v))
        if self._want_prod is True:
            if self._want_i is None or self._want_v is None:
                log.warning("Not sending production on: current/voltage not set")
            else:
                self._send_setting(build_production(True))
        self._last_sent = now
        self._dirty = False

    def _send_setting(self, data):
        command = (data[0] << 8) | data[1]
        if command not in SENDABLE_SETTINGS:
            raise ValueError("command 0x%04X is blocked" % command)
        return self._send(REQ_SETTING, data)

    def _send(self, can_id, data):
        ok = self._send_fn(can_id, bytes(data))
        if not ok:
            log.warning("Sending 0x%08X failed", can_id)
        return ok

    def status(self):
        d = {k: v[0] for k, v in self.values.items()}
        d["reachable"] = self.reachable
        d["multiplier"] = self.multiplier
        d["acks"] = {k: v[0] for k, v in self.acks.items()}
        return d


# ---------------------------------------------------------------------------
# SocketCAN
# ---------------------------------------------------------------------------
class CanBus:
    def __init__(self, ifname):
        try:
            self.sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        except AttributeError as e:  # Python without CAN support
            raise RuntimeError("this Python has no socket.AF_CAN support") from e
        # Deliberately no kernel filter: frames are filtered in recv_all()
        # (more robust, easier to debug).
        self.sock.bind((ifname,))
        self.sock.setblocking(False)

    def fileno(self):
        return self.sock.fileno()

    def send(self, can_id, data):
        data = bytes(data)
        frame = struct.pack(_FRAME_FMT, (can_id & CAN_EFF_MASK) | CAN_EFF_FLAG, len(data), data.ljust(8, b"\0"))
        try:
            self.sock.send(frame)
            return True
        except OSError as e:
            log.debug("send: %s", e)
            return False

    def recv_all(self):
        """All pending frames as a list of (can_id, data)."""
        out = []
        while True:
            try:
                raw = self.sock.recv(_FRAME_SIZE)
            except BlockingIOError:
                return out
            if len(raw) != _FRAME_SIZE:
                continue
            can_id, dlc, data = struct.unpack(_FRAME_FMT, raw)
            log.debug("RX raw id=0x%08X dlc=%d data=%s", can_id, dlc, data[:dlc].hex())
            if not (can_id & CAN_EFF_FLAG) or dlc != 8:
                continue
            cid = can_id & CAN_EFF_MASK
            if (cid & 0xFFFF0000) != 0x10810000:
                continue
            out.append((cid, data[:dlc]))

    def close(self):
        self.sock.close()


# ---------------------------------------------------------------------------
# Command line tool
# ---------------------------------------------------------------------------
def _fmt(v, spec, unit=""):
    return "   -  " if v is None else (format(v, spec) + unit)


def status_line(psu):
    g = psu.get
    return (
        "{reach} U={u} I={i} (limit {imax}) P={p} efficiency={eff} "
        "Tin/out={tin}/{tout} AC={uin} mult={m}  acks={acks}"
    ).format(
        reach="REACHABLE  " if psu.reachable else "unreachable",
        u=_fmt(g("output_voltage"), ".2f", "V"),
        i=_fmt(g("output_current"), ".2f", "A"),
        imax=_fmt(g("output_current_max"), ".2f", "A"),
        p=_fmt(g("output_power"), ".1f", "W"),
        eff=_fmt(g("efficiency"), ".0f", "%"),
        tin=_fmt(g("input_temperature"), ".0f"),
        tout=_fmt(g("output_temperature"), ".0f"),
        uin=_fmt(g("input_voltage"), ".0f", "V"),
        m=_fmt(psu.multiplier, ".2f"),
        acks={k: (round(v[0], 2) if not isinstance(v[0], bool) else v[0]) for k, v in psu.acks.items()},
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Huawei R48xx over SocketCAN. Without options it only reads (requests, no changes)."
    )
    ap.add_argument("interface", help="e.g. can0")
    ap.add_argument("--voltage", type=float, help="online voltage setpoint in V (clamped to --vmin/--vmax)")
    ap.add_argument("--current", type=float, help="online current limit in A (clamped to 0..--imax)")
    ap.add_argument("--production", choices=("on", "off"), help="enable or disable the output")
    ap.add_argument("--vmin", type=float, default=48.0, help="lower voltage limit in V (default 48)")
    ap.add_argument("--vmax", type=float, default=54.0, help="upper voltage limit in V (default 54)")
    ap.add_argument("--imax", type=float, default=10.0, help="current limit in A (default 10)")
    ap.add_argument("--seconds", type=float, default=60.0, help="run time in write mode (default 60)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    manage = args.voltage is not None or args.current is not None or args.production is not None
    if args.production == "on" and (args.voltage is None or args.current is None):
        ap.error("--production on requires --voltage and --current")

    bus = CanBus(args.interface)
    try:
        psu = R4850(bus.send, voltage_limits=(args.vmin, args.vmax), max_current=args.imax)
    except ValueError as e:
        bus.close()
        ap.error(str(e))
    if args.current is not None:
        psu.set_online_current(args.current)
    if args.voltage is not None:
        psu.set_online_voltage(args.voltage)
    if args.production is not None:
        psu.set_production(args.production == "on")

    end = time.monotonic() + args.seconds if manage else None
    last_print = 0.0
    try:
        while end is None or time.monotonic() < end:
            psu.tick()
            readable, _, _ = select.select([bus], [], [], 0.5)
            if readable:
                for can_id, data in bus.recv_all():
                    handled = psu.handle_frame(can_id, data)
                    log.debug("frame 0x%08X %s -> %s", can_id, data.hex(), "ok" if handled else "ignored")
            if time.monotonic() - last_print >= 2.0:
                print(status_line(psu), flush=True)
                last_print = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        if manage:
            print("Exiting: production off, online current 0", flush=True)
            psu.safe_off()
        bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
