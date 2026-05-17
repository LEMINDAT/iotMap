"""
main_controller.py
===================================================
Dieu khien den giao thong thong minh dua tren MAT DO XE.

Thuat toan:
  1. Do mat do xe tren TOAN BO edge dan toi nga tu
  2. Chia thoi gian xanh TY LE voi luong xe moi huong
  3. Khi gridlock: GIU xanh 1 huong du lau de xe thoat ra
  4. Khong doi qua nhanh khi spillback
  5. Bo pha trong: neu huong khong co xe -> skip

  CHI dieu chinh den tin hieu, KHONG can thiep vao xe.
  Dung map da tao boi: python convert.py
  Config:               sim.sumocfg

Cach chay:
  python main_controller.py
"""

import os, sys, time, traci

# --- CONFIG ---

SUMO_BINARY = "sumo-gui"
SUMO_CONFIG = "sim.sumocfg"

# Thong so dieu khien
MIN_GREEN          = 8       # giay toi thieu pha xanh
MAX_GREEN          = 50      # giay toi da pha xanh
YELLOW_TIME        = 3       # giay vang (co san trong map)

# Nguong
PRESSURE_RATIO     = 1.3     # huong cho ap luc hon X lan -> doi
MAX_WAIT_SECONDS   = 40      # giay cho toi da truoc khi bat buoc doi
GRIDLOCK_THRESHOLD = 0.7     # 70% xe dung yen -> coi la gridlock
GRIDLOCK_MIN_GREEN = 20      # khi gridlock: giu xanh it nhat nay giay

DASHBOARD_INTERVAL = 30


# --- DENSITY-BASED CONTROLLER ---

