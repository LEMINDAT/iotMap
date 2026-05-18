"""
main_controller.py
===================================================
Thuat toan dieu khien den giao thong:

  TINH LUU LUONG — PCE (Passenger Car Equivalent)
    xe may = 0.5 PCE  |  o to = 1.0 PCE
    flow_pce = moto*0.5 + car*1.0

  QUYET DINH THOI GIAN XANH — Webster's Formula (1958)
    Tieu chuan ky thuat giao thong duong bo quoc te.
    Tinh thoi gian xanh TY LE voi luu luong tung huong:

      y_i  = flow_i / S   (S = 1800 PCE/h = bao hoa)
      C*   = (1.5*L + 5) / (1 - sum(y_i))   chu ky toi uu
      g_i  = (C* - L) * y_i / sum(y_i)      thoi gian xanh toi uu

    Vi du:
      Huong NS: 400 PCE/h  -> y=0.22
      Huong EW: 600 PCE/h  -> y=0.33
      C* = 60s
      g_NS = 60 * 0.22/0.55 = 24s
      g_EW = 60 * 0.33/0.55 = 36s

  DEN VANG 3 GIAY — bat buoc truoc khi doi xanh
    GREEN --(het g_i)--> YELLOW --(3s)--> GREEN (pha tiep)

Cach chay:
  python main_controller.py
  python main_controller.py --mqtt --tls-id J105 --area A --intersection-id 001 \
    --mqtt-host gaccf6ca.ala.asia-southeast1.emqxsl.com --mqtt-port 8883 \
    --mqtt-username "<username>" --mqtt-password "<password>"
"""

import argparse
import json
import os
import ssl
import sys
import time

traci = None

# ─── CONFIG ──────────────────────────────────────────────────────────────────

SUMO_BINARY = "sumo-gui"
SUMO_CONFIG = "sim.sumocfg"

YELLOW_TIME = 3     # giay den vang bat buoc

# PCE — Passenger Car Equivalent
PCE_MOTORCYCLE = 0.5
PCE_CAR        = 1.0

# Webster parameters
SATURATION_FLOW = 1800   # PCE/gio — luu luong bao hoa (tieu chuan)
LOST_TIME       = 4      # giay mat mat moi pha (thoi gian khoi dong + giai toa)
MIN_GREEN       = 10     # giay — gioi han duoi cho thoi gian xanh
MAX_GREEN       = 90     # giay — gioi han tren
MIN_CYCLE       = 30     # giay — chu ky toi thieu
MAX_CYCLE       = 120    # giay — chu ky toi da

# Gridlock
GRIDLOCK_THRESHOLD = 0.70
GRIDLOCK_MIN_GREEN = 20

DASHBOARD_INTERVAL = 20

# MQTT defaults
MQTT_HOST = "gaccf6ca.ala.asia-southeast1.emqxsl.com"
MQTT_PORT = 8883
MQTT_AREA = "A"
MQTT_INTERSECTION_ID = "001"
MQTT_PUBLISH_INTERVAL = 1
DEFAULT_GROUP_MAP = "1:0,3:0,2:1,4:1"
MQTT_REALTIME = True
MQTT_REALTIME_FACTOR = 1.0
SAFE_SHORTEN_SECONDS = 5

# ─── STATE MACHINE ───────────────────────────────────────────────────────────

STATE_GREEN  = "GREEN"
STATE_YELLOW = "YELLOW"


# ─── MQTT HELPERS ────────────────────────────────────────────────────────────

def parse_group_map(text):
    """
    Doc mapping cum den -> thu tu pha xanh.

    Vi du: "1:0,3:0,2:1,4:1"
      - cum 1 va 3 theo pha xanh thu 0
      - cum 2 va 4 theo pha xanh thu 1
    """
    result = {}
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue

        group, sep, phase = item.partition(":")
        if not sep:
            raise ValueError("Sai group-map '%s'. Dung dinh dang group:phase" % item)

        try:
            group_id = int(group.strip())
            phase_idx = int(phase.strip())
        except ValueError:
            raise ValueError("group-map chi nhan so nguyen: '%s'" % item)

        result[group_id] = phase_idx

    if not result:
        raise ValueError("group-map khong duoc rong")
    return result


