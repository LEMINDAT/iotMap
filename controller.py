"""
controller.py

Input:
  - so xe may
  - so o to
  - ty le dien tich duong bi chiem, neu co

Output:
  - red
  - green

Vi du:
  python controller.py 5 3
  python controller.py 5 3 --occupancy-ratio 0.12
  python detect_vehicles.py --source pic1.png | python controller.py
"""

import argparse
import sys


PCE_MOTORCYCLE = 0.5
PCE_CAR = 1.0

GREEN_THRESHOLD_PCE = 3.0
OCCUPANCY_WEIGHT = 20.0

SATURATION_FLOW = 1800.0
LOST_TIME = 4.0
MIN_GREEN = 10.0
MAX_GREEN = 60.0
MIN_CYCLE = 30.0
MAX_CYCLE = 120.0
WEBSTER_GREEN_THRESHOLD = 15.0


def calculate_pce(motorcycles: int, cars: int) -> float:
    """Quy doi so xe ve PCE."""
    return motorcycles * PCE_MOTORCYCLE + cars * PCE_CAR


def estimate_flow_pce(motorcycles: int, cars: int, occupancy_ratio: float = 0.0) -> float:
    """Uoc luong luu luong PCE tu so xe va dien tich duong bi chiem."""
    return calculate_pce(motorcycles, cars) + occupancy_ratio * OCCUPANCY_WEIGHT


def webster_green_time(flow_pce: float, n_phases: int = 2) -> float:
    """
    Tinh thoi gian xanh theo Webster.

    Vi chi co du lieu 1 huong, cac pha con lai duoc gan demand nho de
    controller van tinh duoc green_time cho huong dang xet.
    """
    other_flow = 1.0
    flows = [flow_pce] + [other_flow] * (n_phases - 1)
    flows_per_hour = [flow * 60.0 for flow in flows]
    y = [flow / SATURATION_FLOW for flow in flows_per_hour]
    sum_y = sum(y)
    lost = n_phases * LOST_TIME

    if sum_y <= 0:
        return MIN_GREEN
    if sum_y >= 0.95:
        return MAX_GREEN

    cycle = (1.5 * lost + 5.0) / (1.0 - sum_y)
    cycle = max(MIN_CYCLE, min(MAX_CYCLE, cycle))

    green = (cycle - lost) * (y[0] / sum_y)
    return max(MIN_GREEN, min(MAX_GREEN, green))


def decide_light(motorcycles: int, cars: int, occupancy_ratio: float = 0.0) -> str:
    """
    Quyet dinh den xanh/do dua tren mat do xe va dien tich duong bi chiem.

    Thuat toan:
      - xe may = 0.5 PCE
      - o to    = 1.0 PCE
      - flow_pce = PCE + occupancy_ratio * 20
      - tinh green_time bang Webster
      - neu green_time >= 15s -> green
      - neu green_time <  15s -> red

    Muc tieu: uu tien bat xanh khi luu luong du lon de giam nguy co tac duong.
    """
    flow_pce = estimate_flow_pce(motorcycles, cars, occupancy_ratio)
    green_time = webster_green_time(flow_pce)

    if green_time >= WEBSTER_GREEN_THRESHOLD:
        return "green"
    return "red"


def parse_detector_output(text: str) -> tuple[int, int, float]:
    """
    Doc output tu detect_vehicles.py.

    Dinh dang hien tai:
      car: 3
      motorcycle: 5
      occupancy_ratio: 0.0915
    """
    cars = 0
    motorcycles = 0
    occupancy_ratio = 0.0

    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue

        key = key.strip().lower()
        value = value.strip()
        try:
            number = float(value)
        except ValueError:
            continue

        if key == "car":
            cars = int(number)
        elif key == "motorcycle":
            motorcycles = int(number)
        elif key == "occupancy_ratio":
            occupancy_ratio = number

    return motorcycles, cars, occupancy_ratio


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Simple traffic light controller")
    parser.add_argument("motorcycles", nargs="?", type=int, help="So xe may")
    parser.add_argument("cars", nargs="?", type=int, help="So o to")
    parser.add_argument(
        "--occupancy-ratio",
        type=float,
        default=0.0,
        help="Ty le dien tich duong bi xe chiem, vi du 0.12",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.motorcycles is not None and args.cars is not None:
        motorcycles = args.motorcycles
        cars = args.cars
        occupancy_ratio = args.occupancy_ratio
    else:
        motorcycles, cars, occupancy_ratio = parse_detector_output(sys.stdin.read())

    print(decide_light(motorcycles, cars, occupancy_ratio))


if __name__ == "__main__":
    main()
