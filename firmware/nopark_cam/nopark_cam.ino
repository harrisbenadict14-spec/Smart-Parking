/*
  Smart No-Parking: ESP32-CAM firmware
  Board: AI Thinker ESP32-CAM | Core: esp32 3.x | Libraries: TinyGPSPlus, ArduinoJson
  Arduino IDE: Tools > Core Debug Level = None, Partition Scheme = Huge APP

  UART0 is wired to the SIM800L, so there is NO serial debug output. Look at
  http://<board-ip>/log?k=<WEB_TOKEN> instead. After the first USB flash you can re-flash over Wi-Fi.
*/
#include <WiFi.h>
#include <WebServer.h>
#include <ArduinoOTA.h>
#include <TinyGPSPlus.h>
#include "esp_task_wdt.h"
#include "config.h"
#include "logbuf.h"
#include "camera.h"
#include "gsm.h"
#include "net.h"

WebServer web(80);
TinyGPSPlus gps;
HardwareSerial GPSSerial(2);
Gsm gsm;

// ---- presence (debounced IR) ----
bool irStable = false, irCandidate = false, presenceDirty = false;
uint32_t irCandidateSince = 0;

// ---- scheduling ----
uint32_t nextHeartbeat = 0, nextLease = 0, lastCapture = 0, lastAckTry = 0;
bool wantCapture = false;
uint32_t bootId = 0, captureSeq = 0;
uint8_t ackFails = 0;

bool irRaw() { return digitalRead(PIN_IR_OUT) == (IR_ACTIVE_LOW ? LOW : HIGH); }

void irLoop() {
  bool raw = irRaw();
  uint32_t now = millis();
  if (raw != irCandidate) { irCandidate = raw; irCandidateSince = now; }
  if (irCandidate != irStable && now - irCandidateSince >= IR_DEBOUNCE_MS) {
    irStable = irCandidate;
    presenceDirty = true;
    LOG(String("presence -> ") + (irStable ? "PRESENT" : "clear"));
    if (!irStable) wantCapture = false;
  }
}

bool gpsValid() { return gps.location.isValid() && gps.location.age() < 10000; }

void doCapture() {
  if (!g_camOk) return;
  uint32_t t = millis();
  size_t len = 0;
  uint8_t *jpg = cameraCaptureJpeg(&len);
  if (!jpg) { LOG("capture failed"); lastCapture = millis(); return; }
  String key = WiFi.macAddress() + "-" + String(bootId, HEX) + "-" + String(++captureSeq);
  bool ok = uploadCapture(jpg, len, key);
  free(jpg);
  lastCapture = millis();
  if (ok) wantCapture = false;  // wait for the backend to say another photo is needed
  LOG(String("capture ") + (ok ? "uploaded" : "upload FAILED") + " in " + (millis() - t) + "ms");
}

void commsLoop() {
  uint32_t now = millis();
  if (!wifiUp()) return;

  if (presenceDirty) {
    if (sendPresence(irStable)) presenceDirty = false;
    else if (now - lastAckTry > 3000) lastAckTry = now;  // retried next pass
  }

  if (now >= nextHeartbeat) {
    bool need = false;
    if (sendHeartbeat(irStable, gpsValid(), gps.location.lat(), gps.location.lng(),
                      gps.satellites.isValid() ? gps.satellites.value() : 0, gsm.csq, gsm.ready, &need)) {
      if (irStable && need) wantCapture = true;
    }
    nextHeartbeat = now + HEARTBEAT_INTERVAL_MS;
  }

  if (irStable && wantCapture && now - lastCapture >= CAPTURE_MIN_GAP_MS) doCapture();

  // Report a finished SMS; keep retrying the ack (backend lease is 120 s) so the outcome is not lost.
  if (gsm.smsFinished()) {
    if (gsm.smsOutcome.length() == 0) gsm.clearResult();  // outcome unknown: deliberately no ack
    else if (now - lastAckTry >= 2000) {
      lastAckTry = now;
      if (ackCommand(gsm.smsId, gsm.smsOutcome, gsm.smsResponse) || ++ackFails >= 20) { gsm.clearResult(); ackFails = 0; }
    }
  }

  // Ask for the next SMS only when the modem is idle AND registered, so jobs wait safely in the queue.
  if (gsm.idle() && gsm.ready && !gsm.smsFinished() && now >= nextLease) {
    nextLease = now + LEASE_POLL_INTERVAL_MS;
    SmsJob job = leaseCommand();
    if (job.valid) {
      LOG("sms job " + job.id.substring(0, 8));
      if (!gsm.startSms(job.id, job.to, job.msg)) {
        ackCommand(job.id, "FAILED_RETRYABLE", "firmware refused job (busy or invalid number)");
      }
    }
  }
}

// ---------------- small local web UI (log, status, camera aiming) ----------------
bool authed() { return web.arg("k") == WEB_TOKEN; }

void setupWeb() {
  web.on("/status", []() {
    JsonDocument d;
    d["present"] = irStable; d["ir_raw"] = irRaw();
    d["camera"] = g_camInfo; d["wifi_rssi"] = WiFi.RSSI(); d["ip"] = WiFi.localIP().toString();
    d["gps_fix"] = gpsValid(); d["gps_sats"] = gps.satellites.isValid() ? gps.satellites.value() : 0;
    d["gsm_ready"] = gsm.ready; d["gsm_alive"] = gsm.modemAlive(); d["sim"] = gsm.sim; d["reg"] = gsm.reg; d["csq"] = gsm.csq;
    d["http_fails"] = g_httpFails; d["free_heap"] = (int)ESP.getFreeHeap(); d["uptime_s"] = millis() / 1000;
    String s; serializeJson(d, s); web.send(200, "application/json", s);
  });
  web.on("/log", []() { if (!authed()) { web.send(401, "text/plain", "token required"); return; } web.send(200, "text/plain", logDump()); });
  web.on("/cam.jpg", []() {
    if (!authed()) { web.send(401, "text/plain", "token required"); return; }
    size_t len = 0;
    uint8_t *j = (web.arg("s") == "1") ? cameraCaptureSmallJpeg(&len) : cameraCaptureJpeg(&len);
    if (!j) { web.send(500, "text/plain", "capture failed"); return; }
    web.sendHeader("Cache-Control", "no-store");
    web.send_P(200, "image/jpeg", (const char *)j, len);
    free(j);
  });
  web.begin();
}

void setup() {
  pinMode(PIN_IR_OUT, INPUT);                        // safe state first
  bootId = esp_random();
  LOG(String("boot, reset reason ") + (int)esp_reset_reason());  // 1=power-on 3=sw 4=panic 5/6=wdt 15=brownout
  gsm.begin();
  GPSSerial.begin(9600, SERIAL_8N1, PIN_GPS_RX, -1);
  cameraInit();
  wifiBegin();

  ArduinoOTA.setHostname("nopark-cam");
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.begin();
  setupWeb();

  esp_task_wdt_config_t wdt = {.timeout_ms = WDT_TIMEOUT_S * 1000, .idle_core_mask = 0, .trigger_panic = true};
  esp_task_wdt_reconfigure(&wdt);
  esp_task_wdt_add(NULL);
}

void loop() {
  esp_task_wdt_reset();
  while (GPSSerial.available()) gps.encode(GPSSerial.read());
  irLoop();
  gsm.loop();
  wifiLoop();
  commsLoop();
  ArduinoOTA.handle();
  web.handleClient();
}
