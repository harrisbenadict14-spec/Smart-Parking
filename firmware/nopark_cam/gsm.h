#pragma once
// Non-blocking SIM800L driver. One AT job at a time. Never calls delay().
//
// SMS outcome rules (mirror the backend's truthfulness rules):
//   "+CMGS: n" + OK        -> SUBMITTED   (network accepted it; NOT proof the phone received it)
//   modem error / no '>'   -> FAILED_RETRYABLE  (nothing was sent)
//   silence after the text was transmitted -> NO ack (outcome unknown): the backend marks the
//                              command UNCERTAIN and does not resend automatically.
#include <Arduino.h>
#include "logbuf.h"
#include "config.h"

class Gsm {
 public:
  enum Phase { INIT_AT, INIT_CFG, IDLE, STATUS, SMS_PROMPT, SMS_BODY, BACKOFF };

  bool ready = false;        // SIM READY and registered on the network
  int csq = -1;
  String sim = "?", reg = "?", lastError = "";

  // finished SMS result, valid when smsFinished()==true until clearResult()
  bool smsFinished() const { return _resultReady; }
  String smsId, smsOutcome, smsResponse;
  bool hasResultToAck() const { return _resultReady && smsOutcome.length() > 0; }
  void clearResult() { _resultReady = false; smsId = smsOutcome = smsResponse = ""; }

  void begin() {
    Serial.setRxBufferSize(1024);                       // HTTP uploads block the loop for seconds
    Serial.begin(GSM_BAUD, SERIAL_8N1, GSM_RX_PIN, GSM_TX_PIN);
    _enter(INIT_AT);
  }

  bool idle() const { return _phase == IDLE && !_busy; }
  bool modemAlive() const { return _alive; }

  // Queue one SMS. Returns false if the driver is busy or the input is invalid.
  bool startSms(const String &id, const String &to, const String &msg) {
    if (!idle() || !ready || !_validNumber(to)) return false;
    smsId = id; _to = to; _msg = _sanitize(msg);
    _smsStartedBody = false;
    _send("AT+CMGS=\"" + to + "\"", 8000, ">");
    _phase = SMS_PROMPT;
    return true;
  }

  void loop() {
    while (Serial.available()) {
      char c = Serial.read();
      if (_rx.length() < 400) _rx += c;
    }
    uint32_t now = millis();
    if (_phase == BACKOFF) { if (now >= _until) _enter(INIT_AT); return; }
    if (!_busy) { _idleWork(now); return; }

    // Wait for a complete line before judging, otherwise "+CME ERROR: S" is read before "IM not inserted"
    // arrives and the tail pollutes the next command. The '>' SMS prompt has no newline.
    bool lineDone = _rx.endsWith("\n");
    bool gotExpect = _rx.indexOf(_expect) >= 0 && (_expect == ">" || lineDone);
    bool gotError = _rx.indexOf("ERROR") >= 0 && lineDone;
    bool timedOut = now > _deadline;
    if (!gotExpect && !gotError && !timedOut) return;
    _busy = false;
    _alive = _alive || gotExpect || gotError;
    _onResult(gotExpect && !gotError, gotError, timedOut);
  }

 private:
  Phase _phase = INIT_AT;
  bool _busy = false, _alive = false, _resultReady = false, _smsStartedBody = false;
  String _rx, _expect, _to, _msg;
  uint32_t _deadline = 0, _until = 0, _nextStatus = 0;
  uint8_t _step = 0, _fails = 0;

  static bool _validNumber(const String &n) {
    if (n.length() < 8 || n.length() > 16 || n[0] != '+') return false;
    for (size_t i = 1; i < n.length(); i++) if (!isDigit(n[i])) return false;
    return true;
  }
  static String _sanitize(const String &m) {
    String o;
    for (size_t i = 0; i < m.length() && o.length() < 160; i++) {
      char c = m[i];
      o += (c >= 32 && c < 127 && c != '"') ? c : (c == '\n' ? ' ' : '?');
    }
    return o;
  }

