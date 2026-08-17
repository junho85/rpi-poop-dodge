#!/usr/bin/env python3
"""Waveshare 3.5" SPI LCD(/dev/fb1)에 Pi 상태를 표시한다.

fb1은 RGB565(16bpp)이므로 PIL RGB 이미지를 직접 변환해 써넣는다.
사용법: python3 lcd-status.py         (1회 그리기)
        python3 lcd-status.py --loop  (5초 간격 갱신, Ctrl+C 종료)
"""
import glob
import os
import socket
import subprocess
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

DRIVER = "fb_ili9486"   # 이 드라이버가 붙은 프레임버퍼를 찾는다
FONT_DIR = "/usr/share/fonts/truetype/dejavu"


def find_fb():
    """fb 번호는 부팅마다 바뀐다(vc4 KMS 등록 타이밍에 따라 fb0/fb1이 뒤집힌다).

    번호를 하드코딩하지 말고 /sys/class/graphics/fb*/name 으로 드라이버를 보고 찾는다.
    반환. (장치경로, 폭, 높이)
    """
    for path in sorted(glob.glob("/sys/class/graphics/fb*")):
        try:
            if open(os.path.join(path, "name")).read().strip() != DRIVER:
                continue
            w, h = open(os.path.join(path, "virtual_size")).read().strip().split(",")
        except (OSError, ValueError):
            continue
        return "/dev/" + os.path.basename(path), int(w), int(h)
    raise SystemExit(
        f"{DRIVER} 프레임버퍼를 찾을 수 없다. "
        "`dtoverlay=piscreen` 적용 여부와 `dmesg | grep ili9486` 을 확인하라."
    )


FB, W, H = find_fb()


def load_font(size, bold=False):
    name = "DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, name), size)
    except OSError:
        return ImageFont.load_default()


def sh(cmd, default="?"):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=5).strip()
    except Exception:
        return default


def ip_addr():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        addr = s.getsockname()[0]
        s.close()
        return addr
    except OSError:
        return "no network"


def collect():
    temp = sh("vcgencmd measure_temp").replace("temp=", "")
    thr = sh("vcgencmd get_throttled").replace("throttled=", "")
    clock_hz = sh("vcgencmd measure_clock arm").split("=")[-1]
    try:
        clock = f"{int(clock_hz) // 1_000_000} MHz"
    except ValueError:
        clock = "?"
    load = open("/proc/loadavg").read().split()[0]
    with open("/proc/meminfo") as f:
        mem = {k.rstrip(":"): int(v) for k, v, *_ in (l.split() for l in f)}
    mem_used_mb = (mem["MemTotal"] - mem["MemAvailable"]) // 1024
    mem_total_mb = mem["MemTotal"] // 1024
    disk = sh("df -h / | awk 'NR==2{print $3\"/\"$2\" (\"$5\")\"}'")
    up = sh("uptime -p").replace("up ", "")
    return [
        ("호스트", socket.gethostname()),
        ("IP", ip_addr()),
        ("온도", temp),
        ("클록", clock),
        ("스로틀", thr),
        ("부하", load),
        ("메모리", f"{mem_used_mb}/{mem_total_mb} MB"),
        ("디스크", disk),
        ("가동", up),
    ]


def render(rows):
    RASPBERRY = (196, 24, 71)
    img = Image.new("RGB", (W, H), (16, 16, 20))
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W, 44], fill=RASPBERRY)
    d.text((14, 10), "Raspberry Pi 3B", font=load_font(24, bold=True), fill=(255, 255, 255))
    d.text((W - 96, 15), time.strftime("%H:%M:%S"), font=load_font(18), fill=(255, 220, 225))

    label_font = load_font(17)
    value_font = load_font(17, bold=True)
    y = 60
    for key, value in rows:
        d.text((16, y), f"{key:>4}", font=label_font, fill=(130, 135, 145))
        colour = (255, 255, 255)
        if key == "스로틀":
            colour = (90, 220, 120) if value == "0x0" else (255, 90, 90)
        d.text((116, y), str(value), font=value_font, fill=colour)
        y += 27

    d.line([0, H - 22, W, H - 22], fill=(60, 62, 70))
    d.text((16, H - 19), f"{DRIVER} @ {FB} / {W}x{H} / dtoverlay=piscreen",
           font=load_font(14), fill=(110, 115, 125))
    return img


def to_rgb565(img):
    a = np.asarray(img, dtype=np.uint16)
    r = (a[:, :, 0] >> 3) << 11
    g = (a[:, :, 1] >> 2) << 5
    b = a[:, :, 2] >> 3
    return (r | g | b).astype("<u2")


def draw_once():
    with open(FB, "wb") as fb:
        to_rgb565(render(collect())).tofile(fb)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        try:
            while True:
                draw_once()
                time.sleep(5)
        except KeyboardInterrupt:
            pass
    else:
        draw_once()
