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

MQTT message format: Payload JSON mẫu:

```json
{
  "area": "A",
  "intersection_id": "001",
  "tls_id": "J105",
  "sim_time": 128,
  "seq": 42,
  "sent_wall_ms": 1766112345678,
  "plan_id": "J105-3-42",
  "plan_seq": 3,
  "plan_reason": "webster",
  "phase_started_sim": 120.0,
  "phase_duration_seconds": 35.0,
  "planned_end_sim": 155.0,
  "remaining_ms": 27000,
  "abrupt_transition": false,
  "controller_state": "GREEN",
  "current_phase": 2,
  "remaining_seconds": 27,
  "groups": [
    { "id": 1, "color": "red", "remaining_seconds": 27 },
    { "id": 2, "color": "green", "remaining_seconds": 27 },
    { "id": 3, "color": "red", "remaining_seconds": 27 },
    { "id": 4, "color": "green", "remaining_seconds": 27 }
  ]
}
```

Bản thực tế khi gửi từ Python sẽ bị nén khoảng trắng, dạng như:

```json
{"area":"A","intersection_id":"001","tls_id":"J105","sim_time":128,"seq":42,"sent_wall_ms":1766112345678,"plan_id":"J105-3-42","plan_seq":3,"plan_reason":"webster","phase_started_sim":120.0,"phase_duration_seconds":35.0,"planned_end_sim":155.0,"remaining_ms":27000,"abrupt_transition":false,"controller_state":"GREEN","current_phase":2,"remaining_seconds":27,"groups":[{"id":1,"color":"red","remaining_seconds":27},{"id":2,"color":"green","remaining_seconds":27},{"id":3,"color":"red","remaining_seconds":27},{"id":4,"color":"green","remaining_seconds":27}]}
```

ESP32 hiện chỉ dùng các trường chính này:

`groups`: danh sách 4 cụm đèn. Mỗi phần tử có `id` từ `1..4` và `color` là `"red"`, `"yellow"`, hoặc `"green"`. ESP sẽ gọi `setGroupColor(id, color)` để bật đúng GPIO.

`sent_wall_ms`: thời điểm server gửi message, tính bằng epoch milliseconds. ESP dùng nó để bỏ qua message cũ và tính độ trễ mạng.

`remaining_ms`: thời gian còn lại của pha hiện tại. ESP trừ thêm độ trễ mạng rồi đặt `remotePlanEndMs`.

`plan_id`: mã plan hiện tại. ESP lưu vào `currentPlanId`, sau đó gửi lại trong telemetry.

`abrupt_transition`: nếu `false` và ESP đang chạy fallback local, ESP sẽ xếp plan vào hàng đợi để áp dụng ở ranh giới chu kỳ fallback. Nếu `true`, ESP áp dụng ngay.

Các trường như `area`, `intersection_id`, `tls_id`, `seq`, `sim_time`, `plan_reason`, `current_phase`, `remaining_seconds` chủ yếu phục vụ dashboard/debug; sketch ESP32 hiện không dựa vào chúng để điều khiển đèn.