def load_dotenv(path=".env"):
    """Nap key=value don gian tu .env neu bien moi truong chua co."""
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def now_wall_ms():
    return int(time.time() * 1000)


def setup_traci_import():
    """Cho phep import traci tu SUMO_HOME/tools neu SUMO da cai ngoai PATH."""
    sumo_home = os.environ.get("SUMO_HOME")
    if sumo_home:
        tools_path = os.path.join(sumo_home, "tools")
        if os.path.isdir(tools_path) and tools_path not in sys.path:
            sys.path.append(tools_path)


def validate_group_map(group_map, controller):
    max_phase_idx = len(controller.green_phases) - 1
    bad = sorted(set(i for i in group_map.values() if i < 0 or i > max_phase_idx))
    if bad:
        raise ValueError(
            "group-map tro toi pha xanh khong ton tai %s. TLS %s chi co index 0..%d"
            % (bad, controller.tls_id, max_phase_idx)
        )


class MqttPublisher:
    """MQTT publisher dung TLS cho EMQX/HiveMQ Cloud."""

    def __init__(self, host, port, username, password, client_id, topic):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.client_id = client_id
        self.topic = topic
        self.client = None
        self.connected = False

    def connect(self):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise RuntimeError(
                "Chua cai paho-mqtt. Hay chay: pip install paho-mqtt"
            )

        self.client = mqtt.Client(client_id=self.client_id)
        if self.username:
            self.client.username_pw_set(self.username, self.password or "")

        self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self.client.connect(self.host, self.port, keepalive=60)
        self.client.loop_start()
        self.connected = True
        print("[MQTT] Connected %s:%d | topic=%s" % (
            self.host, self.port, self.topic))

    def publish(self, payload):
        if not self.client:
            return

        try:
            info = self.client.publish(
                self.topic,
                json.dumps(payload, separators=(",", ":")),
                qos=0,
                retain=False,
            )
            if info.rc != 0:
                print("[MQTT][WARN] publish rc=%s" % info.rc)
        except Exception as e:
            self.connected = False
            print("[MQTT][WARN] publish failed: %s" % e)

    def close(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()


# ─── PCE ─────────────────────────────────────────────────────────────────────

def get_edge_pce(edge_id):
    """
    Dem xe may va o to tren 1 edge, tinh PCE.
    """
    moto = car = halting = 0
    max_wait = 0.0
    try:
        for vid in traci.edge.getLastStepVehicleIDs(edge_id):
            try:
                vtype = traci.vehicle.getTypeID(vid).lower()
                speed = traci.vehicle.getSpeed(vid)
                wait  = traci.vehicle.getWaitingTime(vid)
                if "motorcycle" in vtype or "moto" in vtype:
                    moto += 1
                else:
                    car += 1
                if speed < 0.3:
                    halting += 1
                if wait > max_wait:
                    max_wait = wait
            except traci.TraCIException:
                car += 1
    except traci.TraCIException:
        pass

    pce = moto * PCE_MOTORCYCLE + car * PCE_CAR
    return {
        "pce":      pce,
        "moto":     moto,
        "car":      car,
        "halting":  halting,
        "max_wait": max_wait,
    }


def get_phase_pce(edges):
    """Gop PCE cua nhieu edges thanh 1 pha."""
    total_pce = total_moto = total_car = total_halt = 0
    max_wait  = 0.0
    for edge in edges:
        d = get_edge_pce(edge)
        total_pce  += d["pce"]
        total_moto += d["moto"]
        total_car  += d["car"]
        total_halt += d["halting"]
        if d["max_wait"] > max_wait:
            max_wait = d["max_wait"]
    vehicles   = total_moto + total_car
    halt_ratio = total_halt / max(vehicles, 1)
    return {
        "pce":        total_pce,
        "moto":       total_moto,
        "car":        total_car,
        "vehicles":   vehicles,
        "halting":    total_halt,
        "halt_ratio": halt_ratio,
        "max_wait":   max_wait,
    }


# ─── WEBSTER'S FORMULA ───────────────────────────────────────────────────────

def webster(phase_flows_pce_per_hour, n_phases):
    """
    Tinh thoi gian xanh toi uu theo Webster (1958).

    Tham so:
      phase_flows_pce_per_hour : list luu luong tung pha (PCE/gio)
      n_phases                 : so pha

    Tra ve:
      green_times : list thoi gian xanh (giay) cho tung pha
      cycle       : chu ky toi uu (giay)

    Cong thuc:
      y_i = q_i / S              (ti so luu luong/bao hoa)
      L   = n_phases * LOST_TIME (tong thoi gian mat mat)
      C*  = (1.5*L + 5) / (1 - sum(y))   chu ky Webster
      g_i = (C* - L) * (y_i / sum(y))    thoi gian xanh
    """
    S = SATURATION_FLOW

    # Tinh y_i cho tung pha
    y = [q / S for q in phase_flows_pce_per_hour]
    sum_y = sum(y)

    # Tong thoi gian mat mat
    L = n_phases * LOST_TIME

    # Tranh chia cho 0 hoac am (qua tai)
    if sum_y <= 0:
        # Khong co xe: chia deu thoi gian
        g = max(MIN_GREEN, MIN_CYCLE // n_phases)
        return [g] * n_phases, g * n_phases

    if sum_y >= 0.95:
        # Qua tai (> 95% bao hoa): dung MAX_GREEN
        return [MAX_GREEN] * n_phases, MAX_GREEN * n_phases

    # Chu ky toi uu Webster
    cycle = (1.5 * L + 5) / (1 - sum_y)
    cycle = max(MIN_CYCLE, min(MAX_CYCLE, cycle))

    # Thoi gian xanh tung pha
    green_times = []
    for yi in y:
        if yi <= 0:
            green_times.append(MIN_GREEN)
        else:
            gi = (cycle - L) * (yi / sum_y)
            gi = max(MIN_GREEN, min(MAX_GREEN, gi))
            green_times.append(gi)

    return green_times, cycle


# ─── CONTROLLER ──────────────────────────────────────────────────────────────

class DensityController:
    """
    Dieu khien 1 nut den bang Webster's Formula.

    Moi chu ky:
      1. Do luu luong PCE tung pha (xe/gio)
      2. Chay Webster -> tinh g_i toi uu cho tung pha
      3. Chay tung pha theo g_i, vang 3s giua cac pha
    """

    def __init__(self, tls_id):
        self.tls_id       = tls_id
        self.state        = STATE_GREEN
        self.switch_count = 0
        self.yellow_start = None

        logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]
        self.all_phases  = logic.phases
        self.num_phases  = len(logic.phases)
        self.green_phases = [
            i for i, p in enumerate(logic.phases)
            if 'G' in p.state or 'g' in p.state
        ]

        # Map pha -> edges
        links = traci.trafficlight.getControlledLinks(tls_id)
        self.phase_edges = {}
        for gp in self.green_phases:
            edges = set()
            for i, s in enumerate(self.all_phases[gp].state):
                if s in ('G', 'g') and i < len(links):
                    for link in links[i]:
                        edges.add(link[0].rsplit('_', 1)[0])
            self.phase_edges[gp] = list(edges)

        self.cur_idx     = 0
        self.green_start = traci.simulation.getTime()

        # Khoi tao thoi gian xanh bang nhau
        self.green_times = [MIN_GREEN] * len(self.green_phases)
        self.plan_seq = 0
        self.plan_id = ""
        self.plan_started_sim = self.green_start
        self.plan_duration = self._target_green()
        self.plan_generated_wall_ms = now_wall_ms()
        self.plan_generated_sim = self.green_start
        self.plan_abrupt = False
        self.plan_reason = "initial"

        traci.trafficlight.setPhase(tls_id, self.green_phases[0])
        self._start_plan(
            self.green_start,
            self._target_green(),
            abrupt=False,
            reason="initial_green",
        )

        print("  [TLS %s] %d pha | Webster S=%d PCE/h L=%ds" % (
            tls_id, len(self.green_phases), SATURATION_FLOW, LOST_TIME))
        for gp in self.green_phases:
            print("    Pha %d: %s" % (gp, self.phase_edges[gp]))

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _cur_green(self):
        return self.green_phases[self.cur_idx]

    def _elapsed(self, sim_time):
        return sim_time - self.green_start

    def _target_green(self):
        return self.green_times[self.cur_idx]

    def _start_plan(self, sim_time, duration, abrupt=False, reason="normal"):
        self.plan_seq += 1
        self.plan_started_sim = sim_time
        self.plan_duration = max(0, float(duration))
        self.plan_generated_wall_ms = now_wall_ms()
        self.plan_generated_sim = sim_time
        self.plan_abrupt = abrupt
        self.plan_reason = reason
        self.plan_id = "%s-%d-%d" % (
            self.tls_id,
            int(self.plan_generated_wall_ms),
            self.plan_seq,
        )

    def _shorten_current_green(self, sim_time, safe_remaining, reason):
        elapsed = self._elapsed(sim_time)
        old_target = self._target_green()
        new_target = elapsed + safe_remaining

        if new_target >= old_target:
            return False

        self.green_times[self.cur_idx] = new_target
        self._start_plan(
            self.green_start,
            new_target,
            abrupt=True,
            reason=reason,
        )
        print("  [SHORTEN %s] P%d %.0fs -> %.0fs | remaining=%.0fs | %s" % (
            self.tls_id,
            self._cur_green(),
            old_target,
            new_target,
            safe_remaining,
            reason,
        ))
        return True

    def get_remaining_seconds(self, sim_time):
        return int(round(self.get_remaining_ms(sim_time) / 1000))

    def get_remaining_ms(self, sim_time):
        if self.state == STATE_YELLOW:
            elapsed = sim_time - (self.yellow_start or sim_time)
            return max(0, int(round((YELLOW_TIME - elapsed) * 1000)))

        elapsed = self._elapsed(sim_time)
        return max(0, int(round((self._target_green() - elapsed) * 1000)))

    def build_mqtt_payload(self, sim_time, area, intersection_id, group_map, seq):
        remaining_ms = self.get_remaining_ms(sim_time)
        remaining = int(round(remaining_ms / 1000))
        sent_wall_ms = now_wall_ms()
        current_phase = traci.trafficlight.getPhase(self.tls_id)
        groups = []

        for group_id in sorted(group_map):
            phase_idx = group_map[group_id]
            if phase_idx == self.cur_idx:
                color = "yellow" if self.state == STATE_YELLOW else "green"
            else:
                color = "red"

            groups.append({
                "id": group_id,
                "color": color,
                "remaining_seconds": remaining,
            })

        return {
            "area": area,
            "intersection_id": intersection_id,
            "tls_id": self.tls_id,
            "sim_time": int(round(sim_time)),
            "seq": seq,
            "sent_wall_ms": sent_wall_ms,
            "plan_id": self.plan_id,
            "plan_seq": self.plan_seq,
            "plan_reason": self.plan_reason,
            "phase_started_sim": round(self.plan_started_sim, 3),
            "phase_duration_seconds": round(self.plan_duration, 3),
            "planned_end_sim": round(self.plan_started_sim + self.plan_duration, 3),
            "remaining_ms": remaining_ms,
            "abrupt_transition": self.plan_abrupt,
            "controller_state": self.state,
            "current_phase": current_phase,
            "remaining_seconds": remaining,
            "groups": groups,
        }

    # ── Cap nhat Webster moi chu ky ──────────────────────────────────────────

    def _update_webster(self):
        """
        Do luu luong hien tai, tinh lai thoi gian xanh toi uu.
        Goi 1 lan moi khi bat dau pha xanh moi.
        """
        # Do luu luong tung pha (PCE/buoc) -> quy doi sang PCE/gio
        # SUMO step = 1s, nhan 3600 de ra PCE/gio
        flows_per_hour = []
        for gp in self.green_phases:
            pce_data = get_phase_pce(self.phase_edges[gp])
            # PCE hien tai tren edge * 3600 = luong xe uoc tinh moi gio
            flows_per_hour.append(pce_data["pce"] * 3600 / 60)

        # Chay Webster
        self.green_times, cycle = webster(flows_per_hour, len(self.green_phases))

        print("  [Webster %s] cycle=%.0fs | %s" % (
            self.tls_id,
            cycle,
            " | ".join("P%d=%.0fs" % (self.green_phases[i], self.green_times[i])
                       for i in range(len(self.green_phases)))))

    # ── Step chinh ───────────────────────────────────────────────────────────

    def step(self, sim_time, is_gridlock, safe_shorten_seconds=SAFE_SHORTEN_SECONDS):

        # ── DANG VANG: cho du 3 giay ─────────────────────────────────────────
        if self.state == STATE_YELLOW:
            if sim_time - self.yellow_start >= YELLOW_TIME:
                # Sang pha xanh tiep theo
                self.cur_idx    = (self.cur_idx + 1) % len(self.green_phases)
                self.green_start = sim_time
                self.state       = STATE_GREEN
                traci.trafficlight.setPhase(self.tls_id, self._cur_green())

                # Cap nhat Webster cho chu ky moi
                self._update_webster()
                self._start_plan(
                    sim_time,
                    self._target_green(),
                    abrupt=False,
                    reason="new_green",
                )

                print("  🟢 %s P%d XANH %.0fs (Webster)" % (
                    self.tls_id, self._cur_green(), self._target_green()))
            return

        # ── DANG XANH: kiem tra het thoi gian Webster chua ───────────────────
        elapsed    = self._elapsed(sim_time)
        target     = self._target_green()
        min_green  = GRIDLOCK_MIN_GREEN if is_gridlock else MIN_GREEN

        # Chua du MIN_GREEN -> giu nguyen
        if elapsed < min_green:
            return

        pce = get_phase_pce(self.phase_edges[self._cur_green()])

        should_switch = False
        reason        = ""

        # 1. Bat buoc: qua MAX_GREEN
        if elapsed >= MAX_GREEN:
            should_switch = True
            reason = "MAX_GREEN %ds" % MAX_GREEN

        # 2. Pha trong: khong co xe
        elif pce["vehicles"] == 0 and elapsed >= min_green and elapsed < target:
            remaining = target - elapsed
            if remaining > safe_shorten_seconds:
                self._shorten_current_green(
                    sim_time,
                    safe_shorten_seconds,
                    "pha trong",
                )
                return

        # 3. Webster: het thoi gian xanh toi uu
        elif elapsed >= target:
            should_switch = True
            reason = ("Webster g=%.0fs elapsed=%.0fs | "
                      "moto=%d(%.1fPCE) car=%d(%.1fPCE) total=%.1fPCE") % (
                target, elapsed,
                pce["moto"], pce["moto"] * PCE_MOTORCYCLE,
                pce["car"],  pce["car"]  * PCE_CAR,
                pce["pce"])

        # ── Bat den vang 3 giay ───────────────────────────────────────────────
        if should_switch:
            yellow_idx = (self._cur_green() + 1) % self.num_phases
            traci.trafficlight.setPhase(self.tls_id, yellow_idx)
            self.state        = STATE_YELLOW
            self.yellow_start = sim_time
            self.switch_count += 1
            self._start_plan(
                sim_time,
                YELLOW_TIME,
                abrupt=False,
                reason="yellow_after_%s" % reason,
            )
            print("  🟡 %s VANG 3s | %s" % (self.tls_id, reason))

    # ── Status ───────────────────────────────────────────────────────────────

    def get_status(self):
        cur = traci.trafficlight.getPhase(self.tls_id)
        out = []
        for i, gp in enumerate(self.green_phases):
            pce = get_phase_pce(self.phase_edges[gp])
            out.append({
                "phase":    gp,
                "pce":      pce,
                "is_green": gp == cur,
                "target_g": self.green_times[i] if i < len(self.green_times) else 0,
                "state":    self.state,
                "edges":    self.phase_edges[gp],
            })
        return out


# ─── GRIDLOCK ────────────────────────────────────────────────────────────────

def check_gridlock():
    total = traci.vehicle.getIDCount()
    if total < 10:
        return False, 0, total
    all_ids = traci.vehicle.getIDList()
    waiting = sum(1 for v in all_ids if traci.vehicle.getSpeed(v) < 0.1)
    return waiting / total >= GRIDLOCK_THRESHOLD, waiting, total


# ─── DASHBOARD ───────────────────────────────────────────────────────────────

def print_dashboard(sim_time, controllers, is_gridlock, waiting, total):
    print("")
    print("=" * 80)
    print("  SimTime: %.0fs%s" % (
        sim_time, "  *** GRIDLOCK ***" if is_gridlock else ""))
    print("  %-8s %-4s %-5s %-4s %-6s %-6s %-8s %-8s" % (
        "TLS", "Pha", "Moto", "Car", "PCE", "Dung%", "TargetG", "Trang thai"))
    print("  " + "-"*78)

    for tls_id, ctrl in controllers.items():
        for s in ctrl.get_status():
            p      = s["pce"]
            marker = "<XANH" if s["is_green"] else "     "
            state  = s["state"] if s["is_green"] else ""
            print("  %-8s P%-3d %-5d %-4d %-6.1f %-6s %-8.0fs %s %s" % (
                tls_id, s["phase"],
                p["moto"], p["car"], p["pce"],
                "%.0f%%" % (p["halt_ratio"] * 100),
                s["target_g"],
                marker, state))

    if total > 0:
        all_ids    = traci.vehicle.getIDList()
        avg_speed  = sum(traci.vehicle.getSpeed(v) for v in all_ids) / total
        teleport   = traci.simulation.getStartingTeleportNumber()
        pct        = waiting / total * 100
        total_moto = sum(1 for v in all_ids
                         if "motorcycle" in traci.vehicle.getTypeID(v).lower())
        total_car  = total - total_moto
        print("")
        print("  Xe may: %d (%.1f PCE) | O to: %d (%.1f PCE) | Tong: %d xe" % (
            total_moto, total_moto * PCE_MOTORCYCLE,
            total_car,  total_car  * PCE_CAR, total))
        print("  Dung: %d (%.0f%%) | V_tb: %.1f m/s | Teleport: %d" % (
            waiting, pct, avg_speed, teleport))
        if   pct > 70: print("  [!!!] UN TAC NGHIEM TRONG")
        elif pct > 40: print("  [!!]  Un tac trung binh")
        else:          print("  [OK]  Giao thong on dinh")
    print("=" * 80)


# ─── MAIN ────────────────────────────────────────────────────────────────────

def build_parser():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Webster traffic controller with optional MQTT publisher"
    )
    parser.add_argument("--sumo-binary", default=os.getenv("SUMO_BINARY", SUMO_BINARY))
    parser.add_argument("--sumo-config", default=os.getenv("SUMO_CONFIG", SUMO_CONFIG))

    parser.add_argument("--mqtt", action="store_true", help="Bat publish MQTT")
    parser.add_argument(
        "--tls-id",
        default=os.getenv("TLS_ID"),
        help="TLS/nga tu SUMO can publish, vi du J105",
    )
    parser.add_argument(
        "--area",
        default=os.getenv("MQTT_AREA", MQTT_AREA),
        help="Khu vuc topic, vi du A",
    )
    parser.add_argument(
        "--intersection-id",
        default=os.getenv("MQTT_INTERSECTION_ID", MQTT_INTERSECTION_ID),
        help="Ma thiet bi/nga tu trong topic, vi du 001",
    )
    parser.add_argument("--mqtt-host", default=os.getenv("MQTT_HOST", MQTT_HOST))
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=int(os.getenv("MQTT_PORT", MQTT_PORT)),
    )
    parser.add_argument("--mqtt-username", default=os.getenv("MQTT_USERNAME", ""))
    parser.add_argument("--mqtt-password", default=os.getenv("MQTT_PASSWORD", ""))
    parser.add_argument(
        "--publish-interval",
        type=float,
        default=float(os.getenv("MQTT_PUBLISH_INTERVAL", MQTT_PUBLISH_INTERVAL)),
        help="So giay mo phong giua 2 lan publish",
    )
    parser.add_argument(
        "--realtime",
        action="store_true",
        default=env_bool("MQTT_REALTIME", MQTT_REALTIME),
        help="Giu toc do mo phong gan voi thoi gian that",
    )
    parser.add_argument(
        "--no-realtime",
        action="store_false",
        dest="realtime",
        help="Tat gioi han realtime, chay nhanh nhat co the",
    )
    parser.add_argument(
        "--realtime-factor",
        type=float,
        default=float(os.getenv("MQTT_REALTIME_FACTOR", MQTT_REALTIME_FACTOR)),
        help="1.0 = 1s mo phong/1s that, 2.0 = nhanh gap doi",
    )
    parser.add_argument(
        "--group-map",
        default=os.getenv("GROUP_MAP", DEFAULT_GROUP_MAP),
        help='Mapping cum den -> index pha xanh, vi du "1:0,3:0,2:1,4:1"',
    )
    parser.add_argument(
        "--safe-shorten-seconds",
        type=float,
        default=float(os.getenv("SAFE_SHORTEN_SECONDS", SAFE_SHORTEN_SECONDS)),
        help="Nguong rut ngan an toan khi pha trong",
    )
    return parser


