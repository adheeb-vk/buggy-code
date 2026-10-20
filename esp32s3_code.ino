/*
  ESP32-S3 firmware — buggy sensor/relay layer
  ---------------------------------------------
  Two jobs, running side by side:

  1. SAFETY LAYER (unchanged in spirit from the earlier Arduino build):
     seatbelt+alcohol interlock, short-circuit protection, crash latch,
     hands-on steering, unresponsive-driver sequence. All of this keeps
     working even if the Pi is disconnected, slow, or rebooting.

  2. PI LINK: streams sensor telemetry as JSON over the native USB serial
     link (~5 Hz), and listens for relay commands from the Pi for
     Features 1 (drowsiness), 2 (pothole), 3 (air_quality), 6 (geofence).

  Board setting required: Tools -> USB CDC On Boot -> Enabled
  Library required: ArduinoJson (v6.x), install via Library Manager.

  See ESP32S3_Wiring_Guide.md for the full pinout and wiring notes.
*/

#include <Wire.h>
#include <ArduinoJson.h>

// ---------- Pin map (see ESP32S3_Wiring_Guide.md) ----------
const int PIN_ALCOHOL       = 1;   // ADC1_CH0
const int PIN_WATER         = 2;   // ADC1_CH1
const int PIN_CURRENT       = 4;   // ADC1_CH3
const int PIN_AQI           = 5;   // ADC1_CH4

const int PIN_SEATBELT      = 11;  // INPUT_PULLUP, LOW = fastened
const int PIN_STEER_LEFT    = 12;  // INPUT_PULLUP
const int PIN_STEER_RIGHT   = 13;  // INPUT_PULLUP
const int PIN_RESPONSE_BTN  = 14;  // INPUT_PULLUP
const int PIN_FAULT_RESET   = 15;  // INPUT_PULLUP

const int PIN_SDA           = 16;  // MPU6050
const int PIN_SCL           = 17;

const int PIN_MASTER_MOSFET = 18;  // LEDC PWM, master drive power
const int PIN_HORN_RELAY    = 21;  // Safety layer
const int PIN_HAZARD_RELAY  = 38;  // Safety layer
const int PIN_STATUS_LED    = 39;
const int PIN_BUZZER        = 43;

const int PIN_RELAY_DROWSY  = 40;  // Feature 1 - commanded by the Pi
const int PIN_RELAY_POTHOLE = 41;  // Feature 2 - commanded by the Pi
const int PIN_RELAY_AQI     = 42;  // Feature 3 - commanded by the Pi
const int PIN_RELAY_GEOFENCE = 47; // Feature 6 - commanded by the Pi (verify free on your board)

// ---------- LEDC (PWM) config for the master MOSFET ----------
const int MOSFET_PWM_FREQ = 5000;
const int MOSFET_PWM_RES  = 8;    // 0-255 duty

// ---------- Feature-relay lookup table ----------
struct FeatureRelay {
  const char* name;
  int pin;
};
FeatureRelay relayMap[] = {
  { "drowsiness",  PIN_RELAY_DROWSY   },
  { "pothole",     PIN_RELAY_POTHOLE  },
  { "air_quality", PIN_RELAY_AQI      },
  { "geofence",    PIN_RELAY_GEOFENCE },
};
const int RELAY_MAP_SIZE = sizeof(relayMap) / sizeof(relayMap[0]);

// ---------- Thresholds (tune on the real buggy) ----------
const int ALCOHOL_LIMIT      = 400;
const int WATER_LIMIT        = 300;
const int CURRENT_LIMIT      = 700;
const long CRASH_DELTA       = 6000;
const unsigned long HANDS_OFF_LIMIT  = 1500;
const unsigned long ALERT_WINDOW     = 8000;
const unsigned long RESPONSE_TIMEOUT = 3000;
const unsigned long TELEMETRY_INTERVAL = 200;  // ms, ~5 Hz

// ---------- Safety-layer state ----------
bool systemFault = false;
bool crashLatched = false;
bool unresponsiveLatched = false;
bool handsOffTimerRunning = false;
bool handsOffTripped = false;
unsigned long handsOffSince = 0;
unsigned long lastAlertCheck = 0;
bool waitingForResponse = false;
unsigned long responseAskedAt = 0;
long baselineAccelMag = 16384;
long lastAccelX = 0, lastAccelY = 0, lastAccelZ = 0;
bool imuAvailable = false;

unsigned long lastTelemetryAt = 0;
String serialBuffer = "";

