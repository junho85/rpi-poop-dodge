#!/usr/bin/env python3
"""pygame-on-lcd.py — pygame 게임을 SPI LCD(프레임버퍼)에서 그대로 돌리는 런처.

**게임 코드를 한 줄도 고치지 않는다.** 네 가지를 런처가 대신 처리한다.

1. **화면** — SDL2 에는 fbdev 백엔드가 없어서 pygame 이 /dev/fbN 에 직접 못 그린다.
   그래서 SDL_VIDEODRIVER=dummy 로 띄우고, display.flip()/update() 를 후크해서
   화면 Surface 를 RGB565 로 변환해 프레임버퍼에 복사한다.
2. **시리얼** — 윈도우에서 만든 코드의 'COM4' 같은 포트명을 /dev/ttyACM* 로 보정한다.
   아두이노가 없으면 LCD 터치를 버튼 입력처럼 흘려보내 게임이 그대로 돌아가게 한다.
   아두이노가 있으면 실제 시리얼과 터치를 **둘 다** 받는다.
3. **이미지** — 없는 리소스는 자리표시 Surface 로 대체한다. 그림이 없어도 일단 플레이된다.
4. **해상도** — 게임 화면이 LCD 와 다르면 자동으로 축소·확대한다.

사용법.
    sudo python3 pygame-on-lcd.py your-game.py
    sudo python3 pygame-on-lcd.py your-game.py --key J     # 터치를 어떤 문자로 보낼지 (기본 J)
    sudo python3 pygame-on-lcd.py your-game.py --no-touch  # 터치 주입 없이 실제 시리얼만

프레임버퍼와 입력 장치가 root 소유라 sudo 가 필요하다.
"""
from __future__ import annotations

import argparse
import glob
import os
import runpy
import struct
import sys

DRIVER = "fb_ili9486"        # 다른 패널이면 --driver 로 바꾼다
TOUCH_NAME = "ADS7846"

# struct input_event = timeval(long 2개) + type + code + value.
# native long "l" 을 써야 32bit(16B)/64bit(24B) 양쪽에서 크기가 맞는다.
EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)
EV_KEY, EV_ABS = 0x01, 0x03
BTN_TOUCH = 0x14A


def log(msg: str) -> None:
    print(f"[lcd] {msg}", flush=True)


# --------------------------------------------------------------- framebuffer
def find_fb(driver: str) -> tuple[str, int, int]:
    """번호가 아니라 드라이버 이름으로 찾는다. fb0/fb1 은 부팅마다 뒤집힌다."""
    for path in sorted(glob.glob("/sys/class/graphics/fb*")):
        try:
            if open(os.path.join(path, "name")).read().strip() != driver:
                continue
            w, h = open(os.path.join(path, "virtual_size")).read().strip().split(",")
        except (OSError, ValueError):
            continue
        return "/dev/" + os.path.basename(path), int(w), int(h)
    raise SystemExit(
        f"{driver} 프레임버퍼를 찾을 수 없다.\n"
        "  /boot/firmware/config.txt 의 dtoverlay 설정과 dmesg 를 확인하라."
    )


# --------------------------------------------------------------------- touch
def find_touch() -> str | None:
    for path in sorted(glob.glob("/sys/class/input/event*")):
        try:
            if TOUCH_NAME in open(os.path.join(path, "device", "name")).read():
                return "/dev/input/" + os.path.basename(path)
        except OSError:
            continue
    return None


class Touch:
    """탭만 센다. 점프 같은 버튼 입력에는 좌표가 필요 없어 보정도 필요 없다."""

    def __init__(self, path: str) -> None:
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.pressed = False
        self.tap_count = 0

    def poll(self) -> None:
        while True:
            try:
                data = os.read(self.fd, EVENT_SIZE * 32)
            except BlockingIOError:
                return
            if not data:
                return
            for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _, _, etype, code, value = struct.unpack_from(EVENT_FMT, data, off)
                # 눌림은 BTN_TOUCH 만 믿는다. ABS_PRESSURE 는 항상 0 일 수 있고,
                # 짧은 탭은 press/release 가 한 배치에 오므로 카운터로 latch 한다.
                if etype == EV_KEY and code == BTN_TOUCH:
                    was = self.pressed
                    self.pressed = value == 1
                    if self.pressed and not was:
                        self.tap_count += 1

    def take_taps(self) -> int:
        n, self.tap_count = self.tap_count, 0
        return n


class TouchSerial:
    """터치 탭을 아두이노 버튼 문자처럼 보이게 하는 시리얼 대역.

    실제 시리얼이 열려 있으면 그쪽 데이터도 함께 흘려보낸다.
    게임 코드는 `ser.in_waiting` / `ser.read()` 만 쓰므로 그 둘이 핵심이다.
    """

    def __init__(self, touch: Touch | None, real=None, key: bytes = b"J") -> None:
        self.touch, self.real, self.key = touch, real, key
        self.buf = bytearray()

    @property
    def in_waiting(self) -> int:
        if self.touch is not None:
            self.touch.poll()
            self.buf += self.key * self.touch.take_taps()
        if self.real is not None:
            try:
                n = self.real.in_waiting
                if n:
                    self.buf += self.real.read(n)
            except Exception:                      # 아두이노를 빼도 게임은 계속
                self.real = None
        return len(self.buf)

    def read(self, size: int = 1) -> bytes:
        if not self.buf:
            self.in_waiting
        out = bytes(self.buf[:size])
        del self.buf[:size]
        return out

    def readline(self) -> bytes:
        self.in_waiting
        idx = self.buf.find(b"\n")
        if idx < 0:
            out, self.buf = bytes(self.buf), bytearray()
            return out
        out = bytes(self.buf[: idx + 1])
        del self.buf[: idx + 1]
        return out

    def write(self, data) -> int:
        return self.real.write(data) if self.real is not None else len(data)

    def flush(self) -> None:
        if self.real is not None:
            self.real.flush()

    def close(self) -> None:
        if self.real is not None:
            self.real.close()

    @property
    def is_open(self) -> bool:
        return True


