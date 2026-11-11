#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Write a NUMERIC value to a dbus path (the 'dbus' command line tool writes text).

Usage:   python3 setv.py <service> <path> <number>
Example: python3 setv.py com.victronenergy.battery.socketcan_can0 /Info/MaxChargeCurrent 20
Whole numbers are sent as Int32, everything else as Double.
"""
import sys

import dbus

if len(sys.argv) != 4:
    sys.exit(__doc__)
service, path, text = sys.argv[1:]
try:
    f = float(text)
except ValueError:
    sys.exit("not a number: %r" % text)
value = dbus.Int32(int(f)) if f == int(f) else dbus.Double(f)
obj = dbus.SystemBus().get_object(service, path)
rc = obj.SetValue(value, dbus_interface="com.victronenergy.BusItem")
print("SetValue(%r) -> %s (0 = ok)" % (value, rc))
