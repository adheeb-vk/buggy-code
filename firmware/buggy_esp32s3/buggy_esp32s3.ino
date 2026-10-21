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
uint8_t imuAddress = 0;
uint8_t imuWhoAmI = 0;
uint8_t i2cDevices[8];
uint8_t i2cDeviceCount = 0;
float ultrasonicFrontLeftCm = -1;
float ultrasonicFrontRightCm = -1;
float ultrasonicRearCm = -1;
uint8_t ultrasonicScanIndex = 0;
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

  // Optional parking sensors stay completely untouched while their pin
  // constants are -1. Configure only complete trigger/echo pairs.
  const int ultrasonicPins[][2] = {
    {PIN_US_FRONT_LEFT_TRIG, PIN_US_FRONT_LEFT_ECHO},
    {PIN_US_FRONT_RIGHT_TRIG, PIN_US_FRONT_RIGHT_ECHO},
    {PIN_US_REAR_TRIG, PIN_US_REAR_ECHO},
  };
  for (const auto& pins : ultrasonicPins) {
    if (pins[0] >= 0 && pins[1] >= 0) {
      pinMode(pins[0], OUTPUT);
      digitalWrite(pins[0], LOW);
      pinMode(pins[1], INPUT);
    }
  }

  if (PIN_I2C_SDA < 0 || PIN_I2C_SCL < 0) {
    return;
  }
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
  Wire.setClock(400000);
  for (uint8_t address = 1; address < 127; address++) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() == 0 && i2cDeviceCount < sizeof(i2cDevices)) {
      i2cDevices[i2cDeviceCount++] = address;
    }
  }
  // MPU-9250 / MPU-6500 boards use 0x68 with AD0 low and 0x69 with AD0 high.
  // Scan both so an AD0 jumper or module pull-up cannot make the sensor vanish.
  const uint8_t addresses[] = {0x68, 0x69};
  for (uint8_t address : addresses) {
    Wire.beginTransmission(address);
    if (Wire.endTransmission() == 0) {
      imuAddress = address;
      break;
    }
  }
  if (imuAddress == 0) {
    return;
  }
  Wire.beginTransmission(imuAddress);
  Wire.write(0x6B);  // MPU6050 power-management register.
  Wire.write(0);     // Wake the device.
  if (Wire.endTransmission(true) != 0) {
    imuAddress = 0;
    return;
  }
  Wire.beginTransmission(imuAddress);
  Wire.write(0x75);  // WHO_AM_I register.
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom((int)imuAddress, 1, true) != 1) {
    imuAddress = 0;
    return;
  }
  imuWhoAmI = Wire.read();
  imuAvailable = true;
}

bool readImu() {
  if (!imuAvailable) {
    return false;
  }
  Wire.beginTransmission(imuAddress);
  Wire.write(0x3B);  // First accelerometer register.
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom((int)imuAddress, 6, true) != 6) {
    imuAvailable = false;
    return false;
  }
  accelX = (int16_t)((Wire.read() << 8) | Wire.read());
  accelY = (int16_t)((Wire.read() << 8) | Wire.read());
  accelZ = (int16_t)((Wire.read() << 8) | Wire.read());
  return true;
}

float readUltrasonicCm(int triggerPin, int echoPin) {
  if (triggerPin < 0 || echoPin < 0) {
    return -1;
  }
  digitalWrite(triggerPin, LOW);
  delayMicroseconds(2);
  digitalWrite(triggerPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(triggerPin, LOW);
  unsigned long echoUs = pulseIn(echoPin, HIGH, 18000UL);
  return echoUs == 0 ? -1 : (float)echoUs / 58.0f;
}

void scanOneUltrasonicSensor() {
  switch (ultrasonicScanIndex) {
    case 0:
      ultrasonicFrontLeftCm = readUltrasonicCm(PIN_US_FRONT_LEFT_TRIG, PIN_US_FRONT_LEFT_ECHO);
      break;
    case 1:
      ultrasonicFrontRightCm = readUltrasonicCm(PIN_US_FRONT_RIGHT_TRIG, PIN_US_FRONT_RIGHT_ECHO);
      break;
    default:
      ultrasonicRearCm = readUltrasonicCm(PIN_US_REAR_TRIG, PIN_US_REAR_ECHO);
      break;
  }
  ultrasonicScanIndex = (ultrasonicScanIndex + 1) % 3;
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
  if (PIN_US_FRONT_LEFT_TRIG >= 0 && PIN_US_FRONT_LEFT_ECHO >= 0) {
    if (ultrasonicFrontLeftCm < 0) document["ultrasonic_front_left_cm"] = nullptr;
    else document["ultrasonic_front_left_cm"] = ultrasonicFrontLeftCm;
  }
  if (PIN_US_FRONT_RIGHT_TRIG >= 0 && PIN_US_FRONT_RIGHT_ECHO >= 0) {
    if (ultrasonicFrontRightCm < 0) document["ultrasonic_front_right_cm"] = nullptr;
    else document["ultrasonic_front_right_cm"] = ultrasonicFrontRightCm;
  }
  if (PIN_US_REAR_TRIG >= 0 && PIN_US_REAR_ECHO >= 0) {
    if (ultrasonicRearCm < 0) document["ultrasonic_rear_cm"] = nullptr;
    else document["ultrasonic_rear_cm"] = ultrasonicRearCm;
  }
  if (PIN_I2C_SDA >= 0 && PIN_I2C_SCL >= 0) {
    JsonArray devices = document.createNestedArray("i2c_devices");
    for (uint8_t index = 0; index < i2cDeviceCount; index++) {
      devices.add(i2cDevices[index]);
    }
  }
  if (readImu()) {
    JsonObject accel = document.createNestedObject("accel");
    accel["x"] = accelX;
    accel["y"] = accelY;
    accel["z"] = accelZ;
    document["imu_address"] = imuAddress;
    document["imu_who_am_i"] = imuWhoAmI;
  }
}

void sendTelemetryIfDue() {
  unsigned long now = millis();
  if (now - lastTelemetryAt < TELEMETRY_INTERVAL_MS) {
    return;
  }
  lastTelemetryAt = now;
  scanOneUltrasonicSensor();

  StaticJsonDocument<512> document;
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
