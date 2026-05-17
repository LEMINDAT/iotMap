"""
camera_detector.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Module giám sát giao thông — đọc dữ liệu từ TraCI.

Cung cấp hàm get_all_tls_stats() để lấy thống kê
giao thông từ tất cả nút đèn trong mạng SUMO.

Có thể chạy standalone để xem dashboard real-time:
  python camera_detector.py

Hoặc import vào main_controller.py nếu cần.
"""

import traci
import time


# ─── TRAFFIC DATA FROM TRACI ────────────────────────────────────────────────

def get_lane_stats(lane_id):
    """
    Lấy thống kê 1 làn đường từ TraCI.
    Trả về dict: queue, wait, max_wait, vehicles, speed
    """
    try:
        halting  = traci.lane.getLastStepHaltingNumber(lane_id)
        wait     = traci.lane.getWaitingTime(lane_id)
        vehicles = traci.lane.getLastStepVehicleNumber(lane_id)
        speed    = traci.lane.getLastStepMeanSpeed(lane_id)
        occupancy = traci.lane.getLastStepOccupancy(lane_id)
        return {
            "queue":     halting,
            "wait":      wait,
            "max_wait":  wait,  # TraCI trả waitingTime tổng, dùng làm proxy
            "vehicles":  vehicles,
            "speed":     speed,
            "occupancy": occupancy,
        }
    except traci.TraCIException:
        return {
            "queue": 0, "wait": 0, "max_wait": 0,
            "vehicles": 0, "speed": 0, "occupancy": 0,
        }


def get_tls_stats(tls_id):
    """
    Lấy thống kê cho 1 nút đèn: gộp theo từng pha xanh.
    Trả về:
      {
        phase_idx: {
            "queue": int,
            "wait": float,
            "max_wait": float,
            "vehicles": int,
            "is_green": bool
        }
      }
    """
    logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]
    controlled = traci.trafficlight.getControlledLanes(tls_id)
    current_phase = traci.trafficlight.getPhase(tls_id)

    result = {}
    for pi, phase in enumerate(logic.phases):
        if 'G' not in phase.state and 'g' not in phase.state:
            continue  # bỏ pha vàng / đỏ

        total_q = 0
        total_w = 0.0
        max_w   = 0.0
        total_v = 0
        seen_lanes = set()

        for i, s in enumerate(phase.state):
            if s in ('G', 'g') and i < len(controlled):
                lane = controlled[i]
                if lane in seen_lanes:
                    continue
                seen_lanes.add(lane)
                ls = get_lane_stats(lane)
                total_q += ls["queue"]
                total_w += ls["wait"]
                max_w    = max(max_w, ls["max_wait"])
                total_v += ls["vehicles"]

        result[pi] = {
            "queue":    total_q,
            "wait":     total_w,
            "max_wait": max_w,
            "vehicles": total_v,
            "is_green": (pi == current_phase),
        }

    return result


def get_all_tls_stats():
    """Lấy stats cho toàn bộ nút đèn trong mạng."""
    all_stats = {}
    for tls_id in traci.trafficlight.getIDList():
        all_stats[tls_id] = get_tls_stats(tls_id)
    return all_stats


def get_network_summary():
    """Tổng quan toàn mạng."""
    total = traci.vehicle.getIDCount()
    if total == 0:
        return {"total": 0, "waiting": 0, "avg_speed": 0, "pct_waiting": 0}

    all_ids = traci.vehicle.getIDList()
    waiting = sum(1 for vid in all_ids if traci.vehicle.getSpeed(vid) < 0.1)
    avg_speed = sum(traci.vehicle.getSpeed(vid) for vid in all_ids) / total

    return {
        "total":       total,
        "waiting":     waiting,
        "avg_speed":   avg_speed,
        "pct_waiting": waiting / total * 100,
    }


# ─── STANDALONE DASHBOARD ───────────────────────────────────────────────────

def print_dashboard():
    """In dashboard giao thông real-time."""
    sim_time = traci.simulation.getTime()
    all_stats = get_all_tls_stats()
    summary   = get_network_summary()

    print(f"\n{'━'*65}")
    print(f"  ⏱  SimTime: {sim_time:.0f}s")
    print(f"  {'TLS':<7} {'Pha':<5} {'XeĐứng':<8} {'Wait':<8} "
          f"{'MaxWait':<8} {'TổngXe':<8} {'':>5}")
    print(f"  {'─'*7} {'─'*5} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*5}")

    for tls_id, phases in all_stats.items():
        for pi, p in phases.items():
            marker = " ◀🟢" if p["is_green"] else ""
            print(f"  {tls_id:<7} P{pi:<4} {p['queue']:<8} "
                  f"{p['wait']:<8.1f} {p['max_wait']:<8.1f} "
                  f"{p['vehicles']:<8}{marker}")

    print(f"\n  🚗 Tổng: {summary['total']} xe | "
          f"Chờ: {summary['waiting']} ({summary['pct_waiting']:.0f}%) | "
          f"Tốc độ TB: {summary['avg_speed']:.1f} m/s")


def run_standalone():
    """
    Chạy độc lập: mở SUMO + hiển thị dashboard.
    Không điều khiển đèn — chỉ giám sát.
    """
    print("━" * 55)
    print("  Camera Detector — Giám sát giao thông")
    print("  (Chỉ giám sát, không điều khiển đèn)")
    print("━" * 55)

    sumo_cmd = ["sumo-gui", "-c", "sim.sumocfg", "--start"]
    traci.start(sumo_cmd)
    print("✅ Kết nối TraCI")

    step = 0
    while traci.simulation.getMinExpectedNumber() > 0:
        traci.simulationStep()
        step += 1

        if step % 30 == 0:
            print_dashboard()

    traci.close()
    print("\n✅ Kết thúc!")


if __name__ == "__main__":
    try:
        run_standalone()
    except KeyboardInterrupt:
        print("\n⛔ Dừng")
        try: traci.close()
        except: pass