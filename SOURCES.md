# Corresponding source for the reference binaries

This release contains build outputs only; no configured-device flash dump.

- Meshtastic upstream: https://github.com/meshtastic/firmware/tree/eaba8b95bebab04c4f13a27b63e000ebdc1e5fac
  (GPL-3.0; submodules at the revisions recorded there).
- MeshCore keyboard/hardware base: https://github.com/martinemde/MeshCore/tree/ecc79b6b6fdd2cde7860cbb2518010e170d04904
  (MIT). Apply only DualMesh `meshcore-cardputer/0004-cardputer-keyboard-dualmesh.patch`
  and copy the shared header/partition table as `scripts/build.py` does.
- All DualMesh source, configuration and integration patches: the Git revision
  recorded in this bundle's manifest, available from
  https://github.com/martinemde/dualmesh-cardputer .
- Meshtastic patches: `meshtastic-overlay/0001-add-return-to-launcher-hook.patch`,
  `0002-separate-bluetooth-identity.patch`, `0003-disable-meshtastic-ota.patch`
  (see actual filenames in the source archive/repository).

The source archives accompanying this release retain upstream files and license
notices and include the modified guest source trees. Upstream licenses and those
of libraries, framework packages and submodules continue to apply. Meshtastic's
filesystem here is the original nearly-empty LittleFS image containing only the
static/.gitkeep placeholder, not a populated web client or a user's filesystem.

Use `scripts/build.py` with `requirements-build.txt` to rebuild. It installs
frameworks, compiler tools and libraries through PlatformIO; the checked-in
configuration pins the board/platform dependencies used for this build where
available. Build metadata and host tool differences can change binary hashes;
`manifest.json` identifies the exact reference binary bytes.
