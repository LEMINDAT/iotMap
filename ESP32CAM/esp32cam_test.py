from __future__ import annotations

import argparse
import pathlib
import sys
import time
from urllib.parse import urlparse


def normalize_capture_url(raw_url: str) -> str:
    url = raw_url.strip().rstrip("/")
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "http://" + url
        parsed = urlparse(url)

    if parsed.path in ("", "/"):
        return url.rstrip("/") + "/capture"
    return url


def require_cv2_numpy():
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Missing image dependencies. Run: python -m pip install -r requirements.txt") from exc
    return cv2, np


def fetch_frame(url: str, timeout: float) -> np.ndarray:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests is missing. Run: python -m pip install -r requirements.txt") from exc

    response = requests.get(url, timeout=timeout)
    response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    if "image" not in content_type and not response.content.startswith(b"\xff\xd8"):
        raise RuntimeError(f"Endpoint did not return an image. Content-Type: {content_type}")

    return decode_jpeg(response.content)


def decode_jpeg(data: bytes) -> np.ndarray:
    cv2, np = require_cv2_numpy()
    frame_data = np.frombuffer(data, dtype=np.uint8)
    frame = cv2.imdecode(frame_data, cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("Could not decode JPEG frame from serial response.")
    return frame


def read_exact(serial_connection, length: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    remaining = length

    while remaining > 0 and time.monotonic() < deadline:
        chunk = serial_connection.read(remaining)
        if not chunk:
            continue
        chunks.append(chunk)
        remaining -= len(chunk)

    if remaining:
        received = length - remaining
        raise TimeoutError(f"Serial frame timed out after {received}/{length} bytes.")

    return b"".join(chunks)


def fetch_serial_frame(serial_connection, timeout: float) -> np.ndarray:
    serial_connection.reset_input_buffer()
    serial_connection.write(b"CAPTURE\n")
    serial_connection.flush()

    deadline = time.monotonic() + timeout
    image_length: int | None = None

    while time.monotonic() < deadline:
        line = serial_connection.readline()
        if not line:
            continue

        text = line.strip()
        if text.startswith(b"IMG:"):
            image_length = int(text.split(b":", 1)[1])
            break
        if text.startswith(b"ERR:"):
            raise RuntimeError(text.decode("utf-8", errors="replace"))
        if text:
            print(f"[esp32] {text.decode('utf-8', errors='replace')}", file=sys.stderr)

    if image_length is None:
        raise TimeoutError("Did not receive IMG:<bytes> header from ESP32-CAM.")

    jpeg_data = read_exact(serial_connection, image_length, timeout)

    # Consume the trailing END marker if it has arrived. It is not required for decoding.
    marker_deadline = time.monotonic() + 0.5
    while time.monotonic() < marker_deadline:
        marker = serial_connection.readline().strip()
        if marker == b"END":
            break

    return decode_jpeg(jpeg_data)


def open_serial(port: str, baud: int, timeout: float):
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("pyserial is missing. Run: python -m pip install -r requirements.txt") from exc

    serial_connection = serial.Serial(port=port, baudrate=baud, timeout=0.2, write_timeout=timeout)
    time.sleep(2.0)
    serial_connection.reset_input_buffer()
    return serial_connection


def save_frame(frame: np.ndarray, output_path: pathlib.Path) -> None:
    cv2, _ = require_cv2_numpy()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), frame):
        raise RuntimeError(f"Could not write image to {output_path}")
    print(f"Saved {output_path} ({frame.shape[1]}x{frame.shape[0]})")


def save_capture(url: str, output_path: pathlib.Path, timeout: float) -> None:
    save_frame(fetch_frame(url, timeout), output_path)


def preview_loop(frame_source, timeout: float, delay: float) -> None:
    cv2, _ = require_cv2_numpy()
    print("Press q or Esc to close preview.")
    while True:
        started = time.monotonic()
        frame = frame_source(timeout)
        elapsed = max(time.monotonic() - started, 0.001)

        cv2.putText(
            frame,
            f"{frame.shape[1]}x{frame.shape[0]}  {1 / elapsed:.1f} fps",
            (12, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )
        cv2.imshow("ESP32-CAM preview", frame)

        key = cv2.waitKey(max(int(delay * 1000), 1)) & 0xFF
        if key in (27, ord("q")):
            break

    cv2.destroyAllWindows()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch JPEG images from an ESP32-CAM.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="ESP32-CAM base URL or capture URL for Wi-Fi/HTTP mode.")
    source.add_argument("--port", help="Serial port for direct USB mode, for example COM5.")
    parser.add_argument("--baud", type=int, default=921600, help="Serial baud rate for --port mode.")
    parser.add_argument("--output", default="capture.jpg", help="Output image path.")
    parser.add_argument("--preview", action="store_true", help="Show repeated captures in a preview window.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Operation timeout in seconds.")
    parser.add_argument("--delay", type=float, default=0.15, help="Delay between preview frames.")
    args = parser.parse_args()

    if args.url:
        capture_url = normalize_capture_url(args.url)
        print(f"Using capture URL: {capture_url}")
        if args.preview:
            preview_loop(lambda timeout: fetch_frame(capture_url, timeout), args.timeout, args.delay)
        else:
            save_capture(capture_url, pathlib.Path(args.output), args.timeout)
        return

    with open_serial(args.port, args.baud, args.timeout) as serial_connection:
        print(f"Using serial port: {args.port} at {args.baud} baud")
        if args.preview:
            preview_loop(lambda timeout: fetch_serial_frame(serial_connection, timeout), args.timeout, args.delay)
        else:
            save_frame(fetch_serial_frame(serial_connection, args.timeout), pathlib.Path(args.output))


if __name__ == "__main__":
    main()
