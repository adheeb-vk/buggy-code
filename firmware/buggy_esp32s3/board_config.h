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

// ---- Digital sensors ------------------------------------------------------
constexpr int PIN_SEATBELT = -1;  // TODO: GPIO or -1; expected active LOW

// ---- MPU-9250 / MPU-6500 IMU ----------------------------------------------
// Board labels: GPIO8 -> SDA and GPIO9 -> SCL. The MPU module must be powered
// from 3V3, with AD0 tied to GND, so its I2C address is 0x68.
constexpr int PIN_I2C_SDA = 8;
constexpr int PIN_I2C_SCL = 9;
constexpr uint8_t MPU6050_ADDRESS = 0x68;

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
