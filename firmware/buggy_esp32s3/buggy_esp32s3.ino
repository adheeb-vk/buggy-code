/*
  Buggy ESP32-S3 sensor and relay firmware.

  Purpose
  -------
  - Sends newline-delimited JSON telemetry to the Raspberry Pi at 5 Hz.
  - Accepts feature-scoped relay commands from the Pi.
  - Runs safely with no sensor or relay connected.

  Arduino IDE setup for Pi USB serial
  -----------------------------------
  Board: ESP32S3 Dev Module (or your exact ESP32-S3 board)
  Tools -> USB CDC On Boot: Enabled
  Tools -> USB Mode: Hardware CDC and JTAG
  Tools -> Upload Mode: UART0 / Hardware CDC

  Dependency: ArduinoJson 6.x, installed using Library Manager.
*/

#include <ArduinoJson.h>
#include <Wire.h>

#include "board_config.h"

constexpr char FIRMWARE_VERSION[] = "buggy-esp32s3-1";
constexpr size_t COMMAND_BUFFER_LIMIT = 256;

struct RelayDefinition {
  const char* feature;
  int pin;
};

RelayDefinition RELAYS[] = {
  {"drowsiness", PIN_RELAY_DROWSINESS},
  {"pothole", PIN_RELAY_POTHOLE},
  {"alcohol", PIN_RELAY_ALCOHOL},
  {"geofence", PIN_RELAY_GEOFENCE},
};
constexpr size_t RELAY_COUNT = sizeof(RELAYS) / sizeof(RELAYS[0]);

bool imuAvailable = false;
long accelX = 0;
long accelY = 0;
long accelZ = 0;
unsigned long lastTelemetryAt = 0;
String commandBuffer;

void sendJson(const JsonDocument& document) {
  serializeJson(document, Serial);
  Serial.println();
}

void sendStatus(const char* status) {
  StaticJsonDocument<192> document;
  document["type"] = "status";
  document["status"] = status;
  document["firmware"] = FIRMWARE_VERSION;
  document["imu"] = imuAvailable ? 1 : 0;
  sendJson(document);
}

void initialiseRelayPins() {
  for (size_t index = 0; index < RELAY_COUNT; index++) {
    if (RELAYS[index].pin < 0) {
      continue;
    }
    pinMode(RELAYS[index].pin, OUTPUT);
    digitalWrite(RELAYS[index].pin, RELAY_ON_LEVEL == HIGH ? LOW : HIGH);
  }
}

void initialiseSensors() {
  if (PIN_ALCOHOL >= 0) {
    // The MQ-3 module is powered at 5 V. Its AO signal must first be reduced
    // below 3.3 V by the external divider described in board_config.h.
    analogReadResolution(12);
    analogSetPinAttenuation(PIN_ALCOHOL, ADC_11db);
  }

  if (PIN_SEATBELT >= 0) {
    pinMode(PIN_SEATBELT, INPUT_PULLUP);
  }

  if (PIN_I2C_SDA < 0 || PIN_I2C_SCL < 0) {
    return;
  }
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
  Wire.setClock(400000);
  Wire.beginTransmission(MPU6050_ADDRESS);
  if (Wire.endTransmission() != 0) {
    return;
  }
  Wire.beginTransmission(MPU6050_ADDRESS);
  Wire.write(0x6B);  // MPU6050 power-management register.
  Wire.write(0);     // Wake the device.
  imuAvailable = (Wire.endTransmission(true) == 0);
}

bool readImu() {
  if (!imuAvailable) {
    return false;
  }
  Wire.beginTransmission(MPU6050_ADDRESS);
  Wire.write(0x3B);  // First accelerometer register.
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom((int)MPU6050_ADDRESS, 6, true) != 6) {
    imuAvailable = false;
    return false;
  }
  accelX = (int16_t)((Wire.read() << 8) | Wire.read());
  accelY = (int16_t)((Wire.read() << 8) | Wire.read());
  accelZ = (int16_t)((Wire.read() << 8) | Wire.read());
  return true;
}

