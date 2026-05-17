"""
zone_calibrator.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Công cụ khảo sát cấu trúc mạng đường SUMO.

Hiển thị thông tin về:
  - Các nút đèn giao thông (TLS) trong map
  - Các pha đèn và trạng thái
  - Các làn đường được điều khiển

Cách dùng:
  1. python zone_calibrator.py
  2. Xem thông tin các nút đèn và lanes
"""

import os
import sys
import xml.etree.ElementTree as ET


def inspect_network(net_file="map.net.xml"):
    """Phân tích file .net.xml và in thông tin nút đèn."""
    if not os.path.exists(net_file):
        print(f"❌ Không tìm thấy {net_file}")
        return

    tree = ET.parse(net_file)
    root = tree.getroot()

    print("━" * 60)
    print("  Zone Calibrator — Khảo sát mạng đường SUMO")
    print("━" * 60)

    # Tìm tất cả nút đèn
    tl_logics = root.findall('tlLogic')
    print(f"\n  Tìm thấy {len(tl_logics)} nút đèn giao thông\n")

    for tl in tl_logics:
        tid = tl.get('id')
        phases = tl.findall('phase')

        print(f"  ╔══ NÚT ĐÈN: {tid} ══╗")
        print(f"  ║  Loại: {tl.get('type', 'static')}")
        print(f"  ║  Số pha: {len(phases)}")

        # In các pha
        for i, p in enumerate(phases):
            state = p.get('state')
            dur = p.get('duration')
            has_green = 'G' in state or 'g' in state
            has_yellow = 'y' in state
            if has_green:
                label = "🟢 XANH"
            elif has_yellow:
                label = "🟡 VÀNG"
            else:
                label = "🔴 ĐỎ"
            print(f"  ║  Pha {i}: {label}  dur={dur}s  state={state}")

        # Tìm connections
        conns = [c for c in root.findall('connection') if c.get('tl') == tid]
        print(f"  ║")
        print(f"  ║  {len(conns)} kết nối:")

        # Nhóm theo edge đầu vào
        from_edges = {}
        for c in conns:
            fr = c.get('from')
            if fr not in from_edges:
                from_edges[fr] = []
            from_edges[fr].append({
                'to': c.get('to'),
                'fromLane': c.get('fromLane'),
                'toLane': c.get('toLane'),
                'linkIndex': c.get('linkIndex'),
                'dir': c.get('dir'),
            })

        for edge, connections in from_edges.items():
            dirs = set(c['dir'] for c in connections)
            dir_str = ', '.join(dirs)
            print(f"  ║    {edge} → ({dir_str})")
            for c in connections:
                dir_label = {
                    's': '→ thẳng', 'l': '← rẽ trái',
                    'r': '→ rẽ phải', 't': '↩ quay đầu'
                }.get(c['dir'], c['dir'])
                print(f"  ║      idx={c['linkIndex']:>2s}  "
                      f"L{c['fromLane']} → {c['to']} L{c['toLane']}  {dir_label}")

        print(f"  ╚{'═'*35}╝\n")


def inspect_with_traci():
    """Khảo sát real-time bằng TraCI (cần SUMO đang chạy)."""
    try:
        import traci
    except ImportError:
        print("❌ Không có thư viện traci")
        return

    print("\n  Đang kết nối SUMO...")
    sumo_cmd = ["sumo-gui", "-c", "sim.sumocfg", "--start"]
    traci.start(sumo_cmd)

    tls_ids = traci.trafficlight.getIDList()
    print(f"\n  {len(tls_ids)} nút đèn: {list(tls_ids)}\n")

    for tid in tls_ids:
        controlled = traci.trafficlight.getControlledLanes(tid)
        unique_lanes = list(set(controlled))
        print(f"  {tid}: {len(unique_lanes)} làn duy nhất")
        for lane in unique_lanes:
            print(f"    - {lane}")

    # Chạy 1 bước để lấy dữ liệu
    traci.simulationStep()

    print("\n  Trạng thái đèn hiện tại:")
    for tid in tls_ids:
        phase = traci.trafficlight.getPhase(tid)
        state = traci.trafficlight.getRedYellowGreenState(tid)
        print(f"  {tid}: pha={phase}  state={state}")

    traci.close()
    print("\n✅ Hoàn thành khảo sát!")


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Phân tích file .net.xml (không cần SUMO chạy)
    inspect_network()

    # Hỏi có muốn khảo sát bằng TraCI không
    print("\n" + "─" * 60)
    choice = input("  Bạn có muốn khảo sát real-time bằng TraCI? (y/n): ").strip()
    if choice.lower() == 'y':
        inspect_with_traci()