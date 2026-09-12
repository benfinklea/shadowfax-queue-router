"""FLEET_IPS must address the box it names, not a device that happens to
answer ping on a nearby address (benfinklea/shadowfax-queue-router#38)."""
import unittest
import queue_router as qr

# 192.168.1.12 is a Nest device (MAC OUI 18:b4:30) that answers ping and
# refuses SSH - the exact signature that painted shadowfax OFFLINE forever
# even though the box itself was up 1 week 8 hours.
NEST_DEVICE_IP = "192.168.1.12"
SHADOWFAX_REAL_IP = "192.168.1.49"


class FleetIpsTest(unittest.TestCase):
    def test_shadowfax_points_at_shadowfax_not_the_nest_device(self):
        self.assertEqual(qr.FLEET_IPS["shadowfax"], SHADOWFAX_REAL_IP,
                          "shadowfax must resolve to its own verified LAN IP")
        self.assertNotEqual(qr.FLEET_IPS["shadowfax"], NEST_DEVICE_IP,
                             "shadowfax must not be pinned to the Nest device's IP")

    def test_sam_and_southfarthing_are_untouched_genuinely_offline_boxes(self):
        # Issue #38 is explicit: these two are genuinely down, not misaddressed.
        # A fix that "corrects" them too would hide a real outage.
        self.assertEqual(qr.FLEET_IPS["sam"], "192.168.1.14")
        self.assertEqual(qr.FLEET_IPS["southfarthing"], "192.168.1.61")


if __name__ == "__main__":
    unittest.main()
