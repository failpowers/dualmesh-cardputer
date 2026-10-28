// dualmesh_ota.h
//
// Tiny shared helper for jumping between the three app slots defined in
// ../partitions_dualmesh.csv: "factory" (the launcher), "ota_0" (Meshtastic),
// "ota_1" (MeshCore). Both the launcher and the return-to-launcher hook
// (dropped into Meshtastic and MeshCore, see meshtastic-overlay/ and
// meshcore-cardputer/) include this file — it has no dependencies beyond
// esp_ota_ops / esp_partition, so it compiles cleanly inside any of the
// three separate PlatformIO projects without pulling in the other two.
#pragma once

// This header is force-included (via -include / -I + #include "dualmesh_ota.h")
// into every translation unit of the guest firmware, including any plain-C
// compilation units the build system generates (e.g. arduino-lib-builder's
// dummy .c placeholder object). Preferences.h and the class/namespace code
// below are C++-only, so guard everything behind __cplusplus -- when this
// header is dragged into a .c file, it silently becomes a no-op instead of a
// syntax error.
#ifdef __cplusplus

#include <Arduino.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <Preferences.h>

namespace dualmesh {

enum class Slot : uint8_t {
  Launcher = 0,   // "factory"
  Meshtastic = 1, // "ota_0"
  MeshCore = 2,   // "ota_1"
};

// Namespace/key used to remember the user's last choice across power cycles.
// All three projects share this NVS namespace (distinct from each project's
// own settings namespace, so there's no collision).
inline Preferences &dm_prefs() {
  static Preferences p;
  return p;
}

inline bool findSlotPartition(Slot slot, const esp_partition_t **out) {
  esp_partition_subtype_t subtype;
  esp_partition_type_t type = ESP_PARTITION_TYPE_APP;
  switch (slot) {
    case Slot::Launcher:   subtype = ESP_PARTITION_SUBTYPE_APP_FACTORY; break;
    case Slot::Meshtastic: subtype = ESP_PARTITION_SUBTYPE_APP_OTA_0;   break;
    case Slot::MeshCore:   subtype = ESP_PARTITION_SUBTYPE_APP_OTA_1;   break;
    default: return false;
  }
  *out = esp_partition_find_first(type, subtype, nullptr);
  return *out != nullptr;
}

// Persist which guest app the user last booted into (read by the launcher to
// pick a sane default / to auto-continue after its short splash timeout).
inline void rememberLastSlot(Slot slot) {
  dm_prefs().begin("dualmesh", false);
  dm_prefs().putUChar("lastslot", static_cast<uint8_t>(slot));
  dm_prefs().end();
}

inline Slot lastSlot(Slot fallback = Slot::Meshtastic) {
  dm_prefs().begin("dualmesh", true);
  uint8_t v = dm_prefs().getUChar("lastslot", static_cast<uint8_t>(fallback));
  dm_prefs().end();
  if (v > 2) v = static_cast<uint8_t>(fallback);
  return static_cast<Slot>(v);
}

// Point the bootloader at `slot` and restart. Return false without rebooting on failure.
inline bool bootInto(Slot slot) {
  const esp_partition_t *part = nullptr;
  if (!findSlotPartition(slot, &part)) {
    Serial.printf("[dualmesh] slot %d partition not found\n", static_cast<int>(slot));
    return false;
  }
  esp_err_t err = esp_ota_set_boot_partition(part);
  if (err != ESP_OK) {
    Serial.printf("[dualmesh] cannot select slot %d: %d\n", static_cast<int>(slot), (int)err);
    return false;
  }
  if (slot != Slot::Launcher) rememberLastSlot(slot);
  Serial.flush();
  delay(50);
  esp_restart();
  return true;
}

// ---------------------------------------------------------------------------
// Return-to-launcher hook for guest firmware (Meshtastic / MeshCore).
//
// Usage — exactly ONE line added near the top of the guest firmware's existing
// loop() function (see meshtastic-overlay/ and meshcore-cardputer's patches
// for the two real, already-located call sites):
//   #include "dualmesh_ota.h"
//   ...
//   void loop() { dualmesh::ReturnHook::poll(); ...existing body... }
//
// poll() lazily does its own pinMode() on first call, so there is nothing
// else to wire up. Hold the BOOT button (GPIO0) for 3 seconds at any time
// while the guest firmware is running to jump back to the launcher's app
// picker.
// ---------------------------------------------------------------------------
class ReturnHook {
public:
  static void poll() {
    static bool began_ = false;
    static uint32_t holdStart_ = 0;
    if (!began_) {
      pinMode(BUTTON_GPIO, INPUT_PULLUP);
      began_ = true;
    }
    bool pressed = digitalRead(BUTTON_GPIO) == LOW;
    uint32_t now = millis();
    if (pressed) {
      if (holdStart_ == 0) holdStart_ = now;
      else if (now - holdStart_ >= HOLD_MS) {
        Serial.println("[dualmesh] BOOT held 3s -> returning to launcher");
        bootInto(Slot::Launcher);
        holdStart_ = now; // Avoid flooding errors if the launcher image is invalid.
      }
    } else {
      holdStart_ = 0;
    }
  }

private:
  static constexpr int BUTTON_GPIO = 0;
  static constexpr uint32_t HOLD_MS = 3000;
};

} // namespace dualmesh

#endif // __cplusplus