def build_mqtt_topic(area, intersection_id):
    return "traffic/%s/%s/state" % (area, intersection_id)


def run(args):
    global traci

    setup_traci_import()
    import traci as traci_module
    traci = traci_module

    print("=" * 65)
    print("  Webster's Formula Traffic Controller")
    print("=" * 65)
    print("  Luu luong : PCE  (moto=%.1f  car=%.1f)" % (
        PCE_MOTORCYCLE, PCE_CAR))
    print("  Thuat toan: Webster 1958")
    print("    S=%d PCE/h | L=%ds/pha" % (SATURATION_FLOW, LOST_TIME))
    print("    g_i = (C* - L) * y_i / sum(y)")
    print("  Den vang  : %ds bat buoc" % YELLOW_TIME)
    print("  Green     : %ds - %ds" % (MIN_GREEN, MAX_GREEN))
    print("")

    traci.start([args.sumo_binary, "-c", args.sumo_config, "--start"])
    print("[OK] Ket noi TraCI\n")

    controllers = {}
    for tid in traci.trafficlight.getIDList():
        try:
            controllers[tid] = DensityController(tid)
        except Exception as e:
            print("  [WARN] %s: %s" % (tid, e))

    print("\n  %d nut den: %s\n" % (len(controllers), list(controllers.keys())))

    mqtt_publisher = None
    mqtt_ctrl = None
    mqtt_group_map = None
    mqtt_seq = 0
    last_publish = -args.publish_interval

    if args.mqtt:
        if not args.tls_id:
            raise ValueError("Can truyen --tls-id khi dung --mqtt")
        if args.tls_id not in controllers:
            raise ValueError(
                "Khong tim thay tls-id '%s'. Cac tls hop le: %s"
                % (args.tls_id, list(controllers.keys()))
            )

        mqtt_ctrl = controllers[args.tls_id]
        mqtt_group_map = parse_group_map(args.group_map)
        validate_group_map(mqtt_group_map, mqtt_ctrl)

        mqtt_topic = build_mqtt_topic(args.area, args.intersection_id)
        client_id = "iotmap-%s-%s-publisher" % (args.area, args.intersection_id)
        mqtt_publisher = MqttPublisher(
            args.mqtt_host,
            args.mqtt_port,
            args.mqtt_username,
            args.mqtt_password,
            client_id,
            mqtt_topic,
        )
        mqtt_publisher.connect()

    last_dash = 0
    realtime_start_wall = None
    realtime_start_sim = None
    try:
        while traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            sim_time = traci.simulation.getTime()

            if args.realtime and realtime_start_wall is None:
                realtime_start_wall = time.monotonic()
                realtime_start_sim = sim_time

            is_gridlock, waiting, total = check_gridlock()

            for ctrl in controllers.values():
                try:
                    ctrl.step(sim_time, is_gridlock, args.safe_shorten_seconds)
                except Exception:
                    pass

            if mqtt_publisher and sim_time - last_publish >= args.publish_interval:
                mqtt_seq += 1
                payload = mqtt_ctrl.build_mqtt_payload(
                    sim_time,
                    args.area,
                    args.intersection_id,
                    mqtt_group_map,
                    mqtt_seq,
                )
                mqtt_publisher.publish(payload)
                last_publish = sim_time

            if sim_time - last_dash >= DASHBOARD_INTERVAL:
                print_dashboard(sim_time, controllers, is_gridlock, waiting, total)
                last_dash = sim_time

            if args.realtime and args.realtime_factor > 0:
                sim_elapsed = sim_time - realtime_start_sim
                target_wall = realtime_start_wall + sim_elapsed / args.realtime_factor
                delay = target_wall - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
    finally:
        if mqtt_publisher:
            mqtt_publisher.close()
        traci.close()

    print("\n[OK] Ket thuc!")


if __name__ == "__main__":
    try:
        run(build_parser().parse_args())
    except KeyboardInterrupt:
        print("\n[STOP]")
        try: traci.close()
        except: pass
    except Exception:
        import traceback; traceback.print_exc()
        try: traci.close()
        except: pass
