"""Offline distribution tests. Run: python -m unittest discover -s tests -p test_pack_firmware.py"""

import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pack_firmware", ROOT / "scripts/pack_firmware.py")
packer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packer)


def image_fixture():
    """Small, checksum/digest-valid ESP32-S3 image, not a runnable firmware."""
    header = bytearray.fromhex("e9 01 02 3f 00 00 37 40 ee 00 00 00 09 00 00 00 00 ff ff 00 00 00 00 01")
    segment = bytes(range(16))
    data = header + struct.pack("<II", 0x40370000, len(segment)) + segment
    checksum = 0xEF
    for byte in segment:
        checksum ^= byte
    data.extend(b"\0" * (15 - len(data) % 16))
    data.append(checksum)
    return bytes(data) + hashlib.sha256(data).digest()


class PackFirmwareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.layout, partition_binary = packer.load_partitions(ROOT / "partitions_dualmesh.csv")
        self.inputs = {}
        for name in ("bootloader", "partitions", *packer.COMPONENT_PARTITIONS):
            if name == "partitions":
                data = partition_binary
            elif name == "meshtastic-filesystem":
                data = b"test-public-filesystem".ljust(self.layout["spiffs"][1], b"\xff")
            else:
                data = image_fixture()
            path = self.directory / f"input-{name}.bin"
            path.write_bytes(data)
            self.inputs[name] = path

    def test_layout_matches_shared_csv(self):
        self.assertEqual(self.layout, {
            "nvs": (0x9000, 0x5000), "otadata": (0xE000, 0x2000),
            "factory": (0x10000, 0x100000), "ota_0": (0x110000, 0x3A0000),
            "spiffs": (0x4B0000, 0x100000), "ota_1": (0x5B0000, 0x200000),
            "mc_data": (0x7B0000, 0x40000), "coredump": (0x7F0000, 0x10000),
        })
        data = self.inputs["partitions"].read_bytes()
        self.assertEqual(len(data), 0xC00)
        self.assertEqual(data[256:272], b"\xeb\xeb" + b"\xff" * 14)
        self.assertEqual(data[272:288], hashlib.md5(data[:256]).digest())
        self.assertEqual(data[288:], b"\xff" * (0xC00 - 288))

    def test_package_hashes_fresh_ranges_and_reproducibility(self):
        output = self.directory / "release"
        sources = {"dualmesh": "public-revision", "meshtastic": "upstream-revision"}
        manifest = packer.pack(self.inputs, output, sources)
        merged = (output / packer.FULL_IMAGE).read_bytes()
        self.assertEqual(len(merged), 0x800000)
        expected = bytearray(b"\xff" * 0x800000)
        for item in manifest["components"]:
            data = (output / item["file"]).read_bytes()
            self.assertEqual(data, self.inputs[item["name"]].read_bytes())
            self.assertEqual(item["bytes"], len(data))
            self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest())
            offset = int(item["offset"], 0)
            expected[offset:offset + len(data)] = data
        self.assertEqual(merged, expected)
        for label in ("nvs", "otadata", "mc_data", "coredump"):
            offset, size = self.layout[label]
            self.assertEqual(merged[offset:offset + size], b"\xff" * size)
        self.assertEqual(manifest["full_image"]["sha256"], hashlib.sha256(merged).hexdigest())
        self.assertEqual(manifest["sources"], sources)
        text = (output / "manifest.json").read_text()
        self.assertNotIn(str(self.directory), text)
        self.assertNotIn("backup", text)
        self.assertEqual(json.loads(text), manifest)
        summed = set()
        for line in (output / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ")
            self.assertEqual(digest, hashlib.sha256((output / name).read_bytes()).hexdigest())
            summed.add(name)
        self.assertEqual(summed, {p.name for p in output.iterdir()} - {"SHA256SUMS"})
        second = self.directory / "release-again"
        packer.pack(self.inputs, second, sources)
        for path in output.iterdir():
            self.assertEqual(path.read_bytes(), (second / path.name).read_bytes())

    def test_rejects_oversize_or_empty_raw_inputs(self):
        slots = {"bootloader": 0x8000, "partitions": 0x1000,
                 **{name: self.layout[label][1] for name, label in packer.COMPONENT_PARTITIONS.items()}}
        for name, size in slots.items():
            path = self.inputs[name]
            original = path.read_bytes()
            for data in (b"", b"\xff" * (size + 1)):
                with self.subTest(name=name, bytes=len(data)):
                    path.write_bytes(data)
                    with mock.patch.object(packer, "validate_app"), self.assertRaisesRegex(ValueError, "does not fit"):
                        packer.pack(self.inputs, self.directory / "bad-release")
            path.write_bytes(original)

    def test_rejects_wrong_partition_binary(self):
        path = self.inputs["partitions"]
        original = path.read_bytes()
        for data in (original[:-1], original + b"\xff", original[:5] + b"\0" + original[6:]):
            path.write_bytes(data)
            with mock.patch.object(packer, "validate_app"), self.assertRaisesRegex(ValueError, "exactly match"):
                packer.pack(self.inputs, self.directory / "bad-release")

    def test_rejects_short_filesystem(self):
        self.inputs["meshtastic-filesystem"].write_bytes(b"short")
        with mock.patch.object(packer, "validate_app"), self.assertRaisesRegex(ValueError, "exactly 1048576"):
            packer.pack(self.inputs, self.directory / "bad-release")

    def test_rejects_wrong_chip_and_corrupt_validation_hash(self):
        path = self.inputs["bootloader"]
        original = path.read_bytes()
        wrong_chip = bytearray(original)
        wrong_chip[12] = 0
        path.write_bytes(wrong_chip)
        with self.assertRaisesRegex(ValueError, "raw ESP32-S3"):
            packer.pack(self.inputs, self.directory / "bad-release")
        corrupt = bytearray(original)
        corrupt[-1] ^= 0x01
        path.write_bytes(corrupt)
        with self.assertRaisesRegex(ValueError, "invalid image"):
            packer.pack(self.inputs, self.directory / "bad-release")
        self.assertFalse((self.directory / "bad-release").exists())

    def test_rejects_nonempty_output_directory(self):
        with self.assertRaisesRegex(ValueError, "must be empty"):
            packer.pack(self.inputs, self.directory)


if __name__ == "__main__":
    unittest.main()
