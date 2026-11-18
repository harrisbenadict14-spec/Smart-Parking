/*
  Smart No-Parking - Bench Test (Milestone 1)
  Board: AI-Thinker ESP32-CAM (Arduino IDE: "AI Thinker ESP32-CAM", Core Debug Level = None)
  Library needed: TinyGPSPlus (Library Manager). ESP32 Arduino core 3.x.

  Because the SIM800L uses UART0 (GPIO1/GPIO3), USB serial can NOT be used for
  logging. All results are shown on a web page served by the ESP32 over Wi-Fi.

  FLASHING: unplug the SIM800L TXD wire (GPIO3/U0R) while uploading, then reconnect.
  Open the Serial Monitor NOT at all. Find the board's IP on your router's client list.
*/
#include <WiFi.h>
#include <WebServer.h>
#include <ArduinoOTA.h>
#include <TinyGPSPlus.h>
#include "esp_camera.h"
#include "config.h"

// ---- AI-Thinker camera pins ----
#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22

WebServer server(80);
TinyGPSPlus gps;
HardwareSerial GPSSerial(2);
bool cameraOk = false;
String cameraErr = "", cameraMode = "", cameraPid = "";

// ---------------- Camera ----------------
void initCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM; c.pin_d1 = Y3_GPIO_NUM; c.pin_d2 = Y4_GPIO_NUM; c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM; c.pin_d5 = Y7_GPIO_NUM; c.pin_d6 = Y8_GPIO_NUM; c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM; c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM; c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM; c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.grab_mode = CAMERA_GRAB_LATEST;
  // Attempt 1: native JPEG
  c.pixel_format = PIXFORMAT_JPEG;
  if (psramFound()) {
    c.frame_size = FRAMESIZE_SVGA; c.jpeg_quality = 12; c.fb_count = 2; c.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    c.frame_size = FRAMESIZE_VGA; c.jpeg_quality = 15; c.fb_count = 1; c.fb_location = CAMERA_FB_IN_DRAM;
  }
  esp_err_t err = esp_camera_init(&c);
  cameraMode = "JPEG";
  if (err != ESP_OK) {
    char b[48]; snprintf(b, sizeof b, "JPEG init 0x%x; ", err); cameraErr = b;
    esp_camera_deinit();
    // Attempt 2: sensor may lack hardware JPEG -> raw RGB565, converted to JPEG in software
    c.pixel_format = PIXFORMAT_RGB565;
    c.frame_size = FRAMESIZE_VGA;           // 640x480 (needs PSRAM)
    c.fb_count = 1; c.fb_location = CAMERA_FB_IN_PSRAM;
    err = esp_camera_init(&c);
    cameraMode = "RGB565->JPEG";
    if (err != ESP_OK) { snprintf(b, sizeof b, "RGB565 init 0x%x", err); cameraErr += b; }
  }
  cameraOk = (err == ESP_OK);
  if (cameraOk) {
    cameraErr = "";
    sensor_t *s = esp_camera_sensor_get();
    char b[32]; snprintf(b, sizeof b, "PID 0x%04x", s ? s->id.PID : 0); cameraPid = b;
  }
}

// ---------------- GSM (UART0 / Serial) ----------------
String gsmCmd(const String &cmd, uint32_t timeoutMs = 2000, const char *until = "OK") {
  while (Serial.available()) Serial.read();
  Serial.print(cmd); Serial.print("\r\n");
  String resp; uint32_t start = millis();
  while (millis() - start < timeoutMs) {
    while (Serial.available()) resp += (char)Serial.read();
    if (resp.indexOf(until) >= 0 || resp.indexOf("ERROR") >= 0) break;
    delay(5);
  }
  delay(250);                                   // let the rest of an error line arrive
  while (Serial.available()) resp += (char)Serial.read();
  resp.trim();
  return resp.length() ? resp : String("(no response)");
}

String gsmReport() {
  String r;
  auto step = [&](const char *label, const String &cmd, uint32_t t = 2000) {
    r += String("> ") + label + " [" + cmd + "]\n" + gsmCmd(cmd, t) + "\n\n";
  };
  // Autobaud needs a few AT's
  for (int i = 0; i < 3; i++) gsmCmd("AT", 500);
  step("Modem alive", "AT");
  gsmCmd("ATE0", 500);
  step("SIM status (want +CPIN: READY)", "AT+CPIN?");
  step("Signal (CSQ 10-31 ok; 99 = none)", "AT+CSQ");
  step("Registration (want 0,1 or 0,5)", "AT+CREG?", 3000);
  step("Operator", "AT+COPS?", 5000);
  step("Supply (+CBC: ,pct,millivolts)", "AT+CBC");
  step("Text mode", "AT+CMGF=1");
  return r;
}