void setup() {
  Serial.begin(115200);
  // Do not wait for a host to open USB CDC. Opening /dev/ttyACM0 can reset the
  // ESP32-S3, and a startup wait makes the Pi's first telemetry read time out.

  Wire.begin(PIN_SDA, PIN_SCL);

  pinMode(PIN_SEATBELT, INPUT_PULLUP);
  pinMode(PIN_STEER_LEFT, INPUT_PULLUP);
  pinMode(PIN_STEER_RIGHT, INPUT_PULLUP);
  pinMode(PIN_RESPONSE_BTN, INPUT_PULLUP);
  pinMode(PIN_FAULT_RESET, INPUT_PULLUP);

  pinMode(PIN_HORN_RELAY, OUTPUT);
  pinMode(PIN_HAZARD_RELAY, OUTPUT);
  pinMode(PIN_STATUS_LED, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  for (int i = 0; i < RELAY_MAP_SIZE; i++) {
    pinMode(relayMap[i].pin, OUTPUT);
    digitalWrite(relayMap[i].pin, LOW);
  }

  ledcAttach(PIN_MASTER_MOSFET, MOSFET_PWM_FREQ, MOSFET_PWM_RES);
  // If your ESP32 Arduino core is older than 3.x and ledcAttach() doesn't
  // compile, replace the two PWM lines with the classic channel-based API:
  //   ledcSetup(0, MOSFET_PWM_FREQ, MOSFET_PWM_RES);
  //   ledcAttachPin(PIN_MASTER_MOSFET, 0);
  // and change ledcWrite(PIN_MASTER_MOSFET, ...) to ledcWrite(0, ...) below.

  setupMPU();
  baselineAccelMag = readAccelMagnitude();  // buggy must be still and level here

  setMasterPower(0);   // locked out until interlocks are satisfied
  digitalWrite(PIN_STATUS_LED, HIGH);

  Serial.println("{\"status\":\"ready\"}");
}

void loop() {
  handleIncomingCommands();
  checkFaultReset();

  if (systemFault || crashLatched || unresponsiveLatched) {
    setMasterPower(0);
    handleLatchedFault();
    sendTelemetryIfDue();
    return;
  }

  checkShortCircuit();
  checkWaterAlarm();
  checkCrash();

  if (systemFault || crashLatched) {
    sendTelemetryIfDue();
    return;
  }

  if (checkInterlocks()) {
    if (checkHandsOn() && checkResponsiveness()) {
      setMasterPower(100);
    }
  } else {
    setMasterPower(0);
    handsOffTimerRunning = false;
    handsOffTripped = false;
  }

  sendTelemetryIfDue();
  delay(20);
}

// ================== Safety layer ==================

bool checkInterlocks() {
  bool seatbeltFastened = (digitalRead(PIN_SEATBELT) == LOW);
  bool alcoholClear = (analogRead(PIN_ALCOHOL) < ALCOHOL_LIMIT);
  return seatbeltFastened && alcoholClear;
}

void checkShortCircuit() {
  if (analogRead(PIN_CURRENT) > CURRENT_LIMIT) {
    systemFault = true;
    setMasterPower(0);
  }
}

void handleLatchedFault() {
  digitalWrite(PIN_HAZARD_RELAY, (millis() / 300) % 2);
  digitalWrite(PIN_BUZZER, (millis() / 300) % 2);
}

void checkFaultReset() {
  if (digitalRead(PIN_FAULT_RESET) == LOW && analogRead(PIN_CURRENT) < CURRENT_LIMIT) {
    systemFault = false;
    crashLatched = false;
    unresponsiveLatched = false;
    digitalWrite(PIN_HAZARD_RELAY, LOW);
    digitalWrite(PIN_HORN_RELAY, LOW);
    digitalWrite(PIN_BUZZER, LOW);
  }
}

void checkWaterAlarm() {
  if (analogRead(PIN_WATER) > WATER_LIMIT) {
    digitalWrite(PIN_BUZZER, (millis() / 250) % 2);
  }
}

void setupMPU() {
  Wire.beginTransmission(0x68);
  Wire.write(0x6B);   // power management register
  Wire.write(0);      // wake the sensor up
  imuAvailable = (Wire.endTransmission(true) == 0);
}

long readAccelMagnitude() {
  if (!imuAvailable) {
    lastAccelX = 0;
    lastAccelY = 0;
    lastAccelZ = 0;
    return 0;
  }
  Wire.beginTransmission(0x68);
  Wire.write(0x3B);
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom((int)0x68, 6, true) != 6) {
    imuAvailable = false;
    lastAccelX = 0;
    lastAccelY = 0;
    lastAccelZ = 0;
    return 0;
  }
  int16_t ax = Wire.read() << 8 | Wire.read();
  int16_t ay = Wire.read() << 8 | Wire.read();
  int16_t az = Wire.read() << 8 | Wire.read();
  lastAccelX = ax; lastAccelY = ay; lastAccelZ = az;
  return sqrt((long)ax * ax + (long)ay * ay + (long)az * az);
}

void checkCrash() {
  long mag = readAccelMagnitude();
  if (abs(mag - baselineAccelMag) > CRASH_DELTA) {
    handleCrash();
  }
}

void handleCrash() {
  rampDownAndStop();
  digitalWrite(PIN_HAZARD_RELAY, HIGH);
  digitalWrite(PIN_HORN_RELAY, HIGH);
  crashLatched = true;   // reported to the Pi via telemetry; Pi decides on the SOS text
}

