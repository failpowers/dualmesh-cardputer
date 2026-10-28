// DualMesh launcher — boot-picker for M5Stack Cardputer ADV.
//
// Boots first on every power-on (it lives in the "factory" partition, which the ROM/2nd-stage
// bootloader falls back to whenever otadata is blank or points at factory). Shows a two-item
// menu (Meshtastic / MeshCore), lets you pick with the single BOOT button (GPIO0), and jumps
// into the chosen slot via esp_ota_set_boot_partition(). See ../common/dualmesh_ota.h.
//
// Button gesture (matches the gesture Meshtastic's own stock Cardputer-ADV UI already uses for
// GPIO0, so it should feel familiar):
//   short press  -> cycle the highlighted option
//   hold ~1.2s   -> confirm and boot the highlighted option
//   no input     -> auto-boots your last choice after 5s (first ever boot defaults to Meshtastic)
//
// Any keypress on the TCA8418 keyboard just resets the auto-boot countdown, so you're never
// yanked into a boot mid-decision — it does not otherwise participate in navigation (see
// README.md for why: a robust semantic key-matrix mapping for the new TCA8418-based ADV
// keyboard wasn't available to verify against, so navigation deliberately relies only on the
// one hardware button whose behavior is already confirmed by Meshtastic's own upstream code).

#include <Arduino.h>
#include <SPI.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <Adafruit_TCA8418.h>

#include "board_pins_cardputer_adv.h"
#include "dualmesh_ota.h"

using dualmesh::Slot;

namespace {

struct MenuItem {
  Slot slot;
  const char *label;
  uint16_t accentColor;
};

const MenuItem kMenu[] = {
    {Slot::Meshtastic, "Meshtastic", 0x07E0}, // green
    {Slot::MeshCore, "MeshCore", 0x001F},     // blue
};
constexpr int kMenuCount = sizeof(kMenu) / sizeof(kMenu[0]);
constexpr uint32_t kLongPressMs = 1200;
constexpr uint32_t kAutoBootMs = 5000;

SPIClass tftSPI(FSPI);
Adafruit_ST7789 tft(&tftSPI, DM_TFT_CS, DM_TFT_DC, DM_TFT_RST);
Adafruit_TCA8418 keypad;
bool keypadOk = false;

int selected = 0;
uint32_t deadlineAt = 0;
bool buttonWasDown = false;
bool buttonReady = false;
uint32_t pressStartMs = 0;

void resetDeadline() { deadlineAt = millis() + kAutoBootMs; }

void drawMenu() {
  tft.fillScreen(ST77XX_BLACK);
  tft.setTextWrap(false);

  tft.setTextSize(2);
  tft.setTextColor(ST77XX_WHITE);
  tft.setCursor(6, 4);
  tft.print("DualMesh");

  tft.setTextSize(1);
  tft.setTextColor(0x7BEF); // gray
  tft.setCursor(6, 24);
  tft.print("Cardputer ADV boot picker");

  int y = 44;
  for (int i = 0; i < kMenuCount; i++) {
    bool isSel = (i == selected);
    uint16_t bg = isSel ? kMenu[i].accentColor : ST77XX_BLACK;
    uint16_t fg = isSel ? ST77XX_BLACK : ST77XX_WHITE;

    tft.fillRoundRect(6, y, DM_TFT_WIDTH - 12, 22, 4, bg);
    if (!isSel) tft.drawRoundRect(6, y, DM_TFT_WIDTH - 12, 22, 4, 0x39C7);

    tft.setTextSize(2);
    tft.setTextColor(fg);
    tft.setCursor(14, y + 4);
    tft.print(kMenu[i].label);

    y += 28;
  }

  uint32_t remainMs = (deadlineAt > millis()) ? (deadlineAt - millis()) : 0;
  tft.setTextSize(1);
  tft.setTextColor(0x7BEF);
  tft.setCursor(6, DM_TFT_HEIGHT - 20);
  tft.printf("BOOT: tap=next  hold=go");
  tft.setCursor(6, DM_TFT_HEIGHT - 10);
  tft.printf("auto-boot in %lus", (unsigned long)((remainMs + 999) / 1000));
}

void drawBooting(const char *name) {
  tft.fillScreen(ST77XX_BLACK);
  tft.setTextSize(2);
  tft.setTextColor(ST77XX_WHITE);
  tft.setCursor(10, DM_TFT_HEIGHT / 2 - 10);
  tft.print("Booting ");
  tft.print(name);
  tft.print("...");
}

void confirmAndBoot() {
  const MenuItem &item = kMenu[selected];
  drawBooting(item.label);
  Serial.printf("[launcher] booting slot %d (%s)\n", (int)item.slot, item.label);
  if (!dualmesh::bootInto(item.slot)) {
    tft.fillScreen(ST77XX_BLACK);
    tft.setTextSize(1);
    tft.setCursor(6, 55);
    tft.print("Boot failed. Check firmware image.");
    delay(2000);
    buttonReady = false;
    resetDeadline();
    drawMenu();
  }
}

} // namespace

