#!/usr/bin/env python3
"""터치 이벤트를 있는 그대로 덤프한다. 왜 안 잡히는지 추측하지 않고 확인하기 위한 도구.

LCD 에 "TOUCH NOW" 안내를 띄우고 지정 시간 동안 /dev/input/eventN 을 블로킹으로 읽어
모든 이벤트를 stdout 에 찍는다.

    sudo python3 touch-dump.py [초]
"""
import glob
import os
import struct
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

DRIVER = "fb_ili9486"
TOUCH_NAME = "ADS7846"
# native long 을 써야 32bit/64bit userland 양쪽에서 크기가 맞는다 (아래 주석 참조)
FMT = "llHHi"
SZ = struct.calcsize(FMT)

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
NAMES = {
    (EV_SYN, 0): "SYN_REPORT",
    (EV_KEY, 0x14A): "BTN_TOUCH",
    (EV_ABS, 0x00): "ABS_X",
    (EV_ABS, 0x01): "ABS_Y",
    (EV_ABS, 0x18): "ABS_PRESSURE",
}


def find_fb():
    for path in sorted(glob.glob("/sys/class/graphics/fb*")):
        try:
            if open(os.path.join(path, "name")).read().strip() != DRIVER:
                continue
            w, h = open(os.path.join(path, "virtual_size")).read().strip().split(",")
        except (OSError, ValueError):
            continue
        return "/dev/" + os.path.basename(path), int(w), int(h)
    raise SystemExit(f"{DRIVER} 프레임버퍼 없음")


def find_touch():
    for path in sorted(glob.glob("/sys/class/input/event*")):
        try:
            if TOUCH_NAME in open(os.path.join(path, "device", "name")).read():
                return "/dev/input/" + os.path.basename(path)
        except OSError:
            continue
    raise SystemExit(f"{TOUCH_NAME} 없음")


def banner(seconds: int) -> None:
    dev, w, h = find_fb()
    img = Image.new("RGB", (w, h), (12, 14, 20))
    d = ImageDraw.Draw(img)
    fdir = "/usr/share/fonts/truetype/dejavu"
    big = ImageFont.truetype(f"{fdir}/DejaVuSans-Bold.ttf", 42)
    mid = ImageFont.truetype(f"{fdir}/DejaVuSans.ttf", 20)
    small = ImageFont.truetype(f"{fdir}/DejaVuSans.ttf", 15)

    def centre(y, text, font, fill):
        bb = d.textbbox((0, 0), text, font=font)
        d.text(((w - (bb[2] - bb[0])) // 2 - bb[0], y), text, font=font, fill=fill)

    d.rectangle([0, 0, w, 6], fill=(196, 24, 71))
    centre(70, "TOUCH NOW", big, (255, 205, 70))
    centre(140, f"press / drag anywhere for {seconds}s", mid, (240, 240, 240))
    centre(180, "left, right, several taps, one long press", small, (150, 155, 168))
    centre(240, "raw events are being recorded", small, (110, 115, 130))
    a = np.asarray(img, dtype=np.uint16)
    rgb565 = ((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)
    with open(dev, "wb") as fb:
        fb.write(rgb565.astype("<u2").tobytes())


def main() -> None:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    path = find_touch()
    banner(seconds)
    print(f"device={path} recording {seconds}s", flush=True)

    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    end = time.time() + seconds
    total = 0
    batches = 0
    while time.time() < end:
        try:
            data = os.read(fd, SZ * 64)
        except BlockingIOError:
            time.sleep(0.005)
            continue
        if not data:
            continue
        batches += 1
        parts = []
        for off in range(0, len(data) - SZ + 1, SZ):
            _, _, etype, code, value = struct.unpack_from(FMT, data, off)
            parts.append(f"{NAMES.get((etype, code), f'{etype}/{code}')}={value}")
            total += 1
        # 한 번의 read 에 무엇이 함께 들어오는지가 핵심이라 배치 단위로 찍는다
        print(f"batch#{batches:03d} ({len(parts)}) " + " ".join(parts), flush=True)

    print(f"DONE total_events={total} batches={batches}", flush=True)
    os.close(fd)


if __name__ == "__main__":
    main()
