# ESP32 Traffic Light MQTT Client

ESP32 subscribes to EMQX MQTT over TLS and controls 4 traffic-light groups.

## Libraries

Install these Arduino libraries:

- PubSubClient
- ArduinoJson

## Configure

Edit Wi-Fi and MQTT settings directly in `esp32_traffic_light.ino`:

```cpp
#define WIFI_SSID "your_wifi"
#define WIFI_PASSWORD "your_password"
#define MQTT_USERNAME "your_emqx_username"
#define MQTT_PASSWORD "your_emqx_password"
#define MQTT_TOPIC "traffic/A/001/state"
```

The sketch uses `secureClient.setInsecure()` for the first demo, so it connects to TLS port `8883` without embedding a root CA.

## Default GPIO Map

| Group | Red | Yellow | Green |
| --- | ---: | ---: | ---: |
| 1 | 25 | 26 | 27 |
| 2 | 32 | 33 | 14 |
| 3 | 23 | 22 | 21 |
| 4 | 19 | 18 | 17 |

Outputs are active-high. Use suitable transistor/MOSFET/relay driver hardware for real lamps.

## System Status LEDs

| LED | GPIO | Meaning |
| --- | ---: | --- |
| Wi-Fi | 2 | Solid when Wi-Fi is connected, fast blink while disconnected/retrying |
| MQTT | 5 | Solid when MQTT is connected, medium blink when Wi-Fi is up but MQTT is down, short off-pulse when a valid MQTT plan is received |
| Local | 4 | On while the fixed local fallback cycle is controlling the lamps |
| Plan | 16 | Solid while following a broker plan, double-pulse when a broker plan is queued until the next fallback cycle boundary |

## Matching Python Command

```powershell
python main_controller.py `
  --mqtt `
  --tls-id J105 `
  --area A `
  --intersection-id 001 `
  --mqtt-host gaccf6ca.ala.asia-southeast1.emqxsl.com `
  --mqtt-port 8883 `
  --mqtt-username "<username>" `
  --mqtt-password "<password>"
```

This publishes to:

```text
traffic/A/001/state
```

Use `traffic/A/007/state` for another intersection/device.
