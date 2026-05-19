"""
main_controller.py
===================================================
Thuat toan dieu khien den giao thong:

    DO DEMAND (ap luc pha)
        demand = PCE + hang_doi*QUEUE_WEIGHT + avg_wait*WAIT_WEIGHT
        demand duoc lam muot (EMA) va co bonus cho pha re trai.

    CHU KY ADAPTIVE
        cycle = MIN_CYCLE + total_demand*CYCLE_EXTRA_PER_DEMAND
        green_i = min_green + phan_bo_ti_le theo demand

    THU TU PHA
        Di theo thu tu pha trong tlLogic, tu dong chay pha vang/all-red.

Cach chay:
  python main_controller.py
"""

import argparse
import json
import math
import os
import ssl
import sys
import time

traci = None

# ─── CONFIG ──────────────────────────────────────────────────────────────────

SUMO_BINARY = "sumo-gui"
SUMO_CONFIG = "sim.sumocfg"

# Performance
USE_LANE_METRICS = False  # Faster, but PCE becomes approximate

# PCE — Passenger Car Equivalent
PCE_MOTORCYCLE = 0.5
PCE_CAR        = 1.0

# Cycle parameters
MIN_GREEN       = 10     # giay — gioi han duoi cho thoi gian xanh
MAX_GREEN       = 90     # giay — gioi han tren
MIN_CYCLE       = 30     # giay — chu ky toi thieu
MAX_CYCLE       = 120    # giay — chu ky toi da

# Gridlock
GRIDLOCK_THRESHOLD = 0.70

# Demand smoothing
DEMAND_SMOOTHING = 0.35
QUEUE_WEIGHT = 0.8
WAIT_WEIGHT = 0.03  # Tang neu xe doi lau bi doi qua muc
CYCLE_EXTRA_PER_DEMAND = 2.0
LOW_DEMAND = 2.0

# Left-turn protected phases
TURN_PHASE_DURATION_LIMIT = 12
TURN_PHASE_GREEN_LIMIT = 2
TURN_PHASE_BONUS = 2.5
TURN_PHASE_MIN_GREEN = 15
TURN_PHASE_LOW_DEMAND = 0.5
TURN_PHASE_OVERRIDE = {}
LEFT_TURN_ANGLE_MIN = 0.35
LEFT_TURN_ANGLE_MAX = 2.8
TURN_PREEMPT_THRESHOLD = 4.0
TURN_PREEMPT_RATIO = 1.2
TURN_PREEMPT_COOLDOWN = 8.0

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

# ─── STATE MACHINE ───────────────────────────────────────────────────────────

STATE_GREEN  = "GREEN"
STATE_TRANSITION = "TRANSITION"


def parse_group_map(text):
    """Doc mapping cum den -> thu tu pha xanh."""
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
                "Chua cai paho-mqtt. Hay chay: python -m pip install paho-mqtt"
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

_CACHE_SIM_TIME = None
_EDGE_CACHE = {}
_PHASE_CACHE = {}


def _reset_cache(sim_time):
    global _CACHE_SIM_TIME, _EDGE_CACHE, _PHASE_CACHE
    if sim_time is None:
        return
    if _CACHE_SIM_TIME != sim_time:
        _CACHE_SIM_TIME = sim_time
        _EDGE_CACHE = {}
        _PHASE_CACHE = {}

def get_edge_pce(edge_id, sim_time=None):
    """
    Dem xe may va o to tren 1 edge, tinh PCE.
    """
    _reset_cache(sim_time)
    if sim_time is not None and edge_id in _EDGE_CACHE:
        return _EDGE_CACHE[edge_id]

    moto = car = halting = 0
    total_wait = 0.0
    max_wait = 0.0
    if USE_LANE_METRICS:
        vehicles = 0
        try:
            lane_count = traci.edge.getLaneNumber(edge_id)
        except traci.TraCIException:
            lane_count = 0

        for i in range(lane_count):
            lane_id = f"{edge_id}_{i}"
            try:
                vehicles += traci.lane.getLastStepVehicleNumber(lane_id)
                halting += traci.lane.getLastStepHaltingNumber(lane_id)
                total_wait += traci.lane.getWaitingTime(lane_id)
            except traci.TraCIException:
                continue

        car = vehicles
        pce = vehicles * PCE_CAR
        if vehicles > 0:
            max_wait = total_wait / vehicles
    else:
        try:
            for vid in traci.edge.getLastStepVehicleIDs(edge_id):
                try:
                    vtype = traci.vehicle.getTypeID(vid).lower()
                    speed = traci.vehicle.getSpeed(vid)
                    wait  = traci.vehicle.getWaitingTime(vid)
                    total_wait += wait
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
    out = {
        "pce":      pce,
        "moto":     moto,
        "car":      car,
        "halting":  halting,
        "wait_sum": total_wait,
        "avg_wait": total_wait / max(moto + car, 1),
        "max_wait": max_wait,
    }

    if sim_time is not None:
        _EDGE_CACHE[edge_id] = out
    return out


