# SPDX-License-Identifier: GPL-2.0-or-later
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import r4850can as r  # noqa: E402

# Real frames captured from an R4850G2
CONFIG1 = bytes.fromhex("0001000040680E27")


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def state(label, value, flags=0):
    return bytes([0x01, label, flags >> 8, flags & 0xFF]) + value.to_bytes(4, "big")


def config6():
    return bytes.fromhex("0006000100000000")


class Base(unittest.TestCase):
    def setUp(self):
        self.clk = Clock()
        self.sent = []
        self.psu = r.R4850(lambda i, d: self.sent.append((i, bytes(d))) or True, clock=self.clk)

    def settings(self):
        return [d.hex().upper() for i, d in self.sent if i == r.REQ_SETTING]

    def online(self):
        self.psu.handle_frame(r.RSP_CONFIG, CONFIG1)
        self.psu.handle_frame(r.RSP_CONFIG, config6())
        self.assertTrue(self.psu.reachable)


class TestEncoding(unittest.TestCase):
    def test_voltage(self):
        self.assertEqual(r.build_online_voltage(54).hex().upper(), "010000000000D800")
        self.assertEqual(r.build_online_voltage(53).hex().upper(), "010000000000D400")

    def test_current(self):
        m = 2048 / 104
        self.assertEqual(r.build_online_current(2, m).hex().upper(), "0103000000000027")
        self.assertEqual(r.build_online_current(5, m).hex().upper(), "0103000000000062")

    def test_production(self):
        self.assertEqual(r.build_production(False).hex(), "0132000100000000")
        self.assertEqual(r.build_production(True).hex(), "0132000000000000")


class TestParsing(Base):
    def test_multiplier(self):
        self.online()
        self.assertAlmostEqual(self.psu.multiplier, 19.6923, 3)

    def test_state(self):
        self.online()
        h = self.psu.handle_frame
        h(r.RSP_STATE, state(0x70, 0x0001B399))
        h(r.RSP_STATE, state(0x73, 0x00017F46))
        h(r.RSP_STATE, state(0x74, 0x00000385))
        h(r.RSP_STATE, state(0x75, 0x0000D3EB))
        h(r.RSP_STATE, state(0x76, 0x00000027))
        h(r.RSP_STATE, state(0x78, 0x000390E0))
        h(r.RSP_STATE, state(0x7F, 0x00002000))
        h(r.RSP_STATE, state(0x80, 0x00000C00))
        h(r.RSP_STATE, state(0x81, 0x000007F6))
        g = self.psu.get
        self.assertAlmostEqual(g("input_power"), 108.9, 1)
        self.assertAlmostEqual(g("output_power"), 95.8, 1)
        self.assertAlmostEqual(g("efficiency"), 88, 0)
        self.assertAlmostEqual(g("output_voltage"), 52.98, 2)
        self.assertAlmostEqual(g("output_current_max"), 1.98, 2)
        self.assertAlmostEqual(g("input_voltage"), 228.2, 1)
        self.assertAlmostEqual(g("output_temperature"), 8.0, 1)
        self.assertAlmostEqual(g("input_temperature"), 3.0, 1)
        self.assertAlmostEqual(g("output_current"), 1.99, 2)

    def test_max_current_needs_multiplier(self):
        self.assertFalse(self.psu.handle_frame(r.RSP_STATE, state(0x76, 0x27)))

    def test_negative_temperature(self):
        self.psu.handle_frame(r.RSP_STATE, state(0x7F, (-2048) & 0xFFFFFFFF))
        self.assertAlmostEqual(self.psu.get("output_temperature"), -2.0, 3)

    def test_unknown_label_kept_aside(self):
        self.psu.handle_frame(r.RSP_STATE, state(0x82, 5))
        self.assertIn(0x82, self.psu.extra)

    def test_foreign_frames_ignored(self):
        self.assertFalse(self.psu.handle_frame(0x1001117E, bytes(8)))
        self.assertFalse(self.psu.handle_frame(r.RSP_STATE, bytes(4)))

    def test_ack(self):
        self.online()
        self.psu.handle_frame(r.RSP_ACK, bytes.fromhex("010000000000D400"))
        self.assertAlmostEqual(self.psu.acks["online_voltage"][0], 0xD400 / 1024, 3)
        self.psu.handle_frame(r.RSP_ACK, bytes.fromhex("0132000100000000"))
        self.assertFalse(self.psu.acks["production_enabled"][0])

    def test_ack_error(self):
        self.psu.handle_frame(r.RSP_ACK, bytes.fromhex("2100000000000000"))
        self.assertEqual(self.psu.ack_errors, 1)


