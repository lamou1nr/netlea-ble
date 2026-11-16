"""Golden-packet tests.

Every expected packet below was sent to a real Netlea AT5S Gen 3 and seen to
work (or, for cmdId 26 and 5, is the packet the bridge has always sent). If one
of these tests fails, the bridge no longer sends what the light is known to
accept. Do not "fix" the expected values without testing on a real light.

Run from the repository root:  python -m unittest discover -s tests
"""
import os
import sys
import unittest

os.environ.setdefault("NETLEA_MAC", "AA:BB:CC:DD:EE:FF")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ble_bridge as b  # noqa: E402


class Uuids(unittest.TestCase):
    def test_write_goes_to_ff02(self):
        self.assertEqual(b.WRITE_UUID, "0000ff02-0000-1000-8000-00805f9b34fb")

    def test_replies_expected_on_ff01(self):
        self.assertEqual(b.NOTIFY_UUID, "0000ff01-0000-1000-8000-00805f9b34fb")


class Framing(unittest.TestCase):
    def test_checksum_is_sum_mod_256(self):
        self.assertEqual(b.checksum([0x5A, 0x08, 0x01, 0x05, 0, 0, 0]), 0x68)
        self.assertEqual(b.checksum([200, 100]), 44)

    def test_length_counts_every_byte(self):
        p = b.build_packet(16, [0] * 20)
        self.assertEqual(p[1], len(p))
        self.assertEqual(len(p), 27)

    def test_four_byte_little_endian(self):
        self.assertEqual(b.int_to_bytes_le(500, 4), [0xF4, 0x01, 0x00, 0x00])


class GoldenPackets(unittest.TestCase):
    def test_direct_control(self):
        p = b.build_direct_control(lamp_state=1, lamp_mode=0, r=16, w=22, g=8, b=5, f=0, grade=0)
        self.assertEqual(p.hex(), "5a1b011000000100000010160805000000000000000000000000ba")

    def test_off_is_lampstate_0_and_all_channels_0(self):
        p = b.build_power_command(False)
        self.assertEqual(p.hex(), "5a1b01100000000000000000000000000000000000000000000086")

    def test_schedule_single_trip(self):
        trip = {"tripDisable": 0, "weekCycle": 127, "startH": 17, "startM": 59,
                "endH": 18, "endM": 16, "R": 10, "W": 0, "G": 0, "B": 0, "F": 0,
                "modeId": 0, "grade": 0}
        p = b.build_schedule(1, 0, 0, [trip])
        expected = ("5ab001120000" "01" "00000000" "00000000"
                    "007f113b12100a0000000000" "00000000" + "00" * 144 + "15")
        self.assertEqual(p.hex(), expected)
        self.assertEqual(len(p), 176)

    def test_blank_schedule_is_all_zero(self):
        p = b.build_schedule(0, 0, 0, [])
        self.assertEqual(len(p), 176)
        self.assertEqual(set(p[6:-1]), {0})

    def test_manual_control(self):
        p = b.build_manual_control(on=1, off=0, r=10, w=0, g=0, b=0, f=0, grade=0)
        self.assertEqual(p.hex(), "5a19011a000001000000000000000a00000000000000000099")

    def test_cmd5(self):
        self.assertEqual(b.build_read_state().hex(), "5a08010500000068")


if __name__ == "__main__":
    unittest.main()
