"""Radio pause / busy logic in meshscan.py, with the hardware modules stubbed out."""
import importlib, json, os, sys, tempfile, types, unittest


def stub_hardware(busy):
    gpiod = types.ModuleType("gpiod")
    line = types.ModuleType("gpiod.line")
    line.Direction = types.SimpleNamespace(OUTPUT=1, INPUT=0)
    line.Value = types.SimpleNamespace(ACTIVE=1)
    gpiod.line = line
    gpiod.LineSettings = lambda **kw: kw

    class Req:
        def release(self):
            busy["released"] += 1

    def request_lines(path, consumer, config):
        busy["probes"] += 1
        if busy["held"]:
            raise OSError(16, "Device or resource busy")
        return Req()
    gpiod.request_lines = request_lines
    sys.modules.update({"gpiod": gpiod, "gpiod.line": line,
                        "smbus2": types.ModuleType("smbus2"), "spidev": types.ModuleType("spidev")})


class RadioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MESHSCAN_STATE"] = self.tmp.name
        os.environ["MESHSCAN_CFG"] = os.path.join(self.tmp.name, "none.json")
        self.hw = {"held": False, "probes": 0, "released": 0}
        stub_hardware(self.hw)
        for m in ("meshscan_common", "meshscan"):
            sys.modules.pop(m, None)
        self.ms = importlib.import_module("meshscan")
        self.common = self.ms.common
        os.makedirs(self.common.CTL, exist_ok=True)
        import time as _t
        self._orig = (_t.sleep, _t.time)
        _t.sleep = lambda s: None  # no real waiting
        self.status = []
        self.ms.write_status_raw = lambda d: self.status.append(d)

    def tearDown(self):
        import time as _t
        _t.sleep, _t.time = self._orig
        self.tmp.cleanup()

    def ctl(self, name, value):
        with open(os.path.join(self.common.CTL, name), "w") as f:
            f.write(value)

    def test_switch_file_defaults_on(self):
        self.assertTrue(self.common.radio_on())
        self.ctl("radio", "off"); self.assertFalse(self.common.radio_on())
        self.ctl("radio", "on"); self.assertTrue(self.common.radio_on())

    def test_mode_reader_reports_paused_but_keeps_base_mode(self):
        mode_of = self.ms.mode_reader({"configured": True, "default_mode": "chat"})
        self.assertEqual(mode_of(), "chat")
        self.ctl("radio", "off")
        self.assertEqual(mode_of(), "paused")
        self.assertEqual(mode_of.base(), "chat")

    def test_unconfigured_never_chat(self):
        self.ctl("mode", "chat")
        self.assertEqual(self.ms.mode_reader({"configured": False})(), "scan")

    def test_free_radio_returns_immediately(self):
        self.assertTrue(self.ms.wait_for_radio(lambda: "scan"))
        self.assertEqual(self.hw["released"], 1)  # the probe gives the lines straight back

    def test_busy_reports_and_backs_off_then_recovers(self):
        self.hw["held"] = True
        calls = {"n": 0}
        real_free = self.ms.radio_free

        def free():
            calls["n"] += 1
            if calls["n"] == 4:
                self.hw["held"] = False
            return real_free()
        self.ms.radio_free = free
        t = [0.0]
        self.ms.time.time = lambda: t.__setitem__(0, t[0] + 1) or t[0]
        self.assertTrue(self.ms.wait_for_radio(lambda: "scan"))
        busy = [s for s in self.status if s.get("radio") == "busy"]
        self.assertEqual(len(busy), 3)
        self.assertEqual(busy[0]["radio_message"], "Radio in use by another app")

    def test_paused_never_touches_hardware_and_stops_on_reload(self):
        seq = iter(["paused", "paused", "reload"])
        self.assertFalse(self.ms.wait_for_radio(lambda: next(seq)))
        self.assertEqual(self.hw["probes"], 0)
        self.assertTrue(all(s["radio"] == "paused" for s in self.status))


if __name__ == "__main__":
    unittest.main()
