#!/usr/bin/env python3
"""Fresh install only: erases ALL settings and identities; never an update tool.

Enter the ESP32-S3 ROM download bootloader manually before running. No serial
port is selected automatically and no reset or reboot is issued. SHA-256 checks
catch corruption, not a malicious release: obtain the bundle from a trusted
release source. Backups contain private keys; keep the backup directory private.
Requires esptool 5.x in the Python environment running this script.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

FLASH_BYTES = 0x800000
FULL_IMAGE = "dualmesh-cardputer-adv-full.bin"
# These are the public DualMesh layout, not user-configurable write ranges.
COMPONENTS = {
    "bootloader": ("bootloader.bin", 0, 0x8000),
    "partitions": ("partitions.bin", 0x8000, 0x1000),
    "launcher": ("launcher.bin", 0x10000, 0x100000),
    "meshtastic": ("meshtastic.bin", 0x110000, 0x3A0000),
    "meshtastic-filesystem": ("meshtastic-filesystem.bin", 0x4B0000, 0x100000),
    "meshcore": ("meshcore.bin", 0x5B0000, 0x200000),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def bundle_file(bundle, name):
    if not isinstance(name, str) or Path(name).name != name or name in ("", ".", ".."):
        raise ValueError(f"Unsafe bundle filename: {name!r}")
    path = bundle / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != bundle:
        raise ValueError(f"Bundle file is missing or path mismatch: {name}")
    return path.read_bytes()


def offset(value):
    if isinstance(value, str):
        return int(value, 0)
    if type(value) is not int:
        raise ValueError("Offset must be an integer or a hexadecimal string")
    return value


def verify_bundle(bundle):
    """Return the exact full-image bytes verified against both release records."""
    sums = {}
    for line in bundle_file(bundle, "SHA256SUMS").decode("utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if not match or match[2] in sums:
            raise ValueError("Malformed or duplicate SHA256SUMS entry")
        sums[match[2]] = match[1]
    expected_names = {"manifest.json", FULL_IMAGE} | {v[0] for v in COMPONENTS.values()}
    if set(sums) != expected_names:
        raise ValueError("SHA256SUMS file list does not match the release layout")
    files = {name: bundle_file(bundle, name) for name in expected_names}
    for name, data in files.items():
        if digest(data) != sums[name]:
            raise ValueError(f"SHA256SUMS mismatch: {name}")
    manifest = json.loads(files["manifest.json"])
    if (manifest["schema_version"] != 1 or manifest["chip"] != "esp32s3"
            or manifest["flash_bytes"] != FLASH_BYTES):
        raise ValueError("Bundle must declare schema 1, ESP32-S3 and 8 MiB flash")
    full = manifest["full_image"]
    image = files[FULL_IMAGE]
    if (full["file"] != FULL_IMAGE or offset(full["offset"]) != 0
            or full["bytes"] != FLASH_BYTES or len(image) != FLASH_BYTES
            or full["sha256"] != digest(image)):
        raise ValueError("Full image metadata, size or hash mismatch")
    entries = manifest["components"]
    if not isinstance(entries, list) or len(entries) != len(COMPONENTS):
        raise ValueError("Manifest must contain all six release components")
    seen = set()
    merged = bytearray(b"\xff" * FLASH_BYTES)
    for entry in entries:
        name = entry["name"]
        if name not in COMPONENTS or name in seen:
            raise ValueError(f"Unknown or duplicate component: {name}")
        seen.add(name)
        filename, start, maximum = COMPONENTS[name]
        if (entry["file"] != filename or offset(entry["offset"]) != start
                or entry["slot_bytes"] != maximum):
            raise ValueError(f"Component layout/path mismatch: {name}")
        data = files[filename]
        if (type(entry["bytes"]) is not int or entry["bytes"] != len(data)
                or not 0 < len(data) <= maximum or entry["sha256"] != digest(data)):
            raise ValueError(f"Component size/hash mismatch: {name}")
        merged[start:start + len(data)] = data
    if image != merged:
        raise ValueError("Full image does not match components and erased (FF) gaps")
    for name in ("bootloader", "launcher", "meshtastic", "meshcore"):
        data = files[COMPONENTS[name][0]]
        if len(data) < 24 or data[0] != 0xE9 or int.from_bytes(data[12:14], "little") != 9:
            raise ValueError(f"{name}: expected a raw ESP32-S3 image")
    return image


def run_tool(command):
    print("+ " + " ".join(map(str, command)), flush=True)
    result = subprocess.run(command, check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout, end="", flush=True)
    return result.stdout


def record(path, **fields):
    path.write_text(json.dumps(fields, indent=2) + "\n", encoding="utf-8")


def install(options, runner=run_tool):
    if not options.confirm_erase:
        raise ValueError("--confirm-erase is required: this clean install erases ALL settings and identities")
    if not options.port.strip() or options.baud <= 0:
        raise ValueError("An explicit nonempty --port and positive --baud are required")
    bundle = options.bundle.resolve(strict=True)
    if not bundle.is_dir():
        raise ValueError("--bundle must be an extracted release directory")
    backup_dir = options.backup_dir.resolve()
    if (backup_dir == bundle or bundle in backup_dir.parents
            or backup_dir in bundle.parents):
        raise ValueError("Backup and bundle paths must be separate, non-nested directories")
    if options.backup_dir.is_symlink() or backup_dir.exists():
        raise ValueError("--backup-dir must be a NEW directory; refusing to overwrite backups")
    image = verify_bundle(bundle)
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    snapshot = backup_dir / FULL_IMAGE
    snapshot.write_bytes(image)
    # Autodetect the image type; forcing --chip would mask a wrong bootloader.
    image_info = runner([sys.executable, "-m", "esptool", "image-info", str(snapshot)])
    if (not re.search(r"^Detected image type: ESP32-?S3\s*$", image_info, re.MULTILINE)
            or "invalid" in image_info.lower()
            or not re.search(r"Checksum: .*\(valid\)", image_info)
            or not re.search(r"Validation hash: .*\(valid\)", image_info)):
        raise ValueError("Bootloader image-info did not validate an ESP32-S3 image")

    def tool(*args):
        return runner([sys.executable, "-m", "esptool", "--chip", "esp32s3",
                       "--port", options.port, "--baud", str(options.baud),
                       "--before", "no-reset", "--after", "no-reset", *map(str, args)])

    identity = tool("flash-id")
    (backup_dir / "device-info.txt").write_text(identity, encoding="utf-8")
    if not re.search(r"^Chip (?:type|is):\s+ESP32-S3(?:\b|\s)", identity, re.MULTILINE):
        raise ValueError("Device did not identify as ESP32-S3")
    if not re.search(r"^Detected flash size:\s*8MB\s*$", identity, re.MULTILINE):
        raise ValueError("Device must have exactly 8MB flash")

    def read_full(path):
        tool("read-flash", "0x0", hex(FLASH_BYTES), path)
        data = path.read_bytes()
        if len(data) != FLASH_BYTES:
            raise ValueError(f"Incomplete full-flash read; retained {path}")
        return data

    first = backup_dir / "original-flash.bin"
    second = backup_dir / "original-flash-second.bin"
    original = read_full(first)
    repeated = read_full(second)
    matching = original == repeated
    record(backup_dir / "backup-manifest.json", port=options.port, bytes=FLASH_BYTES,
           first_sha256=digest(original), second_sha256=digest(repeated),
           independent_reads_match=matching, reboot_issued=False)
    if not matching:
        raise ValueError(f"Backup reads differ; refusing to erase. Both retained in {backup_dir}")
    # Never let esptool alter bootloader flash parameters or bypass its safety checks.
    # The snapshot freezes exactly the release bytes verified before any device access.
    if snapshot.read_bytes() != image:
        raise ValueError("Verified image snapshot changed; refusing to erase")
    tool("write-flash", "--flash-mode", "keep", "--flash-freq", "keep",
         "--flash-size", "keep", "--erase-all", "0x0", snapshot)
    actual = read_full(backup_dir / "installed-readback.bin")
    matches = digest(actual) == digest(image)
    record(backup_dir / "install-manifest.json", port=options.port, bytes=FLASH_BYTES,
           expected_sha256=digest(image), readback_sha256=digest(actual),
           matches=matches, reboot_issued=False)
    if not matches:
        raise ValueError(f"Full flash readback verification FAILED; backups retained in {backup_dir}")
    print(f"Verified all {FLASH_BYTES} flash bytes. Private backups retained in {backup_dir}.")
    print("No reset or reboot issued. Reboot manually when ready.")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--port", required=True, help="Explicit serial port; manually enter ROM download mode")
    result.add_argument("--bundle", required=True, type=Path, help="Extracted trusted release bundle")
    result.add_argument("--backup-dir", required=True, type=Path, help="New private directory for full device backups")
    result.add_argument("--confirm-erase", action="store_true", help="Consent to erase ALL settings and identities")
    result.add_argument("--baud", type=int, default=460800)
    return result


def main(argv=None, runner=run_tool):
    options = parser().parse_args(argv)
    try:
        install(options, runner)
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError) and exc.stdout:
            print(exc.stdout, file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        print("No reboot issued. Do not reboot after a failed write; retain all backup files.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