# ----------------------------------------------------------------- patching
def patch_serial(touch: Touch | None, key: bytes) -> None:
    try:
        import serial
    except ImportError:
        return

    real_cls = serial.Serial

    def Serial(port=None, *args, **kwargs):
        chosen = port
        if isinstance(port, str) and (port.upper().startswith("COM")
                                      or not os.path.exists(port)):
            cands = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
            chosen = cands[0] if cands else None
            log(f"serial {port!r} -> {chosen!r}" if chosen
                else f"serial {port!r}: 장치가 없다 → 터치 입력으로 대체")

        real = None
        if chosen:
            try:
                real = real_cls(chosen, *args, **kwargs)
                log(f"serial 열림: {chosen}")
            except Exception as e:                 # 권한·점유 등
                log(f"serial {chosen} 열기 실패({e}) → 터치 입력으로 대체")

        if touch is None and real is not None:
            return real
        return TouchSerial(touch, real, key)

    serial.Serial = Serial


def patch_pygame(dev: str, fb_w: int, fb_h: int) -> None:
    import numpy as np
    import pygame

    fb = open(dev, "wb", buffering=0)
    state = {"warned": False}

    def push() -> None:
        surface = pygame.display.get_surface()
        if surface is None:
            return
        if surface.get_size() != (fb_w, fb_h):
            if not state["warned"]:
                log(f"게임 화면 {surface.get_size()} → LCD {(fb_w, fb_h)} 로 스케일")
                state["warned"] = True
            surface = pygame.transform.smoothscale(surface, (fb_w, fb_h))
        # surfarray 는 (W, H, 3) 축이라 transpose 가 필요하다
        a = np.transpose(pygame.surfarray.array3d(surface), (1, 0, 2)).astype(np.uint16)
        rgb565 = ((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)
        fb.seek(0)
        fb.write(rgb565.astype("<u2").tobytes())

    orig_flip, orig_update = pygame.display.flip, pygame.display.update

    def flip():
        orig_flip()
        push()

    def update(*args, **kwargs):
        orig_update(*args, **kwargs)
        push()

    pygame.display.flip = flip
    pygame.display.update = update

    # 없는 이미지는 자리표시로 — 그림이 없어도 일단 플레이되게 한다
    orig_load = pygame.image.load

    def load(path, *args, **kwargs):
        try:
            return orig_load(path, *args, **kwargs)
        except Exception as e:
            log(f"이미지 없음 {path!r} ({e.__class__.__name__}) → 자리표시로 대체")
            surf = pygame.Surface((64, 64))
            surf.fill((235, 180, 60))
            pygame.draw.rect(surf, (60, 45, 20), surf.get_rect(), 4)
            pygame.draw.line(surf, (60, 45, 20), (0, 0), (64, 64), 3)
            pygame.draw.line(surf, (60, 45, 20), (64, 0), (0, 64), 3)
            return surf

    pygame.image.load = load


# ---------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="pygame 게임을 SPI LCD 에서 실행")
    ap.add_argument("game", help="실행할 게임 .py 경로")
    ap.add_argument("--key", default="J", help="터치 탭을 시리얼로 보낼 문자 (기본 J)")
    ap.add_argument("--driver", default=DRIVER, help=f"프레임버퍼 드라이버 (기본 {DRIVER})")
    ap.add_argument("--no-touch", action="store_true", help="터치 주입을 끈다")
    args = ap.parse_args()

    game = os.path.abspath(args.game)
    if not os.path.exists(game):
        raise SystemExit(f"게임 파일이 없다: {game}")

    dev, w, h = find_fb(args.driver)
    log(f"framebuffer {dev} {w}x{h}")

    touch = None
    if not args.no_touch:
        tpath = find_touch()
        if tpath:
            touch = Touch(tpath)
            log(f"터치 {tpath} → 탭을 '{args.key}' 로 주입")
        else:
            log("터치 장치를 찾지 못했다 (시리얼만 사용)")

    # SDL 은 창을 만들지 않는다. 그려진 Surface 만 가져다 프레임버퍼에 쓴다.
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")   # 사운드 장치 없어도 죽지 않게

    patch_serial(touch, args.key.encode())
    patch_pygame(dev, w, h)

    # 게임이 같은 폴더의 리소스를 상대 경로로 읽으므로 작업 디렉터리를 옮긴다
    os.chdir(os.path.dirname(game) or ".")
    sys.argv = [game]
    log(f"실행: {game}")
    try:
        runpy.run_path(game, run_name="__main__")
    except KeyboardInterrupt:
        log("종료 (Ctrl+C)")
    except SystemExit:
        log("게임이 종료를 요청했다")


if __name__ == "__main__":
    main()