def get_phase_pce(edges, sim_time=None):
    """Gop PCE cua nhieu edges thanh 1 pha."""
    _reset_cache(sim_time)
    cache_key = tuple(sorted(edges))
    if sim_time is not None and cache_key in _PHASE_CACHE:
        return _PHASE_CACHE[cache_key]

    total_pce = total_moto = total_car = total_halt = 0
    total_wait = 0.0
    max_wait  = 0.0
    for edge in edges:
        d = get_edge_pce(edge, sim_time)
        total_pce  += d["pce"]
        total_moto += d["moto"]
        total_car  += d["car"]
        total_halt += d["halting"]
        total_wait += d["wait_sum"]
        if d["max_wait"] > max_wait:
            max_wait = d["max_wait"]
    vehicles   = total_moto + total_car
    halt_ratio = total_halt / max(vehicles, 1)
    out = {
        "pce":        total_pce,
        "moto":       total_moto,
        "car":        total_car,
        "vehicles":   vehicles,
        "halting":    total_halt,
        "halt_ratio": halt_ratio,
        "wait_sum":   total_wait,
        "avg_wait":   total_wait / max(vehicles, 1),
        "max_wait":   max_wait,
    }

    if sim_time is not None:
        _PHASE_CACHE[cache_key] = out
    return out


def get_phase_demand(phase_stats):
    """
    Tinh ap luc cua 1 pha tu PCE, hang doi va thoi gian cho.

    Muc tieu la uu tien pha dang tac, nhung khong de 1 so xe dung im
    lam khuuch dai green_time qua manh.
    """
    return (
        phase_stats["pce"]
        + phase_stats["halting"] * QUEUE_WEIGHT
        + phase_stats["avg_wait"] * WAIT_WEIGHT
    )


# ─── CONTROLLER ──────────────────────────────────────────────────────────────

