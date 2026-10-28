"""Hardware-free safety checks: python -m unittest discover -s tests -p test_install.py."""

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("dualmesh_install", Path(__file__).resolve().parents[1] / "scripts/install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class FakeRunner:
    def __init__(self, image):
        self.image = image
        self.original = b"O" * installer.FLASH_BYTES
        self.calls = []
        self.reads = 0
        self.identity = "Chip type:          ESP32-S3 (QFN56)\nDetected flash size: 8MB\n"
        self.image_info = "Detected image type: ESP32-S3\nChecksum: 00 (valid)\nValidation hash: abcd (valid)\n"
        self.mismatch_backup = False
        self.bad_readback = False
        self.short_read = False
        self.fail_write = False
        self.mutate_snapshot = False

    def __call__(self, command):
        self.calls.append(command)
        if "image-info" in command:
            return self.image_info
        if "flash-id" in command:
            return self.identity
        if "read-flash" in command:
            self.reads += 1
            data = self.original if self.reads <= 2 else self.image
            if self.reads == 2 and self.mismatch_backup:
                data = b"X" + data[1:]
            if self.reads == 3 and self.bad_readback:
                data = b"X" + data[1:]
            if self.short_read:
                data = data[:-1]
            Path(command[-1]).write_bytes(data)
            if self.reads == 2 and self.mutate_snapshot:
                (Path(command[-1]).parent / installer.FULL_IMAGE).write_bytes(b"changed")
        if "write-flash" in command:
            if self.reads != 2:
                raise AssertionError("Write before two backups")
            if self.fail_write:
                raise subprocess.CalledProcessError(1, command, output="write interrupted")
        return ""

    @property
    def writes(self):
        return [call for call in self.calls if "write-flash" in call]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "release"
        self.bundle.mkdir()
        image = bytearray(b"\xff" * installer.FLASH_BYTES)
        components = []
        for index, (name, (filename, start, maximum)) in enumerate(installer.COMPONENTS.items()):
            data = bytearray(bytes([index + 1]) * 48)
            if name in ("bootloader", "launcher", "meshtastic", "meshcore"):
                data[0] = 0xE9
                data[12:14] = b"\x09\x00"
            data = bytes(data)
            (self.bundle / filename).write_bytes(data)
            image[start:start + len(data)] = data
            components.append(dict(name=name, file=filename, offset=hex(start),
                                   slot_bytes=maximum, bytes=len(data), sha256=installer.digest(data)))
        self.image = bytes(image)
        (self.bundle / installer.FULL_IMAGE).write_bytes(self.image)
        self.manifest = dict(schema_version=1, chip="esp32s3", flash_bytes=installer.FLASH_BYTES,
                             full_image=dict(file=installer.FULL_IMAGE, offset="0x0",
                                             bytes=len(image), sha256=installer.digest(image)),
                             components=components)
        self.save_manifest()
        self.options = argparse.Namespace(port="/dev/explicit-test-port", bundle=self.bundle,
                                          backup_dir=self.root / "private-backup", baud=460800,
                                          confirm_erase=True)
        self.runner = FakeRunner(self.image)

    def save_manifest(self):
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest))
        self.save_sums()

    def save_sums(self):
        names = {"manifest.json", installer.FULL_IMAGE} | {v[0] for v in installer.COMPONENTS.values()}
        (self.bundle / "SHA256SUMS").write_text("".join(
            f"{installer.digest((self.bundle / name).read_bytes())}  {name}\n" for name in sorted(names)))

    def run_install(self):
        installer.install(self.options, self.runner)

    def assert_refused(self):
        with self.assertRaises((ValueError, OSError)):
            self.run_install()
        self.assertEqual(self.runner.writes, [])

    def test_full_install_backs_up_twice_and_verifies_without_reset(self):
        self.run_install()
        self.assertEqual(self.runner.reads, 3)
        self.assertEqual(len(self.runner.writes), 1)
        command = self.runner.writes[0]
        self.assertIn("--erase-all", command)
        self.assertEqual(command[-2], "0x0")
        self.assertEqual(Path(command[-1]).read_bytes(), self.image)
        for flag in ("--flash-mode", "--flash-freq", "--flash-size"):
            self.assertEqual(command[command.index(flag) + 1], "keep")
        for call in self.runner.calls:
            if "image-info" not in call:
                for flag in ("--before", "--after"):
                    self.assertEqual(call[call.index(flag) + 1], "no-reset")
                self.assertEqual(call[call.index("--port") + 1], self.options.port)
                self.assertEqual(call[call.index("--chip") + 1], "esp32s3")
            self.assertNotIn("--force", call)
        backup = self.options.backup_dir
        self.assertEqual((backup / "original-flash.bin").read_bytes(), self.runner.original)
        self.assertEqual((backup / "original-flash-second.bin").read_bytes(), self.runner.original)
        report = json.loads((backup / "install-manifest.json").read_text())
        self.assertTrue(report["matches"])
        self.assertFalse(report["reboot_issued"])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)

    def test_requires_explicit_consent_before_any_tool_or_backup(self):
        self.options.confirm_erase = False
        self.assert_refused()
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.options.backup_dir.exists())

    def test_parser_requires_port_bundle_backup_directory(self):
        for absent in ("--port", "--bundle", "--backup-dir"):
            args = ["--port", "test", "--bundle", "release", "--backup-dir", "backup"]
            pos = args.index(absent)
            del args[pos:pos + 2]
            with self.assertRaises(SystemExit):
                installer.parser().parse_args(args)

    def test_blank_port_and_nonpositive_baud_are_rejected(self):
        self.options.port = " "
        self.assert_refused()
        self.options.port = "test"
        self.options.baud = 0
        self.assert_refused()
        self.assertEqual(self.runner.calls, [])

    def test_existing_backup_directory_never_overwritten(self):
        self.options.backup_dir.mkdir()
        marker = self.options.backup_dir / "private.bin"
        marker.write_bytes(b"private")
        self.assert_refused()
        self.assertEqual(marker.read_bytes(), b"private")

    def test_bundle_and_backup_directories_cannot_overlap(self):
        self.options.backup_dir = self.bundle / "backup"
        self.assert_refused()
        self.options.backup_dir = self.bundle
        self.assert_refused()
        self.assertEqual(self.runner.calls, [])

    def test_corrupt_component_or_full_image_refused_before_tools(self):
        for name in ("meshcore.bin", installer.FULL_IMAGE):
            path = self.bundle / name
            original = path.read_bytes()
            path.write_bytes(b"X" + original[1:])
            self.assert_refused()
            path.write_bytes(original)
        self.assertEqual(self.runner.calls, [])

    def test_manifest_hash_and_declared_component_hash_checked(self):
        (self.bundle / "manifest.json").write_text("{}")
        self.assert_refused()
        self.manifest["components"][0]["sha256"] = "0" * 64
        self.save_manifest()
        self.assert_refused()

    def test_wrong_manifest_layout_and_file_paths_refused(self):
        original = copy.deepcopy(self.manifest)
        for key, value in (("offset", "0x100"), ("file", "../bootloader.bin"), ("slot_bytes", 1)):
            self.manifest = copy.deepcopy(original)
            self.manifest["components"][0][key] = value
            self.save_manifest()
            self.assert_refused()

    def test_duplicate_components_and_checksum_entries_refused(self):
        self.manifest["components"][1] = self.manifest["components"][0]
        self.save_manifest()
        self.assert_refused()
        sums = self.bundle / "SHA256SUMS"
        sums.write_text(sums.read_text() + sums.read_text().splitlines()[0] + "\n")
        self.assert_refused()

    def test_component_symlink_is_refused(self):
        path = self.bundle / "meshcore.bin"
        outside = self.root / "outside.bin"
        path.rename(outside)
        path.symlink_to(outside)
        self.assert_refused()
        self.assertEqual(self.runner.calls, [])

    def test_non_erased_gap_refused_even_with_updated_hashes(self):
        image = bytearray(self.image)
        image[-1] = 0
        (self.bundle / installer.FULL_IMAGE).write_bytes(image)
        self.manifest["full_image"]["sha256"] = installer.digest(image)
        self.save_manifest()
        self.assert_refused()

    def test_non_s3_raw_image_refused_even_with_updated_hashes(self):
        path = self.bundle / "bootloader.bin"
        data = bytearray(path.read_bytes())
        data[12:14] = b"\x00\x00"
        path.write_bytes(data)
        image = bytearray(self.image)
        image[:len(data)] = data
        (self.bundle / installer.FULL_IMAGE).write_bytes(image)
        self.manifest["components"][0]["sha256"] = installer.digest(data)
        self.manifest["full_image"]["sha256"] = installer.digest(image)
        self.save_manifest()
        self.assert_refused()
        self.assertEqual(self.runner.calls, [])

    def test_wrong_bootloader_image_info_or_invalid_checksum_refused(self):
        for info in ("Detected image type: ESP32\n", self.runner.image_info.replace("(valid)", "(invalid)")):
            self.runner.image_info = info
            self.assert_refused()
            # Each attempt deliberately requires a new backup location.
            self.options.backup_dir = self.root / "another-backup"
        self.assertEqual(self.runner.reads, 0)

    def test_wrong_chip_or_flash_size_refused_before_backup(self):
        self.runner.identity = "Chip type: ESP32-C3\nDetected flash size: 8MB\n"
        self.assert_refused()
        self.options.backup_dir = self.root / "another-backup"
        self.runner.identity = "Chip type: ESP32-S3\nDetected flash size: 16MB\n"
        self.assert_refused()
        self.assertEqual(self.runner.reads, 0)

    def test_unstable_backup_refused_and_both_reads_retained(self):
        self.runner.mismatch_backup = True
        self.assert_refused()
        backup = self.options.backup_dir
        self.assertTrue((backup / "original-flash.bin").exists())
        self.assertTrue((backup / "original-flash-second.bin").exists())
        report = json.loads((backup / "backup-manifest.json").read_text())
        self.assertFalse(report["independent_reads_match"])

    def test_short_backup_and_changed_snapshot_refused(self):
        self.runner.short_read = True
        self.assert_refused()
        self.runner.short_read = False
        self.runner.reads = 0
        self.runner.mutate_snapshot = True
        self.options.backup_dir = self.root / "another-backup"
        self.assert_refused()

    def test_failed_full_readback_is_reported_with_backups_retained(self):
        self.runner.bad_readback = True
        with self.assertRaisesRegex(ValueError, "verification FAILED"):
            self.run_install()
        report = json.loads((self.options.backup_dir / "install-manifest.json").read_text())
        self.assertFalse(report["matches"])
        self.assertEqual(len(self.runner.writes), 1)
        self.assertTrue((self.options.backup_dir / "original-flash.bin").exists())

    def test_write_error_has_no_subsequent_read_or_reboot(self):
        self.runner.fail_write = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_install()
        self.assertEqual(self.runner.reads, 2)
        self.assertTrue((self.options.backup_dir / "original-flash.bin").exists())


if __name__ == "__main__":
    unittest.main()
