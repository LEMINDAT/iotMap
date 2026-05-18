# iotMap

## MQTT EMQX publisher

Install Python MQTT dependency:

```powershell
pip install paho-mqtt
```

Run SUMO controller and publish one intersection to EMQX over TLS:

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

Topic format:

```text
traffic/{area}/{intersection_id}/state
```

Examples:

```text
traffic/A/001/state
traffic/A/007/state
```

ESP32 Arduino code is in `esp32_traffic_light/`.