class DensityController:
    """
    Dieu khien 1 nut den theo ap luc giao thong (adaptive).

    Moi chu ky:
            1. Do ap luc tung pha (PCE + hang doi + cho)
            2. Cap thoi gian xanh theo ty le ap luc
            3. Di theo thu tu tlLogic, tu dong chay pha vang/all-red
    """

    def __init__(self, tls_id):
        self.tls_id       = tls_id
        self.state        = STATE_GREEN
        self.switch_count = 0
        logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]
        self.all_phases  = logic.phases
        self.num_phases  = len(logic.phases)
        self.phase_durations = [p.duration for p in self.all_phases]
        self.phase_green_counts = [sum(1 for c in p.state if c in ('G', 'g'))
                                   for p in self.all_phases]
        self.green_phases = [
            i for i, p in enumerate(logic.phases)
            if 'G' in p.state or 'g' in p.state
        ]
        self.green_index = {gp: i for i, gp in enumerate(self.green_phases)}
        # Map pha -> edges
        links = traci.trafficlight.getControlledLinks(tls_id)
        self.left_turn_signals = self._infer_left_turn_signals(links)
        self.turn_phases = set()
        if self.left_turn_signals:
            for gp in self.green_phases:
                greens = {idx for idx, c in enumerate(self.all_phases[gp].state)
                          if c in ('G', 'g')}
                if greens and greens.issubset(self.left_turn_signals):
                    self.turn_phases.add(gp)

        if not self.turn_phases:
            self.turn_phases = {
                i for i in self.green_phases
                if self.phase_durations[i] <= TURN_PHASE_DURATION_LIMIT
                and self.phase_green_counts[i] <= TURN_PHASE_GREEN_LIMIT
            }
        override = TURN_PHASE_OVERRIDE.get(tls_id)
        if override:
            self.turn_phases.update(set(override))
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
        self.phase_demand_ema = {gp: None for gp in self.green_phases}
        self.transition_seq = []
        self.transition_pos = 0
        self.transition_start = None
        self.transition_target = None
        self.last_turn_time = -1.0
        self.plan_seq = 0
        self.plan_id = ""
        self.plan_started_sim = self.green_start
        self.plan_duration = MIN_GREEN
        self.plan_generated_wall_ms = now_wall_ms()
        self.plan_generated_sim = self.green_start
        self.plan_abrupt = False
        self.plan_reason = "initial"

        # Khoi tao thoi gian xanh bang nhau
        self.green_times = [MIN_GREEN] * len(self.green_phases)

        traci.trafficlight.setPhase(tls_id, self.green_phases[0])
        self._update_plan(self.green_start)
        self._start_plan(
            self.green_start,
            self._target_green(),
            abrupt=False,
            reason="initial_green",
        )

        print("  [TLS %s] %d pha (adaptive)" % (
            tls_id, len(self.green_phases)))
        for gp in self.green_phases:
            print("    Pha %d: %s" % (gp, self.phase_edges[gp]))

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _cur_green(self):
        return self.green_phases[self.cur_idx]

    def _elapsed(self, sim_time):
        return sim_time - self.green_start

    def _target_green(self):
        return self.green_times[self.cur_idx]

    def _phase_min_green(self, phase_idx):
        min_green = MIN_GREEN
        if phase_idx in self.turn_phases:
            min_green = max(min_green, TURN_PHASE_MIN_GREEN)
        return min_green

    def _start_plan(self, sim_time, duration, abrupt=False, reason="normal"):
        self.plan_seq += 1
        self.plan_started_sim = sim_time
        self.plan_duration = max(0.0, float(duration))
        self.plan_generated_wall_ms = now_wall_ms()
        self.plan_generated_sim = sim_time
        self.plan_abrupt = abrupt
        self.plan_reason = reason
        self.plan_id = "%s-%d-%d" % (
            self.tls_id,
            int(self.plan_generated_wall_ms),
            self.plan_seq,
        )

    def get_remaining_ms(self, sim_time):
        if self.state == STATE_TRANSITION and self.transition_seq:
            _, duration = self.transition_seq[self.transition_pos]
            elapsed = sim_time - (self.transition_start or sim_time)
            return max(0, int(round((duration - elapsed) * 1000)))

        elapsed = self._elapsed(sim_time)
        return max(0, int(round((self._target_green() - elapsed) * 1000)))

    def get_remaining_seconds(self, sim_time):
        return int(round(self.get_remaining_ms(sim_time) / 1000))

    def build_mqtt_payload(self, sim_time, area, intersection_id, group_map, seq):
        remaining_ms = self.get_remaining_ms(sim_time)
        remaining = int(round(remaining_ms / 1000))
        current_phase = traci.trafficlight.getPhase(self.tls_id)
        groups = []

        for group_id in sorted(group_map):
            phase_idx = group_map[group_id]
            if phase_idx == self.cur_idx:
                color = "yellow" if self.state == STATE_TRANSITION else "green"
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
            "sent_wall_ms": now_wall_ms(),
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

    def _lane_heading(self, lane_id, use_end):
        try:
            shape = traci.lane.getShape(lane_id)
        except traci.TraCIException:
            return None
        if len(shape) < 2:
            return None

        if use_end:
            p1, p2 = shape[-2], shape[-1]
        else:
            p1, p2 = shape[0], shape[1]
        return math.atan2(p2[1] - p1[1], p2[0] - p1[0])

    def _is_left_turn_link(self, from_lane, to_lane):
        from_heading = self._lane_heading(from_lane, True)
        to_heading = self._lane_heading(to_lane, False)
        if from_heading is None or to_heading is None:
            return False

        diff = to_heading - from_heading
        while diff <= -math.pi:
            diff += 2 * math.pi
        while diff > math.pi:
            diff -= 2 * math.pi
        return LEFT_TURN_ANGLE_MIN <= diff <= LEFT_TURN_ANGLE_MAX

    def _infer_left_turn_signals(self, links):
        left_signals = set()
        for idx, link_set in enumerate(links):
            for link in link_set:
                if len(link) < 2:
                    continue
                from_lane, to_lane = link[0], link[1]
                if self._is_left_turn_link(from_lane, to_lane):
                    left_signals.add(idx)
                    break
        return left_signals

    def _build_transition_sequence(self, start_phase, target_phase):
        if start_phase == target_phase:
            return []

        seq = []
        phase_idx = (start_phase + 1) % self.num_phases
        while phase_idx != target_phase:
            seq.append((phase_idx, self.phase_durations[phase_idx]))
            phase_idx = (phase_idx + 1) % self.num_phases
            if phase_idx == start_phase:
                break
        return seq

    def _start_green(self, sim_time):
        self.green_start = sim_time
        self.state = STATE_GREEN
        traci.trafficlight.setPhase(self.tls_id, self._cur_green())
        self._update_plan(sim_time)
        self._start_plan(
            sim_time,
            self._target_green(),
            abrupt=False,
            reason="new_green",
        )
        if self._cur_green() in self.turn_phases:
            self.last_turn_time = sim_time

    def _start_transition_to(self, sim_time, target_phase):
        cur_green = self._cur_green()
        if target_phase == cur_green:
            return

        transition = self._build_transition_sequence(cur_green, target_phase)
        if not transition:
            self.cur_idx = self.green_index.get(target_phase, self.cur_idx)
            self._start_green(sim_time)
            return

        self.transition_seq = transition
        self.transition_pos = 0
        self.transition_start = sim_time
        self.transition_target = target_phase
        self.state = STATE_TRANSITION
        traci.trafficlight.setPhase(self.tls_id, self.transition_seq[0][0])
        self._start_plan(
            sim_time,
            self.transition_seq[0][1],
            abrupt=False,
            reason="transition",
        )

    def _best_turn_phase(self, sim_time):
        best_phase = None
        best_demand = 0.0
        for gp in self.turn_phases:
            pce_data = get_phase_pce(self.phase_edges[gp], sim_time)
            demand = get_phase_demand(pce_data) * TURN_PHASE_BONUS
            if demand > best_demand:
                best_demand = demand
                best_phase = gp
        return best_phase, best_demand

    def _select_next_phase(self, sim_time, cur_demand):
        if not self.green_phases:
            return None

        next_idx = (self.cur_idx + 1) % len(self.green_phases)
        default_next = self.green_phases[next_idx]

        if not self.turn_phases or self._cur_green() in self.turn_phases:
            return default_next

        best_turn, turn_demand = self._best_turn_phase(sim_time)
        if best_turn is None:
            return default_next

        if sim_time - self.last_turn_time < TURN_PREEMPT_COOLDOWN:
            return default_next

        if turn_demand < TURN_PREEMPT_THRESHOLD:
            return default_next

        if turn_demand >= cur_demand * TURN_PREEMPT_RATIO or cur_demand <= LOW_DEMAND:
            return best_turn

        return default_next

    # ── Cap nhat plan moi chu ky ─────────────────────────────────────────────

    def _update_plan(self, sim_time):
        """
        Cap nhat green time theo ap luc (demand) cua tung pha.
        Goi 1 lan moi khi bat dau pha xanh moi.
        """
        demands = {}
        min_green = {}
        for gp in self.green_phases:
            pce_data = get_phase_pce(self.phase_edges[gp], sim_time)
            demand = get_phase_demand(pce_data)
            if gp in self.turn_phases:
                demand *= TURN_PHASE_BONUS

            prev = self.phase_demand_ema[gp]
            if prev is None:
                ema = demand
            else:
                ema = prev * (1 - DEMAND_SMOOTHING) + demand * DEMAND_SMOOTHING

            self.phase_demand_ema[gp] = ema
            demands[gp] = max(0.0, ema)
            min_green[gp] = self._phase_min_green(gp)

        total_demand = sum(demands.values())
        total_min = sum(min_green.values())
        cycle = MIN_CYCLE + total_demand * CYCLE_EXTRA_PER_DEMAND
        cycle = max(total_min, cycle)
        if total_min <= MAX_CYCLE:
            cycle = min(cycle, MAX_CYCLE)
        cycle = max(MIN_CYCLE, cycle)

        remaining = max(0.0, cycle - total_min)
        self.green_times = []
        for gp in self.green_phases:
            if total_demand > 0:
                extra = remaining * demands[gp] / total_demand
            else:
                extra = 0.0
            target = min_green[gp] + extra
            target = max(min_green[gp], min(MAX_GREEN, target))
            self.green_times.append(target)

        print("  [PLAN %s] cycle=%.0fs | %s" % (
            self.tls_id,
            cycle,
            " | ".join("P%d=%.0fs" % (self.green_phases[i], self.green_times[i])
                       for i in range(len(self.green_phases)))))

    # ── Step chinh ───────────────────────────────────────────────────────────

    def step(self, sim_time):

        # ── DANG CHUYEN PHA: chay theo duration trong tlLogic ───────────────
        if self.state == STATE_TRANSITION:
            if not self.transition_seq:
                self.state = STATE_GREEN
                return

            phase_idx, duration = self.transition_seq[self.transition_pos]
            if sim_time - self.transition_start >= duration:
                self.transition_pos += 1
                if self.transition_pos >= len(self.transition_seq):
                    if self.transition_target is not None:
                        self.cur_idx = self.green_index.get(self.transition_target, self.cur_idx)
                    else:
                        self.cur_idx = (self.cur_idx + 1) % len(self.green_phases)
                    self._start_green(sim_time)
                else:
                    phase_idx, _ = self.transition_seq[self.transition_pos]
                    self.transition_start = sim_time
                    traci.trafficlight.setPhase(self.tls_id, phase_idx)
                    self._start_plan(
                        sim_time,
                        self.transition_seq[self.transition_pos][1],
                        abrupt=False,
                        reason="transition",
                    )
            return

        # ── DANG XANH: kiem tra het thoi gian target chua ────────────────────
        elapsed    = self._elapsed(sim_time)
        target     = self._target_green()
        min_green  = self._phase_min_green(self._cur_green())

        # Chua du MIN_GREEN -> giu nguyen
        if elapsed < min_green:
            return

        pce = get_phase_pce(self.phase_edges[self._cur_green()], sim_time)
        demand = get_phase_demand(pce)
        if self._cur_green() in self.turn_phases:
            demand *= TURN_PHASE_BONUS
        low_demand = TURN_PHASE_LOW_DEMAND if self._cur_green() in self.turn_phases else LOW_DEMAND
        preferred_next = self._select_next_phase(sim_time, demand)

        should_switch = False
        reason        = ""

        # 1. Bat buoc: qua MAX_GREEN
        if elapsed >= MAX_GREEN:
            should_switch = True
            reason = "MAX_GREEN %ds" % MAX_GREEN

        # 2. Pha yeu: it xe / khong co hang doi
        elif (pce["vehicles"] == 0 or demand <= low_demand) and elapsed >= min_green:
            should_switch = True
            reason = "pha yeu"

        # 3. Het thoi gian xanh toi uu
        elif elapsed >= target:
            should_switch = True
            reason = ("Plan g=%.0fs elapsed=%.0fs | "
                      "moto=%d(%.1fPCE) car=%d(%.1fPCE) total=%.1fPCE") % (
                target, elapsed,
                pce["moto"], pce["moto"] * PCE_MOTORCYCLE,
                pce["car"],  pce["car"]  * PCE_CAR,
                pce["pce"])

        if not should_switch and preferred_next is not None and preferred_next != self._cur_green():
            if elapsed >= min_green and preferred_next in self.turn_phases:
                should_switch = True
                reason = "turn preempt"

        # ── Bat chuyen pha ───────────────────────────────────────────────────
        if should_switch:
            self.switch_count += 1
            target_phase = preferred_next if preferred_next is not None else self._cur_green()
            self._start_transition_to(sim_time, target_phase)
            print("  TRANSITION %s | %s" % (self.tls_id, reason))

    # ── Status ───────────────────────────────────────────────────────────────

    def get_status(self, sim_time=None):
        cur = traci.trafficlight.getPhase(self.tls_id)
        out = []
        for i, gp in enumerate(self.green_phases):
            pce = get_phase_pce(self.phase_edges[gp], sim_time)
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
        for s in ctrl.get_status(sim_time):
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
        description="Adaptive demand traffic controller with optional MQTT publisher"
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
    return parser


