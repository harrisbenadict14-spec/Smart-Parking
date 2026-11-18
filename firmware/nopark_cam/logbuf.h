#pragma once
// UART0 belongs to the modem, so there is no serial debugging. Last 40 events are kept in RAM
// and served at /log?k=WEB_TOKEN.
#include <Arduino.h>

static String g_log[40];
static int g_logHead = 0;

inline void LOG(const String &s) {
  g_log[g_logHead] = String(millis() / 1000) + "s " + s;
  g_logHead = (g_logHead + 1) % 40;
}

inline String logDump() {
  String r;
  for (int i = 0; i < 40; i++) {
    int idx = (g_logHead + i) % 40;
    if (g_log[idx].length()) r += g_log[idx] + "\n";
  }
  return r;
}
