import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("build_firmware", Path(__file__).resolve().parents[1] / "scripts/build.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class BuildTests(unittest.TestCase):
    def test_packages_default_meshtastic_app_and_filesystem_without_cleaning_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            output = work / "dist"
            calls = []

            def compile_project(project, environment, env, *targets):
                images = project / ".pio/build" / environment
                images.mkdir(parents=True, exist_ok=True)
                if environment == builder.MT_ENV:
                    if targets:
                        self.fail("A buildfs-only pass removes the already-built app")
                    (images / "firmware-test.bin").write_bytes(b"app")
                    (images / "littlefs-test.bin").write_bytes(b"fs")

            def package(*command):
                calls.append(command)
                output.mkdir()

            with patch.object(builder, "pio_run", side_effect=compile_project), patch.object(builder, "fix_mt_scons"), patch.object(builder, "run", side_effect=package):
                builder.build(work, output, {"dualmesh": "test", "sources": {}})
            command = list(calls[0])
            self.assertEqual(command[command.index("--bootloader") + 1], work / "meshcore/.pio/build" / builder.MC_ENV / "bootloader.bin")
            self.assertTrue(command[command.index("--meshtastic") + 1].is_file())
            self.assertTrue(command[command.index("--meshtastic-filesystem") + 1].is_file())