class TestControl(Base):
    def test_passive_start(self):
        self.psu.tick()
        self.assertEqual(self.settings(), [])
        self.assertEqual({i for i, _ in self.sent}, {r.REQ_CONFIG, r.REQ_STATE})
        self.online()
        self.psu.tick()
        self.assertEqual(self.settings(), [])

    def test_blocked_commands(self):
        for cmd in (0x0101, 0x0104, 0x0109):
            with self.assertRaises(ValueError):
                self.psu._send_setting(bytes([cmd >> 8, cmd & 0xFF]) + bytes(6))

    def test_enable_order(self):
        self.online()
        self.psu.set_online_voltage(54)
        self.psu.set_online_current(2)
        self.psu.set_production(True)
        self.psu.tick()
        self.assertEqual(self.settings(), ["0103000000000027", "010000000000D800", "0132000000000000"])

    def test_disable_first(self):
        self.online()
        self.psu.set_online_voltage(53)
        self.psu.set_online_current(2)
        self.psu.set_production(False)
        self.psu.tick()
        self.assertEqual(self.settings()[0], "0132000100000000")

    def test_production_on_needs_both(self):
        self.online()
        self.psu.set_online_voltage(53)
        self.psu.set_production(True)
        self.psu.tick()
        self.assertNotIn("0132000000000000", self.settings())

    def test_nothing_before_reachable(self):
        self.psu.set_online_voltage(53)
        self.psu.set_online_current(2)
        self.psu.set_production(True)
        self.psu.tick()
        self.assertEqual(self.settings(), [])

    def test_clamping_and_nan(self):
        self.assertEqual(self.psu.set_online_voltage(60), 54.0)
        self.assertEqual(self.psu.set_online_voltage(40), 48.0)
        self.assertEqual(self.psu.set_online_current(50), 10.0)
        self.assertEqual(self.psu.set_online_current(-1), 0.0)
        with self.assertRaises(ValueError):
            self.psu.set_online_voltage(float("nan"))
        with self.assertRaises(ValueError):
            self.psu.set_online_current(float("inf"))

    def test_refresh(self):
        self.online()
        self.psu.set_online_voltage(53)
        self.psu.set_online_current(2)
        self.psu.tick()
        n = len(self.settings())
        self.clk.t += 5
        self.psu.handle_frame(r.RSP_CONFIG, config6())
        self.psu.tick()
        self.assertEqual(len(self.settings()), n)
        self.clk.t += 6
        self.psu.handle_frame(r.RSP_CONFIG, config6())
        self.psu.tick()
        self.assertEqual(len(self.settings()), 2 * n)

    def test_timeout_and_resend(self):
        self.online()
        self.psu.set_online_voltage(53)
        self.psu.set_online_current(2)
        self.psu.tick()
        n = len(self.settings())
        self.clk.t += 11
        self.psu.tick()
        self.assertFalse(self.psu.reachable)
        self.assertIsNone(self.psu.multiplier)
        self.assertEqual(len(self.settings()), n)
        self.online()
        self.psu.tick()
        self.assertEqual(len(self.settings()), 2 * n)

    def test_no_current_without_multiplier(self):
        self.online()
        self.psu.set_online_voltage(53)
        self.psu.set_online_current(2)
        self.psu.set_production(True)
        self.psu.multiplier = None
        self.psu.tick()
        self.assertEqual(self.settings(), [])

    def test_safe_off_without_multiplier(self):
        self.psu.safe_off()
        self.assertEqual(self.settings(), ["0132000100000000", "0103000000000000"])

    def test_data_age(self):
        self.assertIsNone(self.psu.data_age())
        self.psu.handle_frame(r.RSP_STATE, state(0x75, 0xD3EB))
        self.clk.t += 3
        self.assertAlmostEqual(self.psu.data_age(), 3)
        self.assertIsNone(self.psu.get("output_voltage", max_age=1))


