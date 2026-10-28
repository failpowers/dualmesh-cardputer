#!/usr/bin/env python3
"""Prepare clean pinned source trees, build all images, and package a fresh install."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MT_ENV = "m5stack-cardputer-adv-dualmesh"
MC_ENV = "M5Stack_Cardputer_ADV_companion_radio_ble"
LAUNCHER_ENV = "cardputer_adv_launcher"


def run(*args, cwd=None, env=None):
    command = [str(arg) for arg in args]
    print("+ " + shlex.join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def git_output(directory, *args):
    return subprocess.check_output(
        ["git", "-C", str(directory), *args], text=True
    ).strip()


def require_empty(directory):
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise ValueError(f"Refusing to reuse nonempty directory: {directory}")


def clone_source(source, destination):
    revision = source["revision"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(f"Source revision must be a full commit SHA: {revision}")
    run("git", "clone", "--filter=blob:none", "--no-checkout",
        source["repository"], destination)
    run("git", "-C", destination, "checkout", "--detach", revision)
    if git_output(destination, "rev-parse", "HEAD") != revision:
        raise ValueError(f"Source revision mismatch in {destination}")
    run("git", "-C", destination, "submodule", "update", "--init", "--recursive")


def prepare(work, sources):
    require_empty(work)
    work.mkdir(parents=True, exist_ok=True)
    # Build the launcher copy, never an existing .pio tree in the source checkout.
    shutil.copytree(ROOT / "launcher", work / "launcher",
                    ignore=shutil.ignore_patterns(".pio", "__pycache__"))
    shutil.copytree(ROOT / "common", work / "common")
    shutil.copy2(ROOT / "partitions_dualmesh.csv", work / "partitions_dualmesh.csv")
    for name in ("meshtastic", "meshcore"):
        clone_source(sources[name], work / name)

    mt = work / "meshtastic"
    for patch in sorted((ROOT / "meshtastic-overlay").glob("*.patch")):
        run("git", "-C", mt, "apply", "--check", patch)
        run("git", "-C", mt, "apply", patch)
    stock = mt / "variants/esp32s3/m5stack_cardputer_adv"
    variant = mt / "variants/esp32s3/m5stack_cardputer_adv_dualmesh"
    variant.mkdir()
    for name in ("pins_arduino.h", "variant.h"):
        shutil.copy2(stock / name, variant / name)
    for header in sorted((ROOT / "common").glob("*.h")):
        shutil.copy2(header, variant / header.name)
    shutil.copy2(ROOT / "partitions_dualmesh.csv", variant / "partitions_dualmesh.csv")
    shutil.copy2(ROOT / "meshtastic-overlay/platformio_dualmesh_env.ini",
                 variant / "platformio.ini")

    mc = work / "meshcore"
    patch = ROOT / "meshcore-cardputer/0004-cardputer-keyboard-dualmesh.patch"
    run("git", "-C", mc, "apply", "--check", patch)
    run("git", "-C", mc, "apply", patch)
    variant = mc / "variants/m5stack_cardputer_adv"
    shutil.copy2(ROOT / "common/dualmesh_ota.h", variant / "dualmesh_ota.h")
    shutil.copy2(ROOT / "partitions_dualmesh.csv", variant / "partitions_dualmesh.csv")
    metadata = {
        "dualmesh": git_output(ROOT, "rev-parse", "HEAD"),
        "sources": sources,
    }
    (work / "sources-prepared.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def pio_environment(work, firmware):
    env = os.environ.copy()
    # Each firmware uses a different Arduino/IDF package set. Keep even the
    # platform copies private so the MT workaround cannot alter another build.
    env["PLATFORMIO_CORE_DIR"] = str(work / "platformio")
    env["PLATFORMIO_PACKAGES_DIR"] = str(work / "platformio" / firmware / "packages")
    env["PLATFORMIO_PLATFORMS_DIR"] = str(work / "platformio" / firmware / "platforms")
    return env


def fix_mt_scons(project, env):
    # Install before patching: extra_scripts run too late for nested IDF setup.
    run(sys.executable, "-m", "platformio", "pkg", "install",
        "--project-dir", project, "--environment", MT_ENV, env=env)
    resolver = (
        "from platformio.project.config import ProjectConfig; "
        "from platformio.package.manager.platform import PlatformPackageManager; "
        "c = ProjectConfig.get_instance(); "
        f"spec = c.get('env:{MT_ENV}', 'platform'); "
        "p = PlatformPackageManager().get_package(spec); "
        "assert p is not None, 'Meshtastic platform was not installed'; "
        "print(p.path)"
    )
    platform = Path(subprocess.check_output(
        [sys.executable, "-c", resolver], cwd=project, env=env, text=True
    ).strip())
    private_platforms = Path(env["PLATFORMIO_PLATFORMS_DIR"]).resolve()
    if not platform.resolve().is_relative_to(private_platforms):
        raise ValueError(f"Refusing to patch a shared platform: {platform}")
    path = platform / "platform.py"
    text = path.read_text()
    old = ('COMMON_IDF_PACKAGES = [\n'
           '    "tool-cmake",\n'
           '    "tool-ninja",\n'
           '    "tool-scons",\n'
           '    "tool-esp-rom-elfs"\n'
           ']')
    new = old.replace('    "tool-scons",\n', '')
    if text.count(old) != 1:
        raise ValueError(f"Pinned MT platform changed; SCons workaround does not match: {path}")
    path.write_text(text.replace(old, new, 1))
    print(f"Applied MT COMMON_IDF_PACKAGES SCons workaround: {path}", flush=True)


def pio_run(project, environment, env, *targets):
    command = [sys.executable, "-m", "platformio", "run", "--project-dir", project,
               "--environment", environment]
    for target in targets:
        command.extend(["--target", target])
    run(*command, env=env)


def exactly_one(directory, pattern, exclude_factory=False):
    matches = sorted(p for p in directory.glob(pattern)
                     if p.is_file() and (not exclude_factory or "factory" not in p.name))
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {pattern} in {directory}, found {matches}")
    return matches[0]


def build(work, output, metadata):
    launcher = work / "launcher"
    mt = work / "meshtastic"
    mc = work / "meshcore"
    pio_run(launcher, LAUNCHER_ENV, pio_environment(work, "launcher"))
    mt_env = pio_environment(work, "meshtastic")
    fix_mt_scons(mt, mt_env)
    # The pinned Meshtastic default target builds both the app and LittleFS.
    # A second buildfs-only invocation cleans away the app binary on this platform.
    pio_run(mt, MT_ENV, mt_env)
    pio_run(mc, MC_ENV, pio_environment(work, "meshcore"))

    launcher_images = launcher / ".pio/build" / LAUNCHER_ENV
    mt_images = mt / ".pio/build" / MT_ENV
    mc_images = mc / ".pio/build" / MC_ENV
    command = [sys.executable, ROOT / "scripts/pack_firmware.py",
               "--bootloader", mc_images / "bootloader.bin",
               "--partitions", launcher_images / "partitions.bin",
               "--launcher", launcher_images / "firmware.bin",
               "--meshtastic", exactly_one(mt_images, "firmware-*.bin", exclude_factory=True),
               "--meshtastic-filesystem", exactly_one(mt_images, "littlefs-*.bin"),
               "--meshcore", mc_images / "firmware.bin", "--output-dir", output,
               "--source", f"dualmesh={metadata['dualmesh']}"]
    for name, source in metadata["sources"].items():
        command.extend(["--source", f"{name}={source['revision']}",
                        "--source", f"{name}_repository={source['repository']}"])
    run(*command)
    for name in ("SOURCES.md", "README.md", "requirements-build.txt"):
        shutil.copy2(ROOT / name, output / name)
    shutil.copy2(ROOT / "scripts/install.py", output / "install.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=ROOT / ".build",
                        help="empty build workspace (default: repository .build)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist",
                        help="empty package directory (default: repository dist)")
    parser.add_argument("--prepare-only", action="store_true",
                        help="clone sources and apply overlays without building; workspace cannot be reused")
    args = parser.parse_args()
    try:
        sources = json.loads((ROOT / "sources.json").read_text())
        work = args.work_dir.resolve()
        output = args.output_dir.resolve()
        if not args.prepare_only:
            require_empty(output)
        metadata = prepare(work, sources)
        if not args.prepare_only:
            build(work, output, metadata)
        print(f"Prepared sources: {work}" if args.prepare_only else f"Firmware package: {output}")
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Build failed: {error}\n")


if __name__ == "__main__":
    main()
