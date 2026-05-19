import os
import subprocess
import sys
import xml.etree.ElementTree as ET

SUMO_HOME = os.environ.get("SUMO_HOME")
net = "map.net.xml"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# ===== CONFIG - PEAK & OFF-PEAK HOURS =====
# Peak hours: 7-9 AM, 5-7 PM
# Off-peak: remaining hours

# Thời gian (giây) - ví dụ: 0 = bắt đầu, 3600 = 1 giờ
PEAK_HOURS = [
    (0, 3600),        # 0:00 - 1:00 (1 giờ đầu)
    (11400, 15000)    # 3:10 - 4:10 (1 giờ cuối)
]
END_TIME = 15000      # ~4.17 giờ (15000 giây)

# Tần suất (càng lớn càng ít xe)
p_motor_peak = 3      # xe máy (peak - cao điểm)
p_motor_offpeak = 8   # xe máy (off-peak - không cao điểm)
p_car_peak = 6        # ô tô (peak)
p_car_offpeak =  16    # ô tô (off-peak)

# Scale traffic volume by map width. These base periods are tuned for the
# current map.net.xml width: 266.39 - 24.32 = 242.07m.
BASE_MAP_WIDTH = 242.07
MIN_PERIOD = 0.2
# =============================================

def get_map_width(net_file):
    """Read map width from SUMO convBoundary: x_min,y_min,x_max,y_max."""
    root = ET.parse(net_file).getroot()
    location = root.find("location")
    if location is None:
        raise ValueError(f"Cannot find <location> in {net_file}")

    conv_boundary = location.get("convBoundary")
    if not conv_boundary:
        raise ValueError(f"Cannot find convBoundary in {net_file}")

    try:
        x_min, _, x_max, _ = [float(value) for value in conv_boundary.split(",")]
    except ValueError as exc:
        raise ValueError(f"Invalid convBoundary in {net_file}: {conv_boundary}") from exc

    width = x_max - x_min
    if width <= 0:
        raise ValueError(f"Invalid map width from convBoundary in {net_file}: {width}")
    return width

def scale_period(base_period, scale):
    """Smaller period means more vehicles; wider maps generate more vehicles."""
    return max(base_period / scale, MIN_PERIOD)

map_width = get_map_width(net)
traffic_scale = map_width / BASE_MAP_WIDTH

print("=== Traffic scale by map width ===")
print(f"Map file: {net}")
print(f"Map width: {map_width:.2f} m")
print(f"Base width: {BASE_MAP_WIDTH:.2f} m")
print(f"Traffic scale: {traffic_scale:.3f}x")

def is_peak_hour(start_time, end_time):
    """Kiểm tra nếu thời gian nằm trong giờ cao điểm"""
    for peak_start, peak_end in PEAK_HOURS:
        if start_time >= peak_start and end_time <= peak_end:
            return True
    return False

def generate_trips_with_periods(vtype, filename, p_peak, p_offpeak):
    """Tạo trips với hệ số khác nhau cho peak/off-peak"""
    all_trips = []
    scaled_peak = scale_period(p_peak, traffic_scale)
    scaled_offpeak = scale_period(p_offpeak, traffic_scale)

    print(f"{vtype}: peak period {p_peak} -> {scaled_peak:.3f}s")
    print(f"{vtype}: off-peak period {p_offpeak} -> {scaled_offpeak:.3f}s")
    
    # Generate peak hours
    for start_time, end_time in PEAK_HOURS:
        subprocess.run([
            "python", "randomTrips_custom.py",
            "-n", net,
            "-o", f"temp_peak_{filename}",
            "--vehicle-class", vtype,
            "-p", str(scaled_peak),
            "-b", str(start_time),
            "-e", str(end_time)
        ])
        
        tree = ET.parse(f"temp_peak_{filename}")
        all_trips.extend(tree.getroot())
    
    # Generate off-peak hours
    offpeak_segments = [
        (0, PEAK_HOURS[0][0]),
        (PEAK_HOURS[0][1], PEAK_HOURS[1][0]),
        (PEAK_HOURS[1][1], END_TIME)
    ]
    
    for start_time, end_time in offpeak_segments:
        if start_time < end_time:
            subprocess.run([
                "python", "randomTrips_custom.py",
                "-n", net,
                "-o", f"temp_offpeak_{filename}",
                "--vehicle-class", vtype,
                "-p", str(scaled_offpeak),
                "-b", str(start_time),
                "-e", str(end_time)
            ])
            
            tree = ET.parse(f"temp_offpeak_{filename}")
            all_trips.extend(tree.getroot())
    
    # Write combined
    root = ET.Element("routes")
    for trip in all_trips:
        root.append(trip)
    ET.ElementTree(root).write(filename)