class DensityController:

    def __init__(self, tls_id):
        self.tls_id = tls_id

        logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]
        self.all_phases = logic.phases
        self.num_phases = len(logic.phases)

        self.green_phases = [
            i for i, p in enumerate(logic.phases)
            if 'G' in p.state or 'g' in p.state
        ]

        controlled_links = traci.trafficlight.getControlledLinks(tls_id)

        self.phase_in_edges = {}
        self.phase_out_edges = {}

        for phase_idx in self.green_phases:
            state = self.all_phases[phase_idx].state
            in_edges = set()
            out_edges = set()

            for i, s in enumerate(state):
                if s in ('G', 'g') and i < len(controlled_links):
                    for link in controlled_links[i]:
                        in_lane, out_lane, _ = link
                        in_edge = in_lane.rsplit('_', 1)[0]
                        out_edge = out_lane.rsplit('_', 1)[0]
                        in_edges.add(in_edge)
                        out_edges.add(out_edge)

            self.phase_in_edges[phase_idx] = list(in_edges)
            self.phase_out_edges[phase_idx] = list(out_edges)

        self.current_green_idx = 0
        self.green_start_time = traci.simulation.getTime()
        self.switch_count = 0
        self.gridlock_mode = False

        traci.trafficlight.setPhase(tls_id, self.green_phases[0])

        in_info = {gp: self.phase_in_edges[gp] for gp in self.green_phases}
        print("  [TLS %s] %d pha | edges: %s" % (tls_id, len(self.green_phases), str(in_info)))

    def _get_edge_demand(self, phase_idx):
        edges = self.phase_in_edges.get(phase_idx, [])
        total_vehicles = 0
        total_halting = 0
        total_waiting = 0.0
        max_waiting = 0.0

        for edge in edges:
            try:
                v = traci.edge.getLastStepVehicleNumber(edge)
                h = traci.edge.getLastStepHaltingNumber(edge)
                w = traci.edge.getWaitingTime(edge)
                total_vehicles += v
                total_halting += h
                total_waiting += w
                if w > max_waiting:
                    max_waiting = w
            except traci.TraCIException:
                pass

        return {
            "vehicles":    total_vehicles,
            "halting":     total_halting,
            "waiting":     total_waiting,
            "max_waiting": max_waiting,
            "demand":      total_halting * 2 + total_vehicles,
        }

    def _get_downstream_halting(self, phase_idx):
        """So xe dung tren outgoing edges."""
        out_edges = self.phase_out_edges.get(phase_idx, [])
        total = 0
        for edge in out_edges:
            try:
                total += traci.edge.getLastStepHaltingNumber(edge)
            except traci.TraCIException:
                pass
        return total

    def _calc_green_time(self, cur_demand, next_demand):
        """Tinh thoi gian xanh dua tren ty le demand."""
        total = cur_demand["demand"] + next_demand["demand"]
        if total == 0:
            return MIN_GREEN

        ratio = cur_demand["demand"] / max(total, 1)
        # Thoi gian xanh ty le
        available = MAX_GREEN + MIN_GREEN  # tong green cho 2 pha
        gt = available * ratio
        return max(MIN_GREEN, min(MAX_GREEN, gt))

    def _switch_to_next(self, sim_time, reason):
        cur_phase = self.green_phases[self.current_green_idx]
        yellow = (cur_phase + 1) % self.num_phases
        traci.trafficlight.setPhase(self.tls_id, yellow)

        self.current_green_idx = (self.current_green_idx + 1) % len(self.green_phases)
        self.green_start_time = sim_time + YELLOW_TIME
        self.switch_count += 1

        next_green = self.green_phases[self.current_green_idx]
        print("  >> %s: -> P%d | %s" % (self.tls_id, next_green, reason))

    def step(self, sim_time, is_gridlock):
        current_sumo_phase = traci.trafficlight.getPhase(self.tls_id)
        cur_green = self.green_phases[self.current_green_idx]

        # Dang trong pha vang -> cho
        if current_sumo_phase != cur_green:
            return

        elapsed = sim_time - self.green_start_time

        # Tinh demand
        next_idx = (self.current_green_idx + 1) % len(self.green_phases)
        next_green = self.green_phases[next_idx]

        cur_demand = self._get_edge_demand(cur_green)
        next_demand = self._get_edge_demand(next_green)

        # Tinh green toi uu
        target_green = self._calc_green_time(cur_demand, next_demand)

        # Khi GRIDLOCK: tang min green de xe co thoi gian thoat
        min_green_now = GRIDLOCK_MIN_GREEN if is_gridlock else MIN_GREEN

        if elapsed < min_green_now:
            return

        should_switch = False
        reason = ""

        # 1. MAX_GREEN bat buoc
        if elapsed >= MAX_GREEN:
            should_switch = True
            reason = "max green %ds" % MAX_GREEN

        # 2. Pha hien tai TRONG, huong khac co xe
        elif cur_demand["vehicles"] == 0 and next_demand["vehicles"] > 0:
            should_switch = True
            reason = "pha trong -> kia co %d xe" % next_demand["vehicles"]

        # 3. Dat target green (ty le demand)
        elif elapsed >= target_green:
            should_switch = True
            reason = ("target %.0fs | cur:%d/%d next:%d/%d" %
                      (target_green,
                       cur_demand["halting"], cur_demand["vehicles"],
                       next_demand["halting"], next_demand["vehicles"]))

        # 4. Huong cho co demand cao hon nhieu
        elif (next_demand["demand"] > cur_demand["demand"] * PRESSURE_RATIO
              and next_demand["halting"] >= 3
              and elapsed >= min_green_now + 3):
            should_switch = True
            reason = ("density %d > %d*%.1f" %
                      (next_demand["demand"],
                       cur_demand["demand"], PRESSURE_RATIO))

        # 5. Starvation
        elif next_demand["max_waiting"] > MAX_WAIT_SECONDS and elapsed >= min_green_now:
            should_switch = True
            reason = ("starvation cho %.0fs" % next_demand["max_waiting"])

        if should_switch:
            self._switch_to_next(sim_time, reason)

    def get_status(self):
        current_phase = traci.trafficlight.getPhase(self.tls_id)
        results = []
        for gp in self.green_phases:
            d = self._get_edge_demand(gp)
            ds_halt = self._get_downstream_halting(gp)
            results.append({
                "phase": gp,
                "demand": d,
                "is_green": (gp == current_phase),
                "downstream_halt": ds_halt,
                "edges": self.phase_in_edges[gp],
            })
        return results


# --- DASHBOARD ---

def check_gridlock():
    """Kiem tra toan mang co bi gridlock khong."""
    total = traci.vehicle.getIDCount()
    if total < 10:
        return False, 0, total
    all_ids = traci.vehicle.getIDList()
    waiting = sum(1 for vid in all_ids if traci.vehicle.getSpeed(vid) < 0.1)
    pct = waiting / total
    return pct >= GRIDLOCK_THRESHOLD, waiting, total