  void _send(const String &cmd, uint32_t timeoutMs, const char *expect = "OK") {
    _rx = "";
    Serial.print(cmd); Serial.print("\r\n");
    _expect = expect; _deadline = millis() + timeoutMs; _busy = true;
  }
  void _enter(Phase p) { _phase = p; _step = 0; _busy = false; _rx = ""; }

  void _idleWork(uint32_t now) {
    switch (_phase) {
      case INIT_AT:  _send("AT", 1000); break;
      case INIT_CFG: {
        static const char *cmds[] = {"ATE0", "AT+CMEE=2", "AT+CMGF=1", "AT+CSCS=\"GSM\""};
        if (_step < 4) _send(cmds[_step], 2000); else { _phase = IDLE; _nextStatus = now; LOG("gsm configured"); }
        break;
      }
      case IDLE:
        if (now >= _nextStatus) { _phase = STATUS; _step = 0; }
        break;
      case STATUS: {
        static const char *cmds[] = {"AT+CPIN?", "AT+CSQ", "AT+CREG?"};
        if (_step < 3) _send(cmds[_step], 3000);
        else {
          bool wasReady = ready;
          ready = sim.indexOf("READY") >= 0 && (reg.indexOf(",1") >= 0 || reg.indexOf(",5") >= 0);
          if (ready != wasReady) LOG(String("gsm ready=") + ready + " sim=" + sim + " reg=" + reg + " csq=" + csq);
          _phase = IDLE; _nextStatus = now + GSM_STATUS_INTERVAL_MS;
        }
        break;
      }
      default: break;
    }
  }

  void _onResult(bool ok, bool err, bool timeout) {
    switch (_phase) {
      case INIT_AT:
        if (ok) { _fails = 0; _enter(INIT_CFG); }
        else if (++_fails >= 5) { lastError = "modem not answering"; LOG("gsm: no answer, backing off"); _fails = 0; _phase = BACKOFF; _until = millis() + 10000; }
        break;
      case INIT_CFG:
        _step++;  // config errors are non-fatal; keep going
        break;
      case STATUS: {
        String r = _rx; r.trim();
        if (_step == 0) sim = err ? _firstLine(r) : (r.indexOf("READY") >= 0 ? "READY" : _firstLine(r));
        else if (_step == 1) { int i = r.indexOf("+CSQ:"); csq = (ok && i >= 0) ? r.substring(i + 5).toInt() : -1; }
        else if (_step == 2) reg = ok ? r.substring(max(0, r.indexOf("+CREG:"))) : "?";
        _step++;
        break;
      }
      case SMS_PROMPT:
        if (ok && _rx.indexOf('>') >= 0) {
          _rx = "";
          Serial.print(_msg); Serial.write(26);        // text + Ctrl+Z
          _expect = "OK"; _deadline = millis() + 60000; _busy = true; _smsStartedBody = true;
          _phase = SMS_BODY;
        } else {
          Serial.write(27);                            // ESC: abort any half-open prompt
          _finishSms("FAILED_RETRYABLE", err ? _firstLine(_rx) : "no '>' prompt from modem");
        }
        break;
      case SMS_BODY: {
        String r = _rx; r.trim();
        if (ok && r.indexOf("+CMGS") >= 0) _finishSms("SUBMITTED", r.substring(r.indexOf("+CMGS")));
        else if (err) _finishSms("FAILED_RETRYABLE", _firstLine(r));
        else if (ok) _finishSms("SUBMITTED", r);        // OK without +CMGS: treat as accepted but note it
        else { LOG("sms: no final response; outcome unknown, NOT acking"); smsOutcome = ""; _resultReady = true; _phase = IDLE; }
        break;
      }
      default: break;
    }
  }

  void _finishSms(const char *outcome, const String &resp) {
    smsOutcome = outcome; smsResponse = resp.substring(0, 120); _resultReady = true;
    LOG(String("sms ") + smsId.substring(0, 8) + " -> " + outcome + " | " + smsResponse);
    _phase = IDLE; _nextStatus = millis() + 2000;
  }

  static String _firstLine(const String &s) {
    String t = s; t.trim();
    int n = t.indexOf('\n');
    return (n > 0 ? t.substring(0, n) : t).substring(0, 60);
  }
};
