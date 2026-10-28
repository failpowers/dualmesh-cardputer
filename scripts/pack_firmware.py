#!/usr/bin/env python3
"""Package public build artifacts for a fresh Cardputer ADV install (never flash).

Run with the Python environment containing esptool >= 5. Raw build outputs only:
never pass a device flash dump or a filesystem read back from a configured device.
Source references are user-supplied public provenance, not local build paths.
"""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
FLASH_BYTES = 0x800000
PARTITION_OFFSET = 0x8000
PARTITION_BINARY_BYTES = 0xC00
FULL_IMAGE = "dualmesh-cardputer-adv-full.bin"
WARNING = (
    "Fresh install only: erases all device settings and identities. "
    "Never use as a settings-preserving update. "
    "Use only public build artifacts, never device flash dumps or private filesystems."
)
COMPONENT_PARTITIONS = {
    "launcher": "factory",
    "meshtastic": "ota_0",
    "meshtastic-filesystem": "spiffs",
    "meshcore": "ota_1",
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_partitions(path):
    """Read the shared layout and reproduce ESP-IDF's MD5 partition binary."""
    entries = {}
    binary = bytearray()
    subtypes = {"nvs": 2, "ota": 0, "spiffs": 0x82, "coredump": 3, "factory": 0}
    previous_end = PARTITION_OFFSET + 0x1000
    lines = (line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))
    for row in csv.reader(lines):
        if not row:
            continue
        name, kind, subtype, offset, size, flags = [field.strip() for field in row]
        offset, size = int(offset, 0), int(size, 0)
        if name in entries or flags or offset < previous_end or size <= 0:
            raise ValueError("Invalid shared partition layout")
        type_id = {"app": 0, "data": 1}[kind]
        subtype_id = 0x10 + int(subtype[4:]) if subtype.startswith("ota_") else subtypes[subtype]
        binary.extend(struct.pack("<HBBII16sI", 0x50AA, type_id, subtype_id,
                                  offset, size, name.encode("ascii"), 0))
        entries[name] = (offset, size)
        previous_end = offset + size
    if previous_end != FLASH_BYTES:
        raise ValueError("Shared partition layout must end at 8 MiB")
    binary.extend(b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(binary).digest())
    binary.extend(b"\xff" * (PARTITION_BINARY_BYTES - len(binary)))
    return entries, bytes(binary)


def esptool(*args):
    result = subprocess.run(
        [sys.executable, "-m", "esptool", "--chip", "esp32s3", *map(str, args)],
        capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode:
        raise ValueError(f"esptool failed: {result.stdout}{result.stderr}")
    return result.stdout + result.stderr


def validate_app(path, data):
    if len(data) < 24 or data[0] != 0xE9 or int.from_bytes(data[12:14], "little") != 9:
        raise ValueError(f"{path.name}: expected a raw ESP32-S3 image")
    report = esptool("image-info", path)
    # image-info prints an invalid checksum/hash without necessarily failing its exit code.
    if ("invalid" in report.lower()
            or not re.search(r"Checksum: .*\(valid\)", report)
            or not re.search(r"Validation hash: .*\(valid\)", report)):
        raise ValueError(f"{path.name}: invalid image checksum or validation hash")


def pack(inputs, output_dir, sources=None):
    """Validate inputs, then publish deterministic raw files, image and metadata."""
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("Output directory must be empty (will not mix release artifacts)")
    csv_path = ROOT / "partitions_dualmesh.csv"
    layout, expected_partitions = load_partitions(csv_path)
    slots = {
        "bootloader": (0, PARTITION_OFFSET),
        "partitions": (PARTITION_OFFSET, layout["nvs"][0] - PARTITION_OFFSET),
        **{name: layout[label] for name, label in COMPONENT_PARTITIONS.items()},
    }
    payloads = {}
    for name, (offset, size) in slots.items():
        path = Path(inputs[name])
        data = path.read_bytes()
        if not data or len(data) > size:
            raise ValueError(f"{name}: {len(data)} bytes does not fit slot of {size} bytes")
        if name == "partitions":
            if data != expected_partitions:
                raise ValueError("partitions: binary must exactly match partitions_dualmesh.csv (3072 bytes)")
        elif name == "meshtastic-filesystem":
            if len(data) != size:
                raise ValueError(f"meshtastic-filesystem: must be exactly {size} bytes")
        else:
            validate_app(path, data)
        payloads[name] = data

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dualmesh-pack-", dir=output_dir.parent) as temporary:
        staging = Path(temporary)
        components = []
        merge_args = []
        for name, (offset, size) in slots.items():
            file = f"{name}.bin"
            (staging / file).write_bytes(payloads[name])
            components.append({"name": name, "file": file, "offset": hex(offset),
                               "bytes": len(payloads[name]), "slot_bytes": size,
                               "sha256": sha256(payloads[name])})
            merge_args.extend((hex(offset), staging / file))
        esptool("merge-bin", "--output", staging / FULL_IMAGE,
                "--pad-to-size", "8MB", *merge_args)
        merged = (staging / FULL_IMAGE).read_bytes()
        # Confirm merge kept every raw byte and every unused range freshly erased.
        expected = bytearray(b"\xff" * FLASH_BYTES)
        for name, (offset, _) in slots.items():
            expected[offset:offset + len(payloads[name])] = payloads[name]
        if merged != expected:
            raise ValueError("Merged image differs from raw inputs plus erased padding")
        manifest = {
            "schema_version": 1,
            "chip": "esp32s3",
            "flash_bytes": FLASH_BYTES,
            "full_image": {"file": FULL_IMAGE, "offset": "0x0", "bytes": len(merged),
                           "sha256": sha256(merged)},
            "components": components,
            "sources": dict(sorted((sources or {}).items())),
            "partition_csv_sha256": sha256(csv_path.read_bytes()),
            "warning": WARNING,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        sums = "".join(f"{sha256(path.read_bytes())}  {path.name}\n"
                       for path in sorted(staging.iterdir()))
        (staging / "SHA256SUMS").write_text(sums)
        output_dir.mkdir(exist_ok=True)
        for path in staging.iterdir():
            path.replace(output_dir / path.name)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=WARNING)
    for name in ("bootloader", "partitions", *COMPONENT_PARTITIONS):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source", action="append", default=[], metavar="NAME=REF",
                        help="Public source revision or artifact provenance (repeatable)")
    args = parser.parse_args(argv)
    sources = {}
    for source in args.source:
        name, separator, ref = source.partition("=")
        if not separator or not name or not ref or name in sources:
            parser.error("--source must be a unique, nonempty NAME=REF")
        sources[name] = ref
    inputs = {name: getattr(args, name.replace("-", "_"))
              for name in ("bootloader", "partitions", *COMPONENT_PARTITIONS)}
    try:
        manifest = pack(inputs, args.output_dir, sources)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Packaging failed: {error}\n")
    print(WARNING)
    print(f"Packaged {manifest['full_image']['bytes']} bytes in {args.output_dir}")


if __name__ == "__main__":
    main()
