#pragma once
#include "esp_camera.h"
#include "logbuf.h"
#include "config.h"

// AI-Thinker ESP32-CAM pins
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

static bool g_camOk = false;
static String g_camInfo = "not started";

inline bool cameraInit() {
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
  c.fb_count = 1;
  c.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;

  // Try native JPEG first (OV2640). Your GC2145 (PID 0x2145) has none, so fall back to RGB565 + software JPEG.
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size = psramFound() ? FRAMESIZE_SVGA : FRAMESIZE_VGA;
  c.jpeg_quality = 12;
  esp_err_t err = esp_camera_init(&c);
  g_camInfo = "JPEG";
  if (err != ESP_OK) {
    esp_camera_deinit();
    c.pixel_format = PIXFORMAT_RGB565;
    c.frame_size = FRAMESIZE_VGA;  // 640x480
    err = esp_camera_init(&c);
    g_camInfo = "RGB565->JPEG";
  }
  g_camOk = (err == ESP_OK);
  if (g_camOk) {
    sensor_t *s = esp_camera_sensor_get();
    char b[24];
    snprintf(b, sizeof b, " PID 0x%04x", s ? s->id.PID : 0);
    g_camInfo += b;
    LOG("camera ok: " + g_camInfo);
  } else {
    char b[32];
    snprintf(b, sizeof b, "camera FAIL 0x%x", err);
    g_camInfo = b;
    LOG(g_camInfo);
  }
  return g_camOk;
}

// Fast preview for the dashboard's live view: half-size (320x240), lower quality. Not used for plate reading.
// Returns a heap buffer (caller must free()). Falls back to a full frame if the format is not RGB565.
inline uint8_t *cameraCaptureSmallJpeg(size_t *outLen);

// Returns a heap buffer holding a JPEG (caller must free()). nullptr on failure.
inline uint8_t *cameraCaptureJpeg(size_t *outLen) {
  if (!g_camOk) return nullptr;
  camera_fb_t *fb = esp_camera_fb_get();           // first frame may be stale: discard it
  if (fb) esp_camera_fb_return(fb);
  fb = esp_camera_fb_get();
  if (!fb) return nullptr;
  uint8_t *out = nullptr;
  if (fb->format == PIXFORMAT_JPEG) {
    out = (uint8_t *)ps_malloc(fb->len);
    if (!out) out = (uint8_t *)malloc(fb->len);
    if (out) { memcpy(out, fb->buf, fb->len); *outLen = fb->len; }
  } else {
    size_t l = 0;
    if (frame2jpg(fb, JPEG_QUALITY, &out, &l)) *outLen = l; else out = nullptr;
  }
  esp_camera_fb_return(fb);
  return out;
}

inline uint8_t *cameraCaptureSmallJpeg(size_t *outLen) {
  if (!g_camOk) return nullptr;
  camera_fb_t *fb = esp_camera_fb_get();           // preview: a slightly old frame is fine, so no discard (faster)
  if (!fb) return nullptr;
  uint8_t *out = nullptr;
  size_t l = 0;
  if (fb->format == PIXFORMAT_RGB565 && fb->width >= 320) {
    const int w = fb->width / 2, h = fb->height / 2;
    const size_t n = (size_t)w * h * 2;
    uint16_t *small = (uint16_t *)ps_malloc(n);
    if (small) {
      const uint16_t *src = (const uint16_t *)fb->buf;
      for (int y = 0; y < h; y++) {                // keep every 2nd pixel of every 2nd row
        const uint16_t *row = src + (size_t)(y * 2) * fb->width;
        for (int x = 0; x < w; x++) small[(size_t)y * w + x] = row[x * 2];
      }
      if (fmt2jpg((uint8_t *)small, n, w, h, PIXFORMAT_RGB565, 55, &out, &l)) *outLen = l; else out = nullptr;
      free(small);
    }
  } else if (fb->format == PIXFORMAT_JPEG) {
    out = (uint8_t *)ps_malloc(fb->len);
    if (out) { memcpy(out, fb->buf, fb->len); *outLen = fb->len; }
  }
  esp_camera_fb_return(fb);
  return out;
}
