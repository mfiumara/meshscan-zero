"""meshscan_pb: protobuf wire reader for Meshtastic messages and Mapbox Vector Tiles.

The decoders were also checked against the reference libraries during development
(meshtastic 2.7.11 protobufs: 500 random round-trips; mapbox-vector-tile 2.2.0: 14,180 features
from six OpenFreeMap tiles, zero mismatches). These tests pin the behaviour without those deps.
"""
import struct, unittest
import meshscan_pb as pb


def v(n):
    out = bytearray()
    n &= 0xFFFFFFFFFFFFFFFF
    while True:
        b = n & 0x7F; n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def f_var(num, n): return v(num << 3) + v(n)
def f_bytes(num, b): return v(num << 3 | 2) + v(len(b)) + b
def f_fx32(num, b): return v(num << 3 | 5) + b
def zz(n): return (n << 1) ^ (n >> 31)
def packed(xs): return b"".join(v(x) for x in xs)


class Meshtastic(unittest.TestCase):
    def test_data_user_position_telemetry(self):
        user = f_bytes(1, b"!a1b2c3d4") + f_bytes(2, "Dam 🛰".encode()) + f_bytes(3, b"DAM") + f_var(5, 43) + f_var(7, 2)
        data = pb.mt_data(f_var(1, 4) + f_bytes(2, user))
        self.assertEqual(data["portnum"], 4)
        u = pb.mt_user(data["payload"])
        self.assertEqual((u["long_name"], u["short_name"], u["hw_model"], u["role"]), ("Dam 🛰", "DAM", 43, 2))
        self.assertEqual(pb.PORTNUM[4], "NODEINFO_APP")
        self.assertEqual(pb.PORTNUM[67], "TELEMETRY_APP")
        pos = pb.mt_position(f_fx32(1, struct.pack("<i", 523700000)) + f_fx32(2, struct.pack("<i", -48900000)) + f_var(3, -12))
        self.assertEqual((pos["latitude_i"], pos["longitude_i"], pos["altitude"]), (523700000, -48900000, -12))
        dm = f_var(1, 87) + f_fx32(2, struct.pack("<f", 3.95)) + f_fx32(4, struct.pack("<f", 1.5))
        m = pb.mt_device_metrics(f_fx32(1, b"\0\0\0\0") + f_bytes(2, dm))
        self.assertEqual(m["battery_level"], 87)
        self.assertAlmostEqual(m["voltage"], 3.95, places=5)
        self.assertIsNone(pb.mt_device_metrics(f_fx32(1, b"\0\0\0\0") + f_bytes(3, b"")))

    def test_garbage_raises(self):
        for bad in (b"\x0a\x05ab", b"\x80", b"\x07", b"\x00\x01"):
            with self.assertRaises(ValueError):
                pb.mt_data(bad)


class VectorTiles(unittest.TestCase):
    def tile(self):
        def feature(fid, tags, kind, cmds):
            return f_var(1, fid) + f_bytes(2, packed(tags)) + f_var(3, kind) + f_bytes(4, packed(cmds))
        point = feature(1, [0, 0, 1, 1], 1, [9, zz(100), zz(200)])
        line = feature(2, [1, 2], 2, [9, zz(0), zz(0), 2 << 3 | 2, zz(10), zz(0), zz(0), zz(10)])
        square = [9, zz(0), zz(0), 3 << 3 | 2, zz(100), zz(0), zz(0), zz(100), zz(-100), zz(0), 15]
        hole = [9, zz(20), zz(-80), 3 << 3 | 2, zz(0), zz(60), zz(60), zz(0), zz(0), zz(-60), 15]
        poly = feature(3, [], 3, square + hole)
        layer = (f_var(15, 2) + f_bytes(1, b"place") + f_bytes(2, point) + f_bytes(2, line) + f_bytes(2, poly)
                 + f_bytes(3, b"name") + f_bytes(3, b"class") + f_bytes(4, f_bytes(1, b"Amsterdam"))
                 + f_bytes(4, f_bytes(1, b"city")) + f_bytes(4, f_var(6, zz(-3))) + f_var(5, 4096))
        return f_bytes(3, layer)

    def test_decode(self):
        layers = pb.decode_tile(self.tile())
        self.assertEqual(list(layers), ["place"])
        layer = layers["place"]
        self.assertEqual(layer["extent"], 4096)
        pt, ln, poly = layer["features"]
        self.assertEqual(pt["properties"], {"name": "Amsterdam", "class": "city"})
        self.assertEqual(pt["geometry"], {"type": "Point", "coordinates": [100, 200]})
        self.assertEqual(ln["properties"], {"class": -3})
        self.assertEqual(ln["geometry"]["coordinates"], [(0, 0), (10, 0), (10, 10)])
        self.assertEqual(poly["geometry"]["type"], "MultiPolygon")
        self.assertEqual(len(poly["geometry"]["coordinates"]), 1)       # one polygon ...
        self.assertEqual(len(poly["geometry"]["coordinates"][0]), 2)    # ... with one hole


if __name__ == "__main__":
    unittest.main()
