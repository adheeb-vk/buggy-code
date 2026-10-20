/*
  Temporary ESP32-S3 USB CDC diagnostic.

  This sketch does not read sensors or control any relay. Flash it only to
  verify that the Pi can receive newline-delimited JSON over native USB CDC.

  Arduino IDE settings for ESP32-S3:
    Tools -> USB CDC On Boot -> Enabled
    Tools -> Upload Mode -> UART0 / Hardware CDC
*/

// The Pi reads Serial through the ESP32-S3 Hardware CDC interface.
#if !defined(ARDUINO_USB_CDC_ON_BOOT) || ARDUINO_USB_CDC_ON_BOOT != 1
#error "Set Tools -> USB CDC On Boot -> Enabled before compiling."
#endif
#if !defined(ARDUINO_USB_MODE) || ARDUINO_USB_MODE != 1
#error "Set Tools -> USB Mode -> Hardware CDC and JTAG before compiling."
#endif

const unsigned long TELEMETRY_INTERVAL_MS = 500;
unsigned long lastTelemetryAt = 0;
unsigned long sequenceNumber = 0;

void setup() {
  Serial.begin(115200);
}

void loop() {
  unsigned long now = millis();
  if (now - lastTelemetryAt < TELEMETRY_INTERVAL_MS) {
    return;
  }
  lastTelemetryAt = now;
  sequenceNumber++;

  // Uses the same newline-delimited JSON framing as the complete firmware.
  Serial.printf(
    "{\"seatbelt\":0,\"alcohol\":0,\"water\":0,\"current\":0,"
    "\"imu\":0,\"diagnostic\":\"usb_cdc\",\"sequence\":%lu}\n",
    sequenceNumber
  );
}