def print_dashboard(sim_time, controllers, is_gridlock, waiting, total):
    print("")
    print("=" * 75)
    print("  SimTime: %.0fs%s" % (sim_time, "  *** GRIDLOCK MODE ***" if is_gridlock else ""))
    print("  %-6s %-4s %-18s %-5s %-5s %-6s %-6s %s" %
          ("TLS", "Pha", "Edges", "Xe", "Dung", "Wait", "DsHlt", ""))
    print("  %s %s %s %s %s %s %s %s" %
          ("-"*6, "-"*4, "-"*18, "-"*5, "-"*5, "-"*6, "-"*6, "-"*5))

    for tls_id, ctrl in controllers.items():
        for s in ctrl.get_status():
            d = s["demand"]
            marker = " <== GREEN" if s["is_green"] else ""
            edges_str = ",".join(s["edges"])
            if len(edges_str) > 17:
                edges_str = edges_str[:14] + "..."
            print("  %-6s P%-3d %-18s %-5d %-5d %-6.0f %-6d%s" %
                  (tls_id, s["phase"], edges_str,
                   d["vehicles"], d["halting"],
                   d["max_waiting"], s["downstream_halt"], marker))

    if total > 0:
        avg_speed = sum(traci.vehicle.getSpeed(vid) for vid in traci.vehicle.getIDList()) / total
        teleported = traci.simulation.getStartingTeleportNumber()
        pct = waiting / total * 100

        print("")
        print("  Tong: %d xe | Dung yen: %d (%.0f%%) | V_tb: %.1f m/s | Teleport: %d" %
              (total, waiting, pct, avg_speed, teleported))

        if pct > 70:
            print("  [!!!] UN TAC NGHIEM TRONG")
        elif pct > 40:
            print("  [!!] Un tac trung binh")
        else:
            print("  [OK] Giao thong on dinh")
    print("=" * 75)


# --- MAIN ---

def run():
    print("=" * 60)
    print("  Density-Based Traffic Signal Controller")
    print("  Dieu khien den dua tren MAT DO XE thuc te")
    print("=" * 60)
    print("  Config:     %s" % SUMO_CONFIG)
    print("  Green:      %ds - %ds (gridlock: min %ds)" % (MIN_GREEN, MAX_GREEN, GRIDLOCK_MIN_GREEN))
    print("  Pressure:   >%.1fx to switch" % PRESSURE_RATIO)
    print("  MaxWait:    %ds" % MAX_WAIT_SECONDS)
    print("  Gridlock:   >%.0f%% halting" % (GRIDLOCK_THRESHOLD * 100))
    print("")

    sumo_cmd = [SUMO_BINARY, "-c", SUMO_CONFIG, "--start"]
    traci.start(sumo_cmd)
    print("[OK] Ket noi TraCI thanh cong")
    print("")

    tls_ids = traci.trafficlight.getIDList()
    controllers = {}
    for tid in tls_ids:
        try:
            controllers[tid] = DensityController(tid)
        except Exception as e:
            print("  [WARN] Bo qua %s: %s" % (tid, str(e)))

    print("")
    print("  Dieu khien %d nut den: %s" % (len(controllers), str(list(controllers.keys()))))
    print("")

    last_dash = 0

    while traci.simulation.getMinExpectedNumber() > 0:
        traci.simulationStep()
        sim_time = traci.simulation.getTime()

        # Kiem tra gridlock
        is_gridlock, waiting, total = check_gridlock()

        # Dieu khien den
        for tid, ctrl in controllers.items():
            try:
                ctrl.step(sim_time, is_gridlock)
            except Exception as e:
                pass

        # Dashboard
        if sim_time - last_dash >= DASHBOARD_INTERVAL:
            print_dashboard(sim_time, controllers, is_gridlock, waiting, total)
            last_dash = sim_time

    traci.close()
    print("")
    print("[OK] Mo phong ket thuc!")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n[STOP] Dung")
        try: traci.close()
        except: pass
    except Exception as e:
        import traceback; traceback.print_exc()
        try: traci.close()
        except: pass