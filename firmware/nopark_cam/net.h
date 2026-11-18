#pragma once
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "logbuf.h"
#include "config.h"

static uint32_t g_wifiRetryAt = 0;
static uint32_t g_httpFails = 0;

inline void wifiBegin() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

// Non-blocking Wi-Fi supervision: retry every 10 s while disconnected.
inline void wifiLoop() {
  static bool wasUp = false;
  bool up = WiFi.status() == WL_CONNECTED;
  if (up != wasUp) { LOG(up ? "wifi up " + WiFi.localIP().toString() : String("wifi down")); wasUp = up; }
  if (!up && millis() > g_wifiRetryAt) { WiFi.disconnect(); WiFi.begin(WIFI_SSID, WIFI_PASSWORD); g_wifiRetryAt = millis() + 10000; }
}
inline bool wifiUp() { return WiFi.status() == WL_CONNECTED; }

// Generic JSON POST. Returns HTTP status (or <0 on transport error); fills `resp`.
inline int postJson(const char *path, const String &body, String *resp = nullptr, const char *extraHeaderName = nullptr, const String &extraHeaderValue = "") {
  if (!wifiUp()) return -1;
  HTTPClient http;
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(String(BACKEND_URL) + path)) return -2;
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_KEY);
  if (extraHeaderName) http.addHeader(extraHeaderName, extraHeaderValue);
  int code = http.POST(body);
  if (resp && code > 0) *resp = http.getString();
  http.end();
  if (code < 200 || code >= 300) { g_httpFails++; LOG(String("POST ") + path + " -> " + code); }
  return code;
}

// Heartbeat. gpsValid => lat/lng included. Returns true on success; sets *captureNeeded from the reply.
inline bool sendHeartbeat(bool present, bool gpsValid, double lat, double lng, int sats, int csq, bool gsmReady, bool *captureNeeded) {
  JsonDocument d;
  d["present"] = present;
  d["rssi"] = WiFi.RSSI();
  if (gpsValid) { d["gps"]["lat"] = lat; d["gps"]["lng"] = lng; d["gps"]["sats"] = sats; }
  d["gsm"]["ready"] = gsmReady;
  d["gsm"]["csq"] = csq;
  d["gsm"]["free_heap"] = (int)ESP.getFreeHeap();
  String body, resp;
  serializeJson(d, body);
  int code = postJson("/api/v1/device/heartbeat", body, &resp);
  if (code != 200) return false;
  JsonDocument r;
  if (!deserializeJson(r, resp) && captureNeeded) *captureNeeded = r["capture_needed"] | false;
  return true;
}

inline bool sendPresence(bool present) {
  return postJson("/api/v1/device/presence", String("{\"present\":") + (present ? "true" : "false") + "}") == 200;
}

// multipart/form-data upload of one JPEG. Idempotency-Key makes retries safe.
inline bool uploadCapture(const uint8_t *jpg, size_t len, const String &idemKey) {
  if (!wifiUp()) return false;
  const String boundary = "----nopark7MA4YWxkTrZu0gW";
  String head = "--" + boundary + "\r\nContent-Disposition: form-data; name=\"image\"; filename=\"cap.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n";
  String tail = "\r\n--" + boundary + "--\r\n";
  size_t total = head.length() + len + tail.length();
  uint8_t *buf = (uint8_t *)ps_malloc(total);
  if (!buf) buf = (uint8_t *)malloc(total);
  if (!buf) { LOG("upload: out of memory"); return false; }
  memcpy(buf, head.c_str(), head.length());
  memcpy(buf + head.length(), jpg, len);
  memcpy(buf + head.length() + len, tail.c_str(), tail.length());

  HTTPClient http;
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS * 2);
  bool ok = false;
  if (http.begin(String(BACKEND_URL) + "/api/v1/device/capture")) {
    http.addHeader("Content-Type", "multipart/form-data; boundary=" + boundary);
    http.addHeader("X-Device-Key", DEVICE_KEY);
    http.addHeader("Idempotency-Key", idemKey);
    int code = http.POST(buf, total);
    ok = (code == 201);
    LOG(String("capture upload ") + len + "B -> " + code);
    http.end();
  }
  free(buf);
  return ok;
}

struct SmsJob { bool valid = false; String id, to, msg; };

inline SmsJob leaseCommand() {
  SmsJob job;
  String resp;
  if (postJson("/api/v1/device/commands/lease", "{}", &resp) != 200) return job;
  JsonDocument r;
  if (deserializeJson(r, resp) || r["command"].isNull()) return job;
  job.id = r["command"]["id"].as<String>();
  job.to = r["command"]["to"].as<String>();
  job.msg = r["command"]["message"].as<String>();
  job.valid = job.id.length() > 0;
  return job;
}

inline bool ackCommand(const String &id, const String &outcome, const String &modemResponse) {
  JsonDocument d;
  d["outcome"] = outcome;
  d["modem_response"] = modemResponse;
  String body;
  serializeJson(d, body);
  return postJson((String("/api/v1/device/commands/") + id + "/ack").c_str(), body) == 200;
}
