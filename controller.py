"""
controller.py

Input:
  - so xe may
  - so o to

Output:
  - red
  - yellow
  - green

Vi du:
  python controller.py 5 3
"""

import argparse


PCE_MOTORCYCLE = 0.5
PCE_CAR = 1.0

YELLOW_THRESHOLD_PCE = 1.0
GREEN_THRESHOLD_PCE = 5.0


def calculate_pce(motorcycles: int, cars: int) -> float:
    """Quy doi so xe ve PCE."""
    return motorcycles * PCE_MOTORCYCLE + cars * PCE_CAR


def decide_light(motorcycles: int, cars: int) -> str:
    """
    Quyet dinh mau den dua tren so xe may va o to.

    Thuat toan:
      - xe may = 0.5 PCE
      - o to    = 1.0 PCE
      - PCE >= 5.0 -> green
      - PCE >= 1.0 -> yellow
      - PCE <  1.0 -> red
    """
    pce = calculate_pce(motorcycles, cars)

    if pce >= GREEN_THRESHOLD_PCE:
        return "green"
    if pce >= YELLOW_THRESHOLD_PCE:
        return "yellow"
    return "red"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Simple traffic light controller")
    parser.add_argument("motorcycles", type=int, help="So xe may")
    parser.add_argument("cars", type=int, help="So o to")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    print(decide_light(args.motorcycles, args.cars))


if __name__ == "__main__":
    main()