def build_mqtt_topic(area, intersection_id):
    return "traffic/%s/%s/state" % (area, intersection_id)


def mark_selected_tls(tls_id, area, intersection_id):
    """Danh dau nut den dang publish trong SUMO GUI."""
    try:
        x, y = traci.junction.getPosition(tls_id)
        marker_id = "mqtt_marker_%s_%s_%s" % (area, intersection_id, tls_id)
        square_id = marker_id + "_box"
        size = 18

        traci.poi.add(
            marker_id,
            x,
            y,
            (255, 0, 0, 255),
            poiType="MQTT_SELECTED_TLS",
            layer=100,
            width=8,
            height=8,
        )
        traci.polygon.add(
            square_id,
            [
                (x - size, y - size),
                (x + size, y - size),
                (x + size, y + size),
                (x - size, y + size),
            ],
            (255, 0, 0, 180),
            fill=False,
            polygonType="MQTT_SELECTED_TLS_BOX",
            layer=99,
            lineWidth=4,
        )
        print("[SUMO] Marked MQTT TLS %s at topic traffic/%s/%s/state" % (
            tls_id, area, intersection_id))
    except Exception as e:
        print("[SUMO][WARN] Cannot mark TLS %s: %s" % (tls_id, e))


def run(args):
    global traci

    setup_traci_import()
    import traci as traci_module
    traci = traci_module

    print("=" * 65)
    print("  Adaptive Demand Traffic Controller")
    print("=" * 65)
    print("  Luu luong : PCE  (moto=%.1f  car=%.1f)" % (
        PCE_MOTORCYCLE, PCE_CAR))
    print("  Thuat toan: Adaptive demand")
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
        mark_selected_tls(args.tls_id, args.area, args.intersection_id)

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
                    ctrl.step(sim_time)
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