bool checkHandsOn() {
  bool leftHand = (digitalRead(PIN_STEER_LEFT) == LOW);
  bool rightHand = (digitalRead(PIN_STEER_RIGHT) == LOW);

  if (!leftHand && !rightHand) {
    if (!handsOffTimerRunning) {
      handsOffTimerRunning = true;
      handsOffSince = millis();
    } else if (!handsOffTripped && millis() - handsOffSince > HANDS_OFF_LIMIT) {
      rampDownAndStop();
      handsOffTripped = true;
    }
    if (handsOffTripped) return false;
  } else {
    handsOffTimerRunning = false;
    handsOffTripped = false;
  }
  return true;
}

bool checkResponsiveness() {
  unsigned long now = millis();

  if (!waitingForResponse && now - lastAlertCheck > ALERT_WINDOW) {
    waitingForResponse = true;
    responseAskedAt = now;
  }

  if (!waitingForResponse) return true;

  digitalWrite(PIN_STATUS_LED, (millis() / 150) % 2);

  if (digitalRead(PIN_RESPONSE_BTN) == LOW) {
    waitingForResponse = false;
    lastAlertCheck = now;
    digitalWrite(PIN_STATUS_LED, HIGH);
    return true;
  }

  if (now - responseAskedAt > RESPONSE_TIMEOUT) {
    unresponsiveDriverSequence();
    return false;
  }

  return true;
}

void unresponsiveDriverSequence() {
  rampDownAndStop();
  digitalWrite(PIN_HAZARD_RELAY, HIGH);
  digitalWrite(PIN_HORN_RELAY, HIGH);
  delay(3000);   // wait exactly 3 seconds after the halt, as before
  unresponsiveLatched = true;   // reported to the Pi via telemetry; Pi fires the SOS text
}

void setMasterPower(int dutyPercent) {
  int pwmVal = map(dutyPercent, 0, 100, 0, 255);
  ledcWrite(PIN_MASTER_MOSFET, pwmVal);
  // Older-core fallback: ledcWrite(0, pwmVal);
}

void rampDownAndStop() {
  for (int duty = 100; duty >= 0; duty -= 10) {
    setMasterPower(duty);
    delay(150);
  }
  setMasterPower(0);
}

// ================== Pi link ==================

void sendTelemetryIfDue() {
  unsigned long now = millis();
  if (now - lastTelemetryAt < TELEMETRY_INTERVAL) return;
  lastTelemetryAt = now;

  StaticJsonDocument<384> doc;
  doc["seatbelt"] = (digitalRead(PIN_SEATBELT) == LOW) ? 1 : 0;
  doc["alcohol"] = analogRead(PIN_ALCOHOL);
  doc["water"] = analogRead(PIN_WATER);
  doc["current"] = analogRead(PIN_CURRENT);
  doc["aqi"] = analogRead(PIN_AQI);
  doc["handsOn"] = (!handsOffTripped) ? 1 : 0;

  doc["imu"] = imuAvailable ? 1 : 0;
  // Omit accel entirely until the MPU6050 responds. This prevents the Pi from
  // interpreting made-up zero readings as a pothole spike.
  if (imuAvailable) {
    JsonObject accel = doc.createNestedObject("accel");
    accel["x"] = lastAccelX;
    accel["y"] = lastAccelY;
    accel["z"] = lastAccelZ;
  }

  doc["fault"] = systemFault ? 1 : 0;
  doc["crash"] = crashLatched ? 1 : 0;
  doc["unresponsive"] = unresponsiveLatched ? 1 : 0;

  serializeJson(doc, Serial);
  Serial.println();
}

void handleIncomingCommands() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      processCommandLine(serialBuffer);
      serialBuffer = "";
    } else if (c != '\r') {
      serialBuffer += c;
      if (serialBuffer.length() > 300) serialBuffer = "";  // guard against a runaway line
    }
  }
}

void processCommandLine(const String& line) {
  if (line.length() == 0) return;

  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err) {
    Serial.println("{\"error\":\"bad_json\"}");
    return;
  }

  const char* cmd = doc["cmd"];
  if (cmd == nullptr || String(cmd) != "relay") {
    Serial.println("{\"error\":\"unknown_cmd\"}");
    return;
  }

  const char* feature = doc["feature"];
  int state = doc["state"] | -1;
  if (feature == nullptr || (state != 0 && state != 1)) {
    Serial.println("{\"error\":\"bad_relay_args\"}");
    return;
  }

  bool found = setFeatureRelay(String(feature), state == 1);
  StaticJsonDocument<128> ack;
  ack["ack"] = "relay";
  ack["feature"] = feature;
  ack["state"] = state;
  ack["ok"] = found;
  serializeJson(ack, Serial);
  Serial.println();
}

bool setFeatureRelay(const String& feature, bool state) {
  for (int i = 0; i < RELAY_MAP_SIZE; i++) {
    if (feature == relayMap[i].name) {
      digitalWrite(relayMap[i].pin, state ? HIGH : LOW);
      return true;
    }
  }
  return false;  // unknown feature name — command is ignored, not guessed at
}
