# SPDX-License-Identifier: GPL-2.0-or-later
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from charger_logic import (CONFIG_DEFAULTS, compute_limits, format_status_line, load_config)  # noqa: E402

BAT = dict(ccl=50.0, cvl=55.2, allow=True, current=0.0)


def go(**kw):
    a = dict(mode_on=True, want_v=54.0, want_i=10.0, vmin=48.0, vmax=55.2, imax=30.0,
             bat=BAT, psu_current=0.0, was_enabled=False)
    a.update(kw)
    return compute_limits(**a)


class T(unittest.TestCase):
    def test_normal(self):
        d = go()
        self.assertTrue(d.enable)
        self.assertEqual((d.voltage, d.current), (54.0, 10.0))

    def test_mode_off(self):
        self.assertFalse(go(mode_on=False).enable)

    def test_no_battery_or_missing(self):
        self.assertFalse(go(bat=None).enable)
        self.assertFalse(go(bat=dict(BAT, ccl=None)).enable)
        self.assertFalse(go(bat=dict(BAT, cvl=None)).enable)
        self.assertFalse(go(bat=dict(BAT, current=None)).enable)
        self.assertFalse(go(bat=dict(BAT, ccl=float("nan"))).enable)

    def test_allow_false(self):
        self.assertFalse(go(bat=dict(BAT, allow=False)).enable)
        self.assertTrue(go(bat=dict(BAT, allow=None)).enable)

    def test_allow_types(self):
        for off in (0, 0.0, "0", False, "no", ""):
            self.assertFalse(go(bat=dict(BAT, allow=off)).enable, repr(off))
        for on in (1, 1.0, "1", True):
            self.assertTrue(go(bat=dict(BAT, allow=on)).enable, repr(on))

    def test_cvl_limits_voltage(self):
        d = go(want_v=55.0, bat=dict(BAT, cvl=53.6))
        self.assertEqual(d.voltage, 53.6)
        d = go(want_v=60.0)
        self.assertEqual(d.voltage, 55.2)

    def test_cvl_below_vmin(self):
        self.assertFalse(go(bat=dict(BAT, cvl=47.0)).enable)

    def test_other_sources_reduce_current(self):
        # The Multiplus charges with 40 A, the Huawei with 0 A, CCL is 50 A -> 10 A headroom
        d = go(want_i=20.0, bat=dict(BAT, current=40.0))
        self.assertEqual(d.current, 10.0)
        self.assertEqual(d.reason, "BMS limit")

    def test_own_current_not_counted_as_other(self):
        # 40 A in total, 10 A of them from the Huawei -> others deliver 30 A -> Huawei may use 20 A
        d = go(want_i=30.0, imax=30.0, bat=dict(BAT, current=40.0), psu_current=10.0)
        self.assertEqual(d.current, 20.0)

    def test_ccl_zero_always_off(self):
        # Dummy BMS artefact: battery current 0 despite 17 A from the Huawei must not allow charging
        d = go(bat=dict(BAT, ccl=0.0, current=0.0), psu_current=17.0, was_enabled=True)
        self.assertFalse(d.enable)

    def test_over_ccl_shuts_off(self):
        self.assertFalse(go(bat=dict(BAT, current=60.0)).enable)

    def test_discharge_gives_headroom(self):
        d = go(want_i=60.0, imax=60.0, bat=dict(BAT, current=-10.0))
        self.assertEqual(d.current, 60.0)  # CCL 50 A + 10 A that the loads are currently drawing

    def test_imax(self):
        self.assertEqual(go(want_i=100.0).current, 30.0)

    def test_hysteresis(self):
        small = dict(want_i=0.8)
        self.assertFalse(go(**small).enable)
        self.assertTrue(go(was_enabled=True, **small).enable)
        self.assertFalse(go(was_enabled=True, want_i=0.3).enable)

    def test_bad_setpoints(self):
        self.assertFalse(go(want_v=None).enable)
        self.assertFalse(go(want_i=float("nan")).enable)

    def test_negative_setpoint(self):
        self.assertFalse(go(want_i=-5.0).enable)

    def test_watchdog(self):
        d = go(watchdog_ok=False)
        self.assertFalse(d.enable)
        self.assertEqual(d.reason, "Heartbeat missing")
        self.assertTrue(go(watchdog_ok=True).enable)


class TestStatusLine(unittest.TestCase):
    def test_full(self):
        v = dict(output_voltage=53.34, output_current=1.98, output_current_max=1.98, output_power=105.4,
                 input_voltage=226.0, input_current=0.52, input_power=118.0, input_frequency=50.43,
                 efficiency=89.0, input_temperature=4.0, output_temperature=8.0)
        line = format_status_line(v, "setpoint: 2.0 A / 54.0 V")
        self.assertIn("out 53.34 V 1.98 A (limit 1.98 A) 105 W", line)
        self.assertIn("in 226 V 0.52 A 118 W 50.4 Hz", line)
        self.assertIn("eff 89 %", line)
        self.assertIn("T in/out 4 C/8 C", line)
        self.assertTrue(line.endswith("setpoint: 2.0 A / 54.0 V"))

    def test_missing_values(self):
        line = format_status_line({}, "PSU unreachable")
        self.assertIn("out - - (limit -) -", line)
        self.assertTrue(line.endswith("PSU unreachable"))


class TestConfig(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_missing_file_gives_defaults(self):
        self.assertEqual(load_config("/nonexistent/config.ini"), CONFIG_DEFAULTS)
        self.assertEqual(load_config(None), CONFIG_DEFAULTS)

    def test_values(self):
        cfg = load_config(self.write("[huawei]\ninterface = can1\nimax = 50\nheartbeat_timeout = 120\ndeviceinstance = 291\n"))
        self.assertEqual(cfg["interface"], "can1")
        self.assertEqual(cfg["imax"], 50.0)
        self.assertEqual(cfg["heartbeat_timeout"], 120.0)
        self.assertEqual(cfg["deviceinstance"], 291)
        self.assertEqual(cfg["vmax"], 55.2)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            load_config(self.write("[huawei]\nimax = a lot\n"))

    def test_psu_settings_default_to_none(self):
        cfg = load_config(self.write("[huawei]\ninterface = can1\noffline_voltage =\n"))
        for k in ("offline_voltage", "offline_current", "fan_online_full_speed", "fan_offline_full_speed"):
            self.assertIsNone(cfg[k])

    def test_psu_settings_parsed(self):
        cfg = load_config(self.write("[huawei]\noffline_voltage = 48\noffline_current = 0\nfan_online_full_speed = true\nfan_offline_full_speed = no\n"))
        self.assertEqual(cfg["offline_voltage"], 48.0)
        self.assertEqual(cfg["offline_current"], 0.0)
        self.assertIs(cfg["fan_online_full_speed"], True)
        self.assertIs(cfg["fan_offline_full_speed"], False)

    def test_psu_settings_invalid(self):
        with self.assertRaises(ValueError):
            load_config(self.write("[huawei]\nfan_online_full_speed = maybe\n"))


if __name__ == "__main__":
    unittest.main()