# 1. tạo trips riêng với peak/off-peak
generate_trips_with_periods("motorcycle", "motor.xml", p_motor_peak, p_motor_offpeak)
generate_trips_with_periods("passenger", "car.xml", p_car_peak, p_car_offpeak)

print("✅ Tạo trips với giờ cao điểm (peak) & không cao điểm (off-peak)")

# 2. gộp file
all_trips = []
counter = 0

# map file → type
file_type = {
    "motor.xml": "motorcycle",
    "car.xml": "passenger"
}

for file, vtype in file_type.items():
    tree = ET.parse(file)
    for trip in tree.getroot():
        trip.set("id", str(counter))       # fix ID
        trip.set("type", vtype)            # 🔥 gán type chuẩn
        all_trips.append(trip)
        counter += 1

# sort theo thời gian
all_trips.sort(key=lambda x: float(x.get("depart", 0)))

# Phân tích trips: peak vs off-peak
peak_trips = 0
offpeak_trips = 0
for trip in all_trips:
    depart = float(trip.get("depart", 0))
    if is_peak_hour(depart, depart):
        peak_trips += 1
    else:
        offpeak_trips += 1

print(f"📊 Thống kê trips:")
print(f"   Giờ cao điểm: {peak_trips} chuyến")
print(f"   Giờ không cao điểm: {offpeak_trips} chuyến")
print(f"   Tổng cộng: {len(all_trips)} chuyến")

# tạo root
root = ET.Element("routes")

# 🔥 thêm vType ngay từ đầu
vtype_car = ET.Element("vType", {
    "id": "passenger",
    "vClass": "passenger",

    # Kích thước / động lực học
    "accel": "0.5",
    "decel": "4.5",
    "emergencyDecel": "9.0",

    "length": "4.5",
    "width": "1.8",
    "minGap": "1.8",

    # Bám xe: an toàn hơn để tránh đâm đuôi
    "tau": "1.3",
    "sigma": "0.2",
    "speedFactor": "1.00",
    "speedDev": "0.05",

    # Chuyển làn: vẫn đổi làn nhưng không chen nguy hiểm
    "lcStrategic": "1.0",
    "lcCooperative": "0.8",
    "lcSpeedGain": "1.1",
    "lcKeepRight": "0.2",
    "lcAssertive": "0.8",
    "lcSigma": "0.1",

    # Nút giao: bỏ hẳn hành vi cố vượt/ép xe
    "jmIgnoreFoeProb": "0.0",
    "jmIgnoreFoeSpeed": "0",
    "jmTimegapMinor": "1.5",
    "impatience": "0.0",

    "color": "0,0,255"
})


vtype_motor = ET.Element("vType", {
    "id": "motorcycle",
    "vClass": "motorcycle",

    # Kích thước / động lực học
    "accel": "1.0",
    "decel": "4.5",
    "emergencyDecel": "8.0",

    "length": "1.8",
    "width": "0.6",
    "minGap": "1.2",

    # Bám xe: tăng an toàn nhưng vẫn hợp xe máy
    "tau": "1.2",
    "sigma": "0.25",
    "speedFactor": "1.00",
    "speedDev": "0.08",

    # Lách làn nhẹ: vẫn linh hoạt nhưng không ép sát
    "lcStrategic": "0.8",
    "lcCooperative": "0.7",
    "lcSpeedGain": "1.2",
    "lcKeepRight": "0",
    "lcAssertive": "0.9",
    "lcSigma": "0.15",

    # Nút giao: 
    "jmIgnoreFoeProb": "0.0",
    "jmIgnoreFoeSpeed": "0",
    "jmTimegapMinor": "1.5",
    "impatience": "0.0",

    "guiShape": "motorcycle",
    "color": "255,0,0"
})

root.append(vtype_car)
root.append(vtype_motor)

# thêm trips
for trip in all_trips:
    root.append(trip)

ET.ElementTree(root).write("trips.xml")

print("✅ trips.xml chuẩn (ID + sort + vType)")

# 3. convert sang route
subprocess.run([
    "duarouter",
    "-n", net,
    "-r", "trips.xml",
    "-o", "cars.rou.xml",
    "--remove-loops",
    "--routing-algorithm", "dijkstra"
])

print("🚗 Hoàn thành cars.rou.xml")

# Clean up temp files
import glob
for temp_file in glob.glob("temp_*.xml"):
    try:
        os.remove(temp_file)
    except:
        pass

print("\n✅ Hoàn thành mô phỏng giờ cao điểm & không cao điểm!")
