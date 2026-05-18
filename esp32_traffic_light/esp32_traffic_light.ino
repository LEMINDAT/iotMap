#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <sys/time.h>
#include <time.h>

// Edit Wi-Fi here before uploading.
#define WIFI_SSID "4Chic"
#define WIFI_PASSWORD "@anhdatuet"

#define MQTT_HOST "gaccf6ca.ala.asia-southeast1.emqxsl.com"
#define MQTT_PORT 8883
#define MQTT_USERNAME "emqx_online_test_b9e4d5f9"
#define MQTT_PASSWORD "fK77ab_d-0dYa372JLb6c5fW14]c2f=d"

#define MQTT_TOPIC "traffic/A/001/state"

// System status LEDs.
#define LED_WIFI_PIN 2
#define LED_MQTT_PIN 5
#define LED_LOCAL_PIN 4
#define LED_PLAN_PIN 16
#define STATUS_LED_ACTIVE_HIGH true

struct LightPins {
  uint8_t red;
  uint8_t yellow;
  uint8_t green;
};

const LightPins GROUP_PINS[4] = {
  {25, 26, 27},
  {32, 33, 14},
  {23, 22, 21},
  {19, 18, 17},
};

WiFiClientSecure secureClient;
PubSubClient mqttClient(secureClient);

unsigned long lastPayloadMs = 0;
unsigned long lastReconnectAttemptMs = 0;
unsigned long fallbackPhaseStartMs = 0;
uint8_t fallbackPhase = 0;
bool fallbackActive = false;
bool remotePlanActive = false;
unsigned long remotePlanEndMs = 0;
int64_t lastAcceptedSentWallMs = 0;
char currentPlanId[64] = "";
unsigned long lastWiFiAttemptMs = 0;
bool wifiConnectStarted = false;
bool wifiWasConnected = false;

// Cải tiến NTP bất đồng bộ
bool timeSyncRequested = false;
unsigned long lastTimeSyncCheckMs = 0;

unsigned long mqttRxPulseUntilMs = 0;

struct PendingRemotePlan {
  bool active;
  int64_t sentWallMs;
  int64_t adjustedRemainingMs;
  bool abruptTransition;
  char planId[64];
  char colors[4][8];
};

PendingRemotePlan pendingRemotePlan;

const unsigned long MQTT_RECONNECT_INTERVAL_MS = 5000;
const unsigned long FAILSAFE_TIMEOUT_MS = 5000;
const unsigned long FALLBACK_GREEN_MS = 10000;
const unsigned long FALLBACK_YELLOW_MS = 5000;
const unsigned long REMOTE_PLAN_GRACE_MS = 1500;
const unsigned long WIFI_RECONNECT_INTERVAL_MS = 5000;
const unsigned long MQTT_RX_PULSE_MS = 120;

void writeLed(uint8_t pin, bool activeHigh, bool on) {
  digitalWrite(pin, on == activeHigh ? HIGH : LOW);
}

void setupIndicatorPins() {
  pinMode(LED_WIFI_PIN, OUTPUT);
  pinMode(LED_MQTT_PIN, OUTPUT);
  pinMode(LED_LOCAL_PIN, OUTPUT);
  pinMode(LED_PLAN_PIN, OUTPUT);
  writeLed(LED_WIFI_PIN, STATUS_LED_ACTIVE_HIGH, false);
  writeLed(LED_MQTT_PIN, STATUS_LED_ACTIVE_HIGH, false);
  writeLed(LED_LOCAL_PIN, STATUS_LED_ACTIVE_HIGH, false);
  writeLed(LED_PLAN_PIN, STATUS_LED_ACTIVE_HIGH, false);
}

bool blinkEvery(unsigned long periodMs) {
  return (millis() / periodMs) % 2 == 0;
}

bool doublePulsePattern() {
  const unsigned long slot = millis() % 1200;
  return slot < 120 || (slot >= 240 && slot < 360);
}

void markMqttRx() {
  mqttRxPulseUntilMs = millis() + MQTT_RX_PULSE_MS;
}

