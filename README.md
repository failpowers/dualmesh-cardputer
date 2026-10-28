# DualMesh for M5Stack Cardputer ADV

Meshtastic and keyboard-enabled MeshCore on one **Cardputer ADV + Cap LoRa-1262**.
Choose from a launcher; only one network runs at a time. Requires 8 MiB flash.

## Install the same firmware as the reference device

1. Download **dualmesh-cardputer-adv-reference.zip** from
   [Releases](https://github.com/martinemde/dualmesh-cardputer/releases/latest).
   Extract it. Do not use a random Actions run or a guest `factory.bin`.
2. Clone this repository and install the pinned tools (Python 3.13 recommended):
   ```sh
   git clone https://github.com/martinemde/dualmesh-cardputer.git
   cd dualmesh-cardputer
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements-build.txt
   ```
3. Connect the Cardputer by USB. Keep the LoRa antenna attached. Close serial
   monitors. Enter download mode (hold BOOT while resetting/powering on, then
   release BOOT). Identify its serial port explicitly.
4. **This is a fresh install. It erases all settings, identities and messages.**
   Backups contain private data: keep them outside the repository and do not share.
   Run, replacing the paths and port:
   ```sh
   .venv/bin/python scripts/install.py \
     --bundle /path/to/extracted-bundle \
     --port /dev/cu.usbmodemXXXX \
     --backup-dir /path/to/private-backups \
     --confirm-erase
   ```
   The installer checks bundle hashes, chip and flash size, requires two matching
   8 MiB backups, writes at offset 0, and compares a complete readback. It does
   not reboot automatically. Reset/power-cycle after it reports success.

The release uses the same public application binaries as the reference device,
with fresh storage—not a clone of someone's identities, settings or bonds.
See `manifest.json`, `SHA256SUMS` and `SOURCES.md` in the bundle.

### Existing DualMesh devices

**Do not use the full image to update an existing installation.** It resets all
storage. Advanced slot-only updates need a full verified backup and readback,
the exact matching partition table, and the raw app at the correct offset below.
Do not use PlatformIO upload, uploadfs, either guest's factory bundle, or either
firmware's self-OTA: ordinary A/B OTA can overwrite the other guest.

## Use

- Tap BOOT to select Meshtastic or MeshCore; hold about 1.2 seconds to launch.
- After release, no input for 5 seconds launches the last choice.
- Hold BOOT for 3 seconds in either guest to return to the launcher.
- MeshCore: press **M** on home, choose a contact, type, **Enter** to queue a message.
  BLE Companion Radio remains available. Add contacts through the app.
- Both firmwares default to **US/915 MHz**. These defaults are not legal everywhere.
  Set the correct region and match the local network's frequency/modem settings.
- MeshCore's default pairing PIN is `123456`; change it in your own configuration.
- The two guests have separate Bluetooth identities on each device. The change
  preserves Meshtastic's local IRK while updating its address association; a phone
  may still need to forget the old pairing. Phone reconnection tests are pending.

## What this repository owns

The launcher, shared partition layout, guest integration patches and build tools.
It fetches full guest sources from the pinned revisions in `sources.json`:

- Meshtastic `eaba8b95bebab04c4f13a27b63e000ebdc1e5fac` plus patches in
  `meshtastic-overlay/` (return hook, Bluetooth separation, self-OTA guard).
- [`martinemde/MeshCore`, `cardputer-adv`](https://github.com/martinemde/MeshCore/tree/cardputer-adv)
  at `ecc79b6b6fdd2cde7860cbb2518010e170d04904`, containing the keyboard and
  Cardputer hardware support. DualMesh applies **only**
  `meshcore-cardputer/0004-cardputer-keyboard-dualmesh.patch` and shared files.
  Legacy MeshCore patches 0001–0003 and the old board overlay are not used.

The MeshCore integration supports only `M5Stack_Cardputer_ADV_companion_radio_ble`.
Do not use its other standalone environments with the DualMesh partition table.

## Build from source

With the tools above installed:

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/build.py --work-dir .build --output-dir dist
```

The build uses fresh pinned checkouts, applies all integration changes, compiles
three apps and the **1 MiB Meshtastic filesystem**, and packages `dist/`. It does
not access hardware. Use a new/empty work directory for each clean build.
GitHub Actions runs the same command; download `dualmesh-cardputer-adv-bin` from
a successful run. Build timestamps/tool metadata can change binary hashes; use
the reference release when exact reference app bytes matter.

PlatformIO packages are isolated per firmware to prevent Arduino/IDF versions
from replacing one another. The build includes the local SCons-package fix needed
by Meshtastic's pinned platform. No personal device settings are used.

## Flash layout

| Component | Offset | Capacity |
|---|---:|---:|
| MeshCore IDF4 bootloader | `0x0` | 32 KiB |
| Shared partition table | `0x8000` | 4 KiB |
| Launcher | `0x10000` | 1 MiB |
| Meshtastic | `0x110000` | 3.625 MiB |
| Meshtastic LittleFS | `0x4B0000` | 1 MiB |
| MeshCore | `0x5B0000` | 2 MiB |
| MeshCore storage | `0x7B0000` | 256 KiB |

See `partitions_dualmesh.csv` for NVS, boot selection and coredump regions. The
fresh-install image is a full 8 MiB with blank NVS/boot selection/MeshCore storage.
The older MeshCore bootloader is used for the mixed IDF versions; launcher and
guest-generated merged bundles must not be substituted.

## Verification and limits

The reference binaries were built successfully and their installation was checked
by flash readback. The launcher orientation and guest switching were observed.
The source reconstruction for both guests is checked against the local installed
build sources. This does **not** establish complete radio, GPS, keyboard/chat or
Bluetooth reconnection testing on every device. Installer tests use fake hardware;
preparing this repository does not flash or reboot the reference device.

Upstream Meshtastic, MeshCore and their dependencies retain their licenses. The
bundle includes source links and integration instructions in `SOURCES.md`.
