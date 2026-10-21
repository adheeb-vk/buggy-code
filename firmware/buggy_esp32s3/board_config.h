#pragma once

/*
  Buggy ESP32-S3 board configuration.

  Every pin is -1 until its wiring is physically verified. A -1 sensor is
  omitted from telemetry. A -1 relay remains disabled and cannot be switched.
  This lets the same firmware run safely with no sensors or relays connected.

  TODO: Set only pins that have been verified on this exact ESP32-S3 board.
  TODO: Set RELAY_ON_LEVEL after verifying whether the relay board is active
        HIGH or active LOW. Do not connect a relay before doing so.
*/

// ---- Analog sensors -------------------------------------------------------
// MQ-3 AO through a 20 kOhm / 10 kOhm divider. GPIO4 is labelled "4" on this
// ESP32-S3 N16R8 board and is ADC1_CH3. TODO: Revert to -1 if wiring differs.
constexpr int PIN_ALCOHOL = 4;
constexpr int PIN_WATER = -1;    // TODO: ADC-capable GPIO or -1
constexpr int PIN_CURRENT = -1;  // TODO: ADC-capable GPIO or -1

// ---- Optional ultrasonic parking sensors -------------------------------
// Proposed assignments from the uploaded ESP32-S3 N16R8 pinout. These are
// GPIO numbers (use the board's GPIO labels, not header pin positions).
// TODO: Confirm your sensor leads match this table before powering the sensors.
// Do not feed a 5 V echo output directly into the ESP32-S3; level-shift it.
constexpr int PIN_US_FRONT_LEFT_TRIG = 5;  // TODO: proposed GPIO5
constexpr int PIN_US_FRONT_LEFT_ECHO = 6;  // TODO: proposed GPIO6
constexpr int PIN_US_FRONT_RIGHT_TRIG = 7; // TODO: proposed GPIO7
constexpr int PIN_US_FRONT_RIGHT_ECHO = 8; // TODO: proposed GPIO8
constexpr int PIN_US_REAR_TRIG = 9;        // TODO: proposed GPIO9
constexpr int PIN_US_REAR_ECHO = 10;       // TODO: proposed GPIO10

// ---- Digital sensors ------------------------------------------------------
constexpr int PIN_SEATBELT = -1;  // TODO: GPIO or -1; expected active LOW

// ---- MPU-9250 / MPU-6500 IMU ----------------------------------------------
// Board labels: GPIO1 -> SDA and GPIO2 -> SCL. These pins have no boot, USB,
// flash, or onboard RGB assignment on the photographed ESP32-S3 N16R8 board.
// GPIO4 remains reserved for MQ-3 AO. Power the IMU from 3V3. AD0 may be tied
// to GND (0x68) or 3V3 (0x69); firmware scans both addresses.
constexpr int PIN_I2C_SDA = 1;
constexpr int PIN_I2C_SCL = 2;

// ---- Pi-commanded feature relays -----------------------------------------
constexpr int PIN_RELAY_DROWSINESS = -1;  // TODO: GPIO or -1
constexpr int PIN_RELAY_POTHOLE = -1;     // TODO: GPIO or -1
constexpr int PIN_RELAY_ALCOHOL = -1;     // TODO: GPIO or -1
constexpr int PIN_RELAY_GEOFENCE = -1;    // TODO: GPIO or -1

constexpr uint8_t RELAY_ON_LEVEL = HIGH;  // TODO: verify active level

// ---- Serial protocol ------------------------------------------------------
// 50 Hz preserves short road-shock samples for the pothole detector.
constexpr unsigned long TELEMETRY_INTERVAL_MS = 20;
constexpr unsigned long SERIAL_BAUD_RATE = 115200;
