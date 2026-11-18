#pragma once
// Copy this file to config.h and fill it in. config.h holds secrets: never share or commit it.
#define WIFI_SSID      "your-2.4GHz-wifi-name"
#define WIFI_PASSWORD  "your-wifi-password"

// Laptop running the backend (same Wi-Fi). Find it with `ipconfig`. A phone hotspot may change it.
#define BACKEND_URL    "http://192.168.1.50:8000"
// Dashboard > System > "Register a camera unit" shows this key ONCE. Paste it here.
#define DEVICE_KEY     "dk_paste_the_key_here"

#define OTA_PASSWORD   "choose-a-wireless-flash-password"
#define WEB_TOKEN      "choose-a-camera-view-token"   // must equal CAM_VIEW_TOKEN in backend\.env

// ---- Pins (verified on the bench; microSD is NOT used) ----
#define PIN_IR_OUT     13
#define IR_ACTIVE_LOW  1       // OUT = 0 when an object is in front
#define PIN_GPS_RX     14      // NEO-6M TX -> GPIO14
// SIM800L on UART0. Verified: the modem answers with ESP RX=GPIO1, TX=GPIO3.
#define GSM_RX_PIN     1
#define GSM_TX_PIN     3
#define GSM_BAUD       9600

// ---- Timing ----
#define IR_DEBOUNCE_MS          1500
#define HEARTBEAT_INTERVAL_MS   10000
#define CAPTURE_MIN_GAP_MS      15000
#define LEASE_POLL_INTERVAL_MS  5000
#define GSM_STATUS_INTERVAL_MS  30000
#define HTTP_TIMEOUT_MS         8000
#define JPEG_QUALITY            80
#define WDT_TIMEOUT_S           90