// Diagnostic: try several baud rates and both pin orientations, report any bytes heard.
String gsmScan() {
  String r;
  const uint32_t bauds[] = {9600, 115200, 57600, 19200, 38400};
  const int orient[2][2] = {{3, 1}, {1, 3}};   // {rxPin, txPin}
  const char *names[2] = {"normal (RX=GPIO3, TX=GPIO1)", "SWAPPED (RX=GPIO1, TX=GPIO3)"};
  for (int o = 0; o < 2; o++) {
    for (uint32_t b : bauds) {
      Serial.end();
      Serial.begin(b, SERIAL_8N1, orient[o][0], orient[o][1]);
      delay(100);
      String got;
      for (int i = 0; i < 3; i++) {
        while (Serial.available()) Serial.read();
        Serial.print("AT\r\n");
        uint32_t s = millis();
        while (millis() - s < 700) { while (Serial.available()) got += (char)Serial.read(); delay(5); }
        if (got.indexOf("OK") >= 0) break;
      }
      got.trim();
      String shown;
      for (size_t i = 0; i < got.length() && i < 30; i++) shown += isPrintable(got[i]) ? String(got[i]) : String("?");
      r += String(names[o]) + " @" + String(b) + ": " + (got.length() ? String(got.length()) + " bytes -> " + shown : String("silence")) + "\n";
    }
  }
  Serial.end();
  Serial.begin(GSM_BAUD, SERIAL_8N1, GSM_RX_PIN, GSM_TX_PIN);   // restore
  return r;
}

// Only digits and a leading + ; message restricted to safe printable chars
bool validNumber(const String &n) {
  if (n.length() < 8 || n.length() > 16) return false;
  for (size_t i = 0; i < n.length(); i++) {
    char c = n[i];
    if (!(isDigit(c) || (i == 0 && c == '+'))) return false;
  }
  return true;
}

String gsmSendSms(const String &to, const String &msg) {
  String r = gsmCmd("AT+CMGF=1", 1000);
  while (Serial.available()) Serial.read();
  Serial.print("AT+CMGS=\""); Serial.print(to); Serial.print("\"\r\n");
  String resp; uint32_t s = millis();
  while (millis() - s < 5000 && resp.indexOf('>') < 0) { while (Serial.available()) resp += (char)Serial.read(); delay(5); }
  if (resp.indexOf('>') < 0) { Serial.write(27); return "No '>' prompt. Got: " + resp; }
  Serial.print(msg); Serial.write(26);
  resp = ""; s = millis();
  while (millis() - s < 30000) {
    while (Serial.available()) resp += (char)Serial.read();
    if (resp.indexOf("+CMGS") >= 0 && resp.indexOf("OK") >= 0) break;
    if (resp.indexOf("ERROR") >= 0) break;
    delay(10);
  }
  resp.trim();
  return resp + "\n(Note: +CMGS means the modem/network ACCEPTED it, not that the phone received it.)";
}

// ---------------- Web UI ----------------
const char PAGE[] PROGMEM = R"HTML(<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Bench Test</title><style>body{font:15px system-ui;max-width:720px;margin:16px auto;padding:0 12px}
pre{background:#111;color:#0f0;padding:10px;white-space:pre-wrap;border-radius:6px}button{padding:8px 12px;margin:3px}
.k{display:grid;grid-template-columns:150px 1fr;gap:4px}img{max-width:100%}</style>
<h2>Smart No-Parking: Bench Test</h2>
<div class=k id=st>loading...</div>
<h3>Camera</h3><button onclick="document.getElementById('im').src='/cam.jpg?'+Date.now()">Capture</button><br><img id=im>
<h3>GSM (SIM800L)</h3>
<button onclick="run('/gsm/test')">Run GSM checks</button><br>
<input id=to placeholder="+91XXXXXXXXXX"> <input id=msg value="Bench test SMS">
<button onclick="sms()">Send test SMS</button>
<pre id=out>results appear here</pre>
<script>
async function poll(){try{const j=await (await fetch('/status')).json();
let h='';for(const[k,v]of Object.entries(j))h+=`<b>${k}</b><span>${v}</span>`;st.innerHTML=h}catch(e){st.textContent='offline'}}
setInterval(poll,1000);poll();
async function run(u){out.textContent='running...';out.textContent=await (await fetch(u)).text()}
function sms(){const t=to.value.trim();if(!confirm('Send a real SMS to '+t+'? (uses SIM credit)'))return;
run('/gsm/sms?to='+encodeURIComponent(t)+'&msg='+encodeURIComponent(msg.value))}
</script>)HTML";