void setup() {
  Serial.begin(115200);
  delay(150);
  Serial.println("\n[launcher] DualMesh boot picker starting");

  pinMode(DM_BUTTON_PIN, INPUT_PULLUP);

  pinMode(DM_TFT_BACKLIGHT, OUTPUT);
  digitalWrite(DM_TFT_BACKLIGHT, DM_TFT_BACKLIGHT_ON);

  tftSPI.begin(DM_TFT_SCK, DM_TFT_MISO, DM_TFT_MOSI, DM_TFT_CS);
  tft.init(DM_TFT_HEIGHT, DM_TFT_WIDTH); // panel is mounted landscape; see setRotation below
  tft.setRotation(3);
  tft.fillScreen(ST77XX_BLACK);

  Wire.begin(DM_I2C_SDA, DM_I2C_SCL);
  keypadOk = keypad.begin(DM_TCA8418_ADDR, &Wire);
  if (keypadOk) {
    keypad.matrix(8, 10); // conservative upper bound on the Cardputer ADV matrix; we only use
                           // "was any key pressed", not specific rows/cols, so exact size is
                           // not safety-critical here.
    keypad.flush();
  } else {
    Serial.println("[launcher] warning: TCA8418 keyboard not found, continuing without it "
                    "(BOOT button still works)");
  }

  // Pre-select whatever the user booted last time, defaulting to Meshtastic on a factory-fresh
  // device (dualmesh::lastSlot()'s own fallback argument).
  Slot last = dualmesh::lastSlot(Slot::Meshtastic);
  for (int i = 0; i < kMenuCount; i++) {
    if (kMenu[i].slot == last) { selected = i; break; }
  }

  resetDeadline();
  drawMenu();
}

void loop() {
  bool buttonDown = digitalRead(DM_BUTTON_PIN) == LOW;
  uint32_t now = millis();

  // A guest returns while BOOT is still held. Require release before selecting.
  if (!buttonReady) {
    resetDeadline();
    if (!buttonDown) buttonReady = true;
    delay(10);
    return;
  }

  if (buttonDown && !buttonWasDown) {
    pressStartMs = now;
  }
  if (buttonDown) {
    if (now - pressStartMs >= kLongPressMs) {
      confirmAndBoot(); // does not return
    }
  } else if (buttonWasDown) {
    // rising edge: a short press happened
    uint32_t heldFor = now - pressStartMs;
    if (heldFor < kLongPressMs) {
      selected = (selected + 1) % kMenuCount;
      resetDeadline();
      drawMenu();
    }
  }
  buttonWasDown = buttonDown;

  if (keypadOk && keypad.available()) {
    keypad.getEvent(); // don't decode; any activity just means "still deciding"
    resetDeadline();
  }

  if (!buttonDown && now >= deadlineAt) {
    confirmAndBoot(); // does not return
  }

  // Cheap 1x/sec countdown redraw without fighting the button-driven redraws above.
  static uint32_t lastTick = 0;
  if (now - lastTick >= 1000) {
    lastTick = now;
    if (!buttonDown) drawMenu();
  }

  delay(10);
}