class TestPersistentSettings(Base):
    def ack(self, data):
        self.psu.handle_frame(r.RSP_ACK, data)

    def test_nothing_written_unless_queued(self):
        self.online()
        for _ in range(10):
            self.clk.t += 5
            self.psu.tick()
        for cmd in ("0101", "0104", "0134", "0135"):
            self.assertFalse([s for s in self.settings() if s.startswith(cmd)], cmd)

    def test_offline_voltage_written_and_acknowledged(self):
        self.psu.queue_setting("offline_voltage", 48.0)
        self.psu.tick()  # not reachable yet
        self.assertEqual([s for s in self.settings() if s.startswith("0101")], [])
        self.online()
        self.psu.tick()
        self.assertEqual([s for s in self.settings() if s.startswith("0101")], ["010100000000C000"])
        self.ack(bytes.fromhex("010100000000C000"))
        self.clk.t += 10
        self.psu.tick()
        self.assertEqual(len([s for s in self.settings() if s.startswith("0101")]), 1)

    def test_retry_and_give_up(self):
        self.online()
        self.psu.queue_setting("fan_online_full_speed", True)
        for _ in range(20):
            self.clk.t += 4
            self.psu.handle_frame(r.RSP_CONFIG, config6())  # PSU keeps answering
            self.psu.tick()
        self.assertEqual(len([s for s in self.settings() if s.startswith("0134")]), self.psu.SETTING_MAX_TRIES)

    def test_rewritten_after_reconnect(self):
        self.online()
        self.psu.queue_setting("fan_offline_full_speed", True)
        self.psu.tick()
        self.ack(bytes.fromhex("0135000100000000"))
        self.assertEqual(self.psu._pending, {})
        self.clk.t += 30  # config replies stop -> unreachable
        self.psu.tick()
        self.assertFalse(self.psu.reachable)
        self.sent.clear()
        self.online()
        self.psu.tick()
        self.assertEqual([s for s in self.settings() if s.startswith("0135")], ["0135000100000000"])

    def test_validation(self):
        for name, value in (("offline_voltage", 60.0), ("offline_voltage", 30.0),
                            ("offline_current", -1.0), ("offline_current", 99.0),
                            ("offline_voltage", float("nan")), ("bogus", 1)):
            with self.assertRaises(ValueError, msg=(name, value)):
                self.psu.queue_setting(name, value)

    def test_wrong_ack_value_is_not_applied(self):
        self.online()
        self.psu.queue_setting("offline_voltage", 48.0)
        self.ack(r.build_offline_voltage(50.0))
        self.assertIn("offline_voltage", self.psu._pending)

    def test_fan_frames(self):
        self.assertEqual(r.build_fan(True, True).hex().upper(), "0134000100000000")
        self.assertEqual(r.build_fan(False, False).hex().upper(), "0135000000000000")


class TestFindInterface(unittest.TestCase):
    def make(self, root, name, driver):
        d = os.path.join(root, name, "device")
        os.makedirs(d)
        os.symlink("../../../../bus/x/drivers/" + driver, os.path.join(d, "driver"))

    def test_finds_by_driver_not_by_name(self):
        with tempfile.TemporaryDirectory() as root:
            self.make(root, "can0", "gs_usb")
            self.make(root, "can1", "mcp251xfd")
            self.assertEqual(r.find_interface("gs_usb", root), "can0")
        with tempfile.TemporaryDirectory() as root:  # swapped numbering
            self.make(root, "can0", "mcp251xfd")
            self.make(root, "can1", "gs_usb")
            self.assertEqual(r.find_interface("gs_usb", root), "can1")

    def test_interface_driver(self):
        with tempfile.TemporaryDirectory() as root:
            self.make(root, "can0", "mcp251xfd")
            self.assertEqual(r.interface_driver("can0", root), "mcp251xfd")
            self.assertIsNone(r.interface_driver("can1", root))

    def test_not_found(self):
        with tempfile.TemporaryDirectory() as root:
            self.make(root, "can0", "mcp251xfd")
            os.makedirs(os.path.join(root, "eth0"))
            self.assertIsNone(r.find_interface("gs_usb", root))
        self.assertIsNone(r.find_interface("gs_usb", "/nonexistent"))


if __name__ == "__main__":
    unittest.main()