void updateStatusIndicators() {
  const bool wifiOk = WiFi.status() == WL_CONNECTED;
  const bool mqttOk = mqttClient.connected();

  writeLed(LED_WIFI_PIN, STATUS_LED_ACTIVE_HIGH, wifiOk || blinkEvery(150));

  bool mqttLedOn = mqttOk || (wifiOk && blinkEvery(400));
  if (millis() < mqttRxPulseUntilMs) {
    mqttLedOn = false;  
  }
  writeLed(LED_MQTT_PIN, STATUS_LED_ACTIVE_HIGH, mqttLedOn);

  writeLed(LED_LOCAL_PIN, STATUS_LED_ACTIVE_HIGH, fallbackActive);

  bool planLedOn = false;
  if (pendingRemotePlan.active) {
    planLedOn = doublePulsePattern();
  } else if (remotePlanActive) {
    planLedOn = true;
  }
  writeLed(LED_PLAN_PIN, STATUS_LED_ACTIVE_HIGH, planLedOn);
}

void setGroupColor(uint8_t groupId, const char *color) {
  if (groupId < 1 || groupId > 4) return;

  const LightPins pins = GROUP_PINS[groupId - 1];
  digitalWrite(pins.red, LOW);
  digitalWrite(pins.yellow, LOW);
  digitalWrite(pins.green, LOW);

  if (strcmp(color, "red") == 0)        digitalWrite(pins.red, HIGH);
  else if (strcmp(color, "yellow") == 0) digitalWrite(pins.yellow, HIGH);
  else if (strcmp(color, "green") == 0)  digitalWrite(pins.green, HIGH);
}

void setAllOff() {
  for (uint8_t i = 1; i <= 4; i++) {
    const LightPins pins = GROUP_PINS[i - 1];
    digitalWrite(pins.red, LOW);
    digitalWrite(pins.yellow, LOW);
    digitalWrite(pins.green, LOW);
  }
}

void applyRemotePlanOutputs(const char colors[4][8]) {
  for (uint8_t i = 1; i <= 4; i++) {
    setGroupColor(i, colors[i - 1]);
  }
}

void clearPendingRemotePlan() {
  pendingRemotePlan.active = false;
  pendingRemotePlan.sentWallMs = 0;
  pendingRemotePlan.adjustedRemainingMs = 0;
  pendingRemotePlan.abruptTransition = false;
  pendingRemotePlan.planId[0] = '\0';
  for (uint8_t i = 0; i < 4; i++) {
    strcpy(pendingRemotePlan.colors[i], "red");
  }
}