void addConfiguredSensors(JsonDocument& document) {
  if (PIN_SEATBELT >= 0) {
    document["seatbelt"] = digitalRead(PIN_SEATBELT) == LOW ? 1 : 0;
  }
  if (PIN_ALCOHOL >= 0) {
    document["alcohol"] = analogRead(PIN_ALCOHOL);
  }
  if (PIN_WATER >= 0) {
    document["water"] = analogRead(PIN_WATER);
  }
  if (PIN_CURRENT >= 0) {
    document["current"] = analogRead(PIN_CURRENT);
  }
  if (readImu()) {
    JsonObject accel = document.createNestedObject("accel");
    accel["x"] = accelX;
    accel["y"] = accelY;
    accel["z"] = accelZ;
  }
}

void sendTelemetryIfDue() {
  unsigned long now = millis();
  if (now - lastTelemetryAt < TELEMETRY_INTERVAL_MS) {
    return;
  }
  lastTelemetryAt = now;

  StaticJsonDocument<384> document;
  document["type"] = "telemetry";
  document["firmware"] = FIRMWARE_VERSION;
  document["uptime_ms"] = now;
  document["imu"] = imuAvailable ? 1 : 0;
  addConfiguredSensors(document);
  sendJson(document);
}

RelayDefinition* findRelay(const char* feature) {
  if (feature == nullptr) {
    return nullptr;
  }
  for (size_t index = 0; index < RELAY_COUNT; index++) {
    if (strcmp(feature, RELAYS[index].feature) == 0) {
      return &RELAYS[index];
    }
  }
  return nullptr;
}

void sendRelayAcknowledgement(const char* feature, int state, bool ok, const char* reason) {
  StaticJsonDocument<192> document;
  document["type"] = "ack";
  document["cmd"] = "relay";
  document["feature"] = feature == nullptr ? "" : feature;
  document["state"] = state;
  document["ok"] = ok;
  if (reason != nullptr) {
    document["reason"] = reason;
  }
  sendJson(document);
}

void processCommand(const String& line) {
  StaticJsonDocument<256> command;
  DeserializationError error = deserializeJson(command, line);
  if (error) {
    sendRelayAcknowledgement(nullptr, -1, false, "bad_json");
    return;
  }
  const char* commandName = command["cmd"];
  const char* feature = command["feature"];
  int state = command["state"] | -1;
  if (commandName == nullptr || strcmp(commandName, "relay") != 0) {
    sendRelayAcknowledgement(feature, state, false, "unknown_command");
    return;
  }
  if (state != 0 && state != 1) {
    sendRelayAcknowledgement(feature, state, false, "bad_state");
    return;
  }
  RelayDefinition* relay = findRelay(feature);
  if (relay == nullptr) {
    sendRelayAcknowledgement(feature, state, false, "unknown_feature");
    return;
  }
  if (relay->pin < 0) {
    sendRelayAcknowledgement(feature, state, false, "relay_unconfigured");
    return;
  }
  bool active = state == 1;
  uint8_t level = active ? RELAY_ON_LEVEL : (RELAY_ON_LEVEL == HIGH ? LOW : HIGH);
  digitalWrite(relay->pin, level);
  sendRelayAcknowledgement(feature, state, true, nullptr);
}

void readCommands() {
  while (Serial.available() > 0) {
    char character = (char)Serial.read();
    if (character == '\n') {
      processCommand(commandBuffer);
      commandBuffer = "";
    } else if (character != '\r') {
      commandBuffer += character;
      if (commandBuffer.length() > COMMAND_BUFFER_LIMIT) {
        commandBuffer = "";
        sendRelayAcknowledgement(nullptr, -1, false, "command_too_long");
      }
    }
  }
}

void setup() {
  Serial.begin(SERIAL_BAUD_RATE);
  initialiseRelayPins();
  initialiseSensors();
  sendStatus("ready");
}

void loop() {
  readCommands();
  sendTelemetryIfDue();
}
