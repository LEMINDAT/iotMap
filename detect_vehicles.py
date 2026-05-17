"""YOLO vehicle detection for cars and motorcycles.

Usage:
	python detect_vehicles.py --source image.jpg
	python detect_vehicles.py --source path/to/video.mp4

This script uses the standard Ultralytics YOLO11s COCO model.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
from ultralytics import YOLO


TARGET_CLASSES = {2: "car", 3: "motorcycle"}


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Detect cars and motorcycles with YOLO11s.")
	parser.add_argument(
		"--source",
		default="pic2.png",
		help="Image or video path. Default: pic2.png",
	)
	parser.add_argument(
		"--model",
		default="yolo11s.pt",
		help="Ultralytics YOLO11s model path. Default: yolo11s.pt",
	)
	parser.add_argument(
		"--conf",
		type=float,
		default=0.17,
		help="Confidence threshold. Default: 0.25",
	)
	parser.add_argument(
		"--imgsz",
		type=int,
		default=1280,
		help="Inference image size. Default: 1280",
	)
	parser.add_argument(
		"--save",
		action="store_true",
		help="Save annotated video output to runs/detect/",
	)
	return parser.parse_args()


def is_image_file(path: str) -> bool:
	return Path(path).suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def draw_boxes(frame, result) -> None:
	boxes = result.boxes
	if boxes is None:
		return

	for box in boxes:
		class_id = int(box.cls[0])
		if class_id not in TARGET_CLASSES:
			continue

		x1, y1, x2, y2 = map(int, box.xyxy[0])
		confidence = float(box.conf[0])
		label = f"{TARGET_CLASSES[class_id]} {confidence:.2f}"
		color = (0, 255, 0) if class_id == 2 else (255, 0, 0)

		cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
		cv2.putText(
			frame,
			label,
			(x1, max(25, y1 - 10)),
			cv2.FONT_HERSHEY_SIMPLEX,
			0.7,
			color,
			2,
			cv2.LINE_AA,
		)


def count_target_classes(result) -> dict[str, int]:
	counts = {"car": 0, "motorcycle": 0}
	boxes = result.boxes
	if boxes is None:
		return counts

	for box in boxes:
		class_id = int(box.cls[0])
		if class_id == 2:
			counts["car"] += 1
		elif class_id == 3:
			counts["motorcycle"] += 1

	return counts


def estimate_occupied_area(result) -> int:
	"""Tinh tong dien tich bbox cua xe may va o to."""
	occupied_area = 0
	boxes = result.boxes
	if boxes is None:
		return occupied_area

	for box in boxes:
		class_id = int(box.cls[0])
		if class_id not in TARGET_CLASSES:
			continue

		x1, y1, x2, y2 = map(int, box.xyxy[0])
		occupied_area += max(0, x2 - x1) * max(0, y2 - y1)

	return occupied_area


def estimate_road_area(frame) -> int:
	"""Tam thoi lay toan bo anh la dien tich duong."""
	height, width = frame.shape[:2]
	return width * height


def run_image(model: YOLO, source: str, conf: float, imgsz: int) -> None:
	result = model.predict(source=source, conf=conf, imgsz=imgsz, classes=[2, 3], verbose=False)[0]
	counts = count_target_classes(result)
	frame = result.orig_img.copy()
	road_area = estimate_road_area(frame)
	occupied_area = estimate_occupied_area(result)
	occupancy_ratio = occupied_area / road_area if road_area > 0 else 0.0

	print(f"car: {counts['car']}")
	print(f"motorcycle: {counts['motorcycle']}")
	print(f"road_area: {road_area}")
	print(f"occupied_area: {occupied_area}")
	print(f"occupancy_ratio: {occupancy_ratio:.4f}")

	output_path = Path("runs/detect")
	output_path.mkdir(parents=True, exist_ok=True)
	draw_boxes(frame, result)
	output_file = output_path / f"{Path(source).stem}_annotated.jpg"
	cv2.imwrite(str(output_file), frame)
	print(f"saved: {output_file}")


def run_video(model: YOLO, source: str, conf: float, imgsz: int, save: bool) -> None:
	cap = cv2.VideoCapture(source)
	if not cap.isOpened():
		raise RuntimeError(f"Cannot open source: {source}")

	writer = None
	output_path = Path("runs/detect")
	if save:
		output_path.mkdir(parents=True, exist_ok=True)

	while True:
		ret, frame = cap.read()
		if not ret:
			break

		result = model.predict(frame, conf=conf, imgsz=imgsz, classes=[2, 3], verbose=False)[0]
		annotated = result.orig_img.copy()
		draw_boxes(annotated, result)

		counts = count_target_classes(result)
		print(f"car: {counts['car']}")
		print(f"motorcycle: {counts['motorcycle']}")

		if save and writer is None:
			height, width = annotated.shape[:2]
			fps = cap.get(cv2.CAP_PROP_FPS)
			if fps <= 0:
				fps = 30.0
			fourcc = cv2.VideoWriter_fourcc(*"mp4v")
			writer = cv2.VideoWriter(str(output_path / "output.mp4"), fourcc, fps, (width, height))

		if writer is not None:
			writer.write(annotated)

	cap.release()
	if writer is not None:
		writer.release()


def main() -> None:
	args = parse_args()
	model = YOLO(args.model)

	source = str(args.source)
	if is_image_file(source):
		run_image(model, source, args.conf, args.imgsz)
	else:
		run_video(model, source, args.conf, args.imgsz, args.save)


if __name__ == "__main__":
	main()