int64_t currentEpochMs() {
  struct timeval tv;
  if (gettimeofday(&tv, nullptr) != 0) return 0;
  if (tv.tv_sec < 1700000000) return 0; // Đảm bảo thời gian hợp lệ (> năm 2023)
  return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

bool applyPendingRemotePlanIfValid() {
  if (!pendingRemotePlan.active) return false;

  const unsigned long now = millis();
  const int64_t ageMs = (int64_t)(now - lastPayloadMs);
  const int64_t remainingAtBoundary = pendingRemotePlan.adjustedRemainingMs - ageMs;
  
  if (remainingAtBoundary <= 0) {
    Serial.println("Dropped expired pending remote plan");
    clearPendingRemotePlan();
    return false;
  }

  applyRemotePlanOutputs(pendingRemotePlan.colors);

  remotePlanActive = true;
  remotePlanEndMs = now + (unsigned long)remainingAtBoundary;
  lastAcceptedSentWallMs = pendingRemotePlan.sentWallMs;
  
  strncpy(currentPlanId, pendingRemotePlan.planId, sizeof(currentPlanId) - 1);
  currentPlanId[sizeof(currentPlanId) - 1] = '\0';
  
  fallbackActive = false;
  lastPayloadMs = now; // Khôi phục mốc nhận tin để tránh loop kế tiếp kích hoạt Failsafe nhầm

  Serial.print("Applied pending remote plan at fallback cycle boundary: ");
  Serial.println(currentPlanId);

  clearPendingRemotePlan();
  return true;
}

void setFallbackPhaseOutputs() {
  const bool nsActive = fallbackPhase == 0 || fallbackPhase == 1;
  const char *activeColor = (fallbackPhase == 1 || fallbackPhase == 3) ? "yellow" : "green";

  setGroupColor(1, nsActive ? activeColor : "red");
  setGroupColor(3, nsActive ? activeColor : "red");
  setGroupColor(2, nsActive ? "red" : activeColor);
  setGroupColor(4, nsActive ? "red" : activeColor);
}

void runFixedFallbackCycle() {
  const unsigned long now = millis();

  if (!fallbackActive) {
    fallbackActive = true;
    fallbackPhase = 0;
    fallbackPhaseStartMs = now;
    Serial.println("Failsafe: fixed local cycle initiated");
    setFallbackPhaseOutputs();
    return;
  }

  const unsigned long elapsed = now - fallbackPhaseStartMs;
  const unsigned long target = (fallbackPhase == 1 || fallbackPhase == 3)
    ? FALLBACK_YELLOW_MS
    : FALLBACK_GREEN_MS;

  if (elapsed >= target) {
    fallbackPhase = (fallbackPhase + 1) % 4;
    fallbackPhaseStartMs = now;
    
    // Nếu hết chu kỳ cũ và có gói cấu hình từ xa đang đợi, áp dụng ngay
    if (fallbackPhase == 0 && applyPendingRemotePlanIfValid()) {
      return;
    }
    setFallbackPhaseOutputs();
  }
}

// Thay thế hàm syncClock cũ bằng Non-blocking logic
void startClockSync() {
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  timeSyncRequested = true;
  Serial.println("NTP Time sync requested...");
}

void handleMqttMessage(char *topic, byte *payload, unsigned int length) {
  // Khuyến nghị: Thay StaticJsonDocument bằng JsonDocument nếu dùng ArduinoJson V7
  JsonDocument doc; 
  DeserializationError error = deserializeJson(doc, payload, length);
  if (error) {
    Serial.print("JSON parse failed: ");
    Serial.println(error.c_str());
    return;
  }

  JsonArray groups = doc["groups"].as<JsonArray>();
  if (groups.isNull()) {
    Serial.println("Payload missing groups[]");
    return;
  }

  char receivedColors[4][8] = {"red", "red", "red", "red"};
  for (JsonObject group : groups) {
    uint8_t id = group["id"] | 0;
    const char *color = group["color"] | "red";
    if (id >= 1 && id <= 4) {
      strncpy(receivedColors[id - 1], color, sizeof(receivedColors[id - 1]) - 1);
      receivedColors[id - 1][sizeof(receivedColors[id - 1]) - 1] = '\0';
    }
  }

  int64_t sentWallMs = doc["sent_wall_ms"] | 0;
  int64_t remainingMs = doc["remaining_ms"] | 0;
  const char *planId = doc["plan_id"] | "";
  bool abruptTransition = doc["abrupt_transition"] | false;

  int64_t latestKnownSentWallMs = lastAcceptedSentWallMs;
  if (pendingRemotePlan.active && pendingRemotePlan.sentWallMs > latestKnownSentWallMs) {
    latestKnownSentWallMs = pendingRemotePlan.sentWallMs;
  }

  if (sentWallMs > 0 && sentWallMs <= latestKnownSentWallMs) {
    Serial.println("Ignored stale MQTT message");
    return;
  }

  int64_t delayMs = 0;
  int64_t nowEpoch = currentEpochMs();
  if (nowEpoch > 0 && sentWallMs > 0) {
    delayMs = nowEpoch - sentWallMs;
    if (delayMs < 0) delayMs = 0;
    else if (delayMs > 30000) delayMs = 30000;
  }

  int64_t adjustedRemainingMs = remainingMs - delayMs;
  if (adjustedRemainingMs <= 0) {
    Serial.println("Ignored expired MQTT plan");
    return;
  }

  unsigned long now = millis();
  lastPayloadMs = now;

  // Nếu đang chạy chế độ Local fallback và nhận được tin nhắn bắt buộc chuyển đổi mượt (Smooth Transition)
  if (fallbackActive && !abruptTransition) {
    pendingRemotePlan.active = true;
    pendingRemotePlan.sentWallMs = sentWallMs;
    pendingRemotePlan.adjustedRemainingMs = adjustedRemainingMs;
    pendingRemotePlan.abruptTransition = abruptTransition;
    strncpy(pendingRemotePlan.planId, planId, sizeof(pendingRemotePlan.planId) - 1);
    pendingRemotePlan.planId[sizeof(pendingRemotePlan.planId) - 1] = '\0';
    for (uint8_t i = 0; i < 4; i++) {
      strncpy(pendingRemotePlan.colors[i], receivedColors[i], sizeof(pendingRemotePlan.colors[i]) - 1);
      pendingRemotePlan.colors[i][sizeof(pendingRemotePlan.colors[i]) - 1] = '\0';
    }

    Serial.print("Queued remote plan until fallback cycle boundary: ");
    Serial.println(pendingRemotePlan.planId);
    markMqttRx();
    return;
  }

  // Áp dụng trực tiếp nếu không ở chế độ fallback hoặc tin nhắn yêu cầu ngắt khẩn cấp (abruptTransition = true)
  applyRemotePlanOutputs(receivedColors);
  remotePlanActive = true;
  remotePlanEndMs = now + (unsigned long)adjustedRemainingMs;
  lastAcceptedSentWallMs = sentWallMs;
  strncpy(currentPlanId, planId, sizeof(currentPlanId) - 1);
  currentPlanId[sizeof(currentPlanId) - 1] = '\0';
  fallbackActive = false;

  Serial.print("MQTT plan applied directly: ");
  Serial.println(currentPlanId);
  markMqttRx();
}

void startWiFiConnect() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  wifiConnectStarted = true;
  lastWiFiAttemptMs = millis();
  Serial.println("WiFi connect started");
}

void maintainWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    if (!wifiWasConnected) {
      wifiWasConnected = true;
      Serial.print("WiFi connected: ");
      Serial.println(WiFi.localIP());
      startClockSync();
    }
    return;
  }

  wifiWasConnected = false;
  unsigned long now = millis();
  if (!wifiConnectStarted || now - lastWiFiAttemptMs >= WIFI_RECONNECT_INTERVAL_MS) {
    startWiFiConnect();
  }
}

bool connectMqtt() {
  if (mqttClient.connected()) return true;
  if (WiFi.status() != WL_CONNECTED) return false;

  String clientId = "esp32-traffic-light-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  Serial.print("Connecting MQTT...");

  bool ok = mqttClient.connect(clientId.c_str(), MQTT_USERNAME, MQTT_PASSWORD);

  if (!ok) {
    Serial.print("failed, rc=");
    Serial.println(mqttClient.state());
    return false;
  }

  Serial.println("connected");
  mqttClient.subscribe(MQTT_TOPIC);
  return true;
}

void setupPins() {
  for (uint8_t i = 0; i < 4; i++) {
    pinMode(GROUP_PINS[i].red, OUTPUT);
    pinMode(GROUP_PINS[i].yellow, OUTPUT);
    pinMode(GROUP_PINS[i].green, OUTPUT);
  }
  setAllOff();
}

void setup() {
  Serial.begin(115200);
  delay(200);

  setupIndicatorPins();
  clearPendingRemotePlan();
  setupPins();
  startWiFiConnect();

  // Chờ WiFi tối đa 3 giây lúc khởi động, nếu không được thì chạy tiếp (tránh treo cứng)
  unsigned long wifiWaitStartMs = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - wifiWaitStartMs < 3000) {
    delay(100);
  }

  if (WiFi.status() == WL_CONNECTED) {
    wifiWasConnected = true;
    startClockSync();
  }

  secureClient.setInsecure();
  mqttClient.setServer(MQTT_HOST, MQTT_PORT);
  mqttClient.setCallback(handleMqttMessage);
  mqttClient.setBufferSize(1536);

  if (WiFi.status() == WL_CONNECTED) {
    connectMqtt();
  }
  lastPayloadMs = millis();
}

void loop() {
  maintainWiFi();

  // Kiểm tra trạng thái đồng bộ thời gian bất đồng bộ
  if (timeSyncRequested && WiFi.status() == WL_CONNECTED) {
    if (millis() - lastTimeSyncCheckMs >= 500) {
      lastTimeSyncCheckMs = millis();
      if (currentEpochMs() > 0) {
        Serial.println("NTP Time Synchronized successfully.");
        timeSyncRequested = false; // Hoàn thành đồng bộ
      }
    }
  }

  if (WiFi.status() != WL_CONNECTED) {
    mqttClient.disconnect();
  } else if (!mqttClient.connected()) {
    unsigned long now = millis();
    if (now - lastReconnectAttemptMs >= MQTT_RECONNECT_INTERVAL_MS) {
      lastReconnectAttemptMs = now;
      connectMqtt();
    }
  } else {
    mqttClient.loop();
  }

  unsigned long now = millis();
  
  // Sửa lỗi so sánh tràn số (Safe Time Check)
  bool remotePlanExpired = false;
  if (remotePlanActive) {
    if (now >= remotePlanEndMs) {
      remotePlanExpired = (now - remotePlanEndMs) > REMOTE_PLAN_GRACE_MS;
    }
  }

  const bool timedOutWithoutPlan = !remotePlanActive && (now - lastPayloadMs > FAILSAFE_TIMEOUT_MS);

  if (fallbackActive) {
    runFixedFallbackCycle();
  } else if (timedOutWithoutPlan || remotePlanExpired) {
    remotePlanActive = false;
    runFixedFallbackCycle();
  }

  updateStatusIndicators();
}