void handleStatus() {
  int raw = digitalRead(PIN_IR_OUT);
  bool present = IR_ACTIVE_LOW ? (raw == LOW) : (raw == HIGH);
  String j = "{";
  j += "\"IR raw level\":\"" + String(raw) + "\",";
  j += "\"IR says present\":\"" + String(present ? "YES" : "no") + "\",";
  j += "\"Camera\":\"" + String(cameraOk ? "OK " + cameraMode + " " + cameraPid : "FAIL " + cameraErr) + "\",";
  j += "\"PSRAM\":\"" + String(psramFound() ? "yes" : "no") + "\",";
  j += "\"GPS bytes seen\":\"" + String(gps.charsProcessed()) + "\",";
  j += "\"GPS sentences ok\":\"" + String(gps.passedChecksum()) + "\",";
  j += "\"GPS satellites\":\"" + String(gps.satellites.isValid() ? gps.satellites.value() : 0) + "\",";
  j += "\"GPS fix\":\"" + String(gps.location.isValid() ? String(gps.location.lat(), 6) + ", " + String(gps.location.lng(), 6) : String("no fix")) + "\",";
  j += "\"WiFi RSSI\":\"" + String(WiFi.RSSI()) + " dBm\",";
  j += "\"Free heap\":\"" + String(ESP.getFreeHeap()) + "\"}";
  server.send(200, "application/json", j);
}

void handleCam() {
  if (!cameraOk) { server.send(500, "text/plain", "camera not initialised: " + cameraErr); return; }
  camera_fb_t *fb = esp_camera_fb_get();
  if (fb) { esp_camera_fb_return(fb); fb = esp_camera_fb_get(); } // discard stale frame
  if (!fb) { server.send(500, "text/plain", "capture failed"); return; }
  server.sendHeader("Cache-Control", "no-store");
  if (fb->format == PIXFORMAT_JPEG) {
    server.send_P(200, "image/jpeg", (const char *)fb->buf, fb->len);
  } else {
    uint8_t *jpg = nullptr; size_t jl = 0;
    if (frame2jpg(fb, 80, &jpg, &jl)) { server.send_P(200, "image/jpeg", (const char *)jpg, jl); free(jpg); }
    else server.send(500, "text/plain", "jpeg conversion failed");
  }
  esp_camera_fb_return(fb);
}

void setup() {
  pinMode(PIN_IR_OUT, INPUT);               // add external pull-up if your module output floats
  Serial.begin(GSM_BAUD, SERIAL_8N1, GSM_RX_PIN, GSM_TX_PIN);   // UART0 = SIM800L (no debug prints!)
  GPSSerial.begin(9600, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
  initCamera();
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t s = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - s < 20000) delay(250);

  server.on("/", []() { server.send_P(200, "text/html", PAGE); });
  server.on("/status", handleStatus);
  server.on("/cam.jpg", handleCam);
  server.on("/gsm/test", []() { server.send(200, "text/plain", gsmReport()); });
  // Bench-only raw AT console. Refuses SMS-send, dial and data commands.
  server.on("/gsm/at", []() {
    String c = server.arg("cmd"); c.trim();
    String u = c; u.toUpperCase();
    bool ok = u.startsWith("AT") && c.length() <= 40 && u.indexOf("CMGS") < 0 && u.indexOf("CMGW") < 0 &&
              !u.startsWith("ATD") && u.indexOf("CIPSEND") < 0 && u.indexOf("CIICR") < 0;
    if (!ok) { server.send(400, "text/plain", "refused"); return; }
    server.send(200, "text/plain", gsmCmd(c, 5000));
  });
  server.on("/gsm/scan", []() { server.send(200, "text/plain", gsmScan()); });
  server.on("/gsm/sms", []() {
    String to = server.arg("to"), msg = server.arg("msg");
    if (!validNumber(to)) { server.send(400, "text/plain", "Invalid number"); return; }
    msg.replace("\"", ""); if (msg.length() > 140) msg = msg.substring(0, 140);
    server.send(200, "text/plain", gsmSendSms(to, msg));
  });
  server.begin();
  ArduinoOTA.setHostname("nopark-cam");
  ArduinoOTA.setPassword(OTA_PASSWORD);   // wireless re-flash, no need to unplug GSM wires
  ArduinoOTA.begin();
}

void loop() {
  ArduinoOTA.handle();
  while (GPSSerial.available()) gps.encode(GPSSerial.read());
  server.handleClient();
  if (WiFi.status() != WL_CONNECTED) { WiFi.reconnect(); delay(1000); }
}
