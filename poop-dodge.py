#!/usr/bin/env python3
"""똥피하기 — Waveshare 3.5" SPI LCD(fbtft) + ADS7846 터치로 하는 프레임버퍼 게임.

떨어지는 똥을 좌우로 움직여 피한다. 조작은 터치 한 가지뿐 —
화면을 누른 x 위치로 캐릭터가 따라온다.

실행.
    sudo systemctl stop lcd-status     # 상태 화면과 프레임버퍼를 다투므로 먼저 정지
    sudo python3 poop-dodge.py
    sudo python3 poop-dodge.py --recalibrate   # 터치 축을 다시 잡을 때

설계 메모.
  * fb 번호(fb0/fb1)는 부팅마다 바뀐다 → /sys/class/graphics/fb*/name 으로 찾는다.
  * fbtft 프레임버퍼는 RGB565(16bpp)라 PIL RGB 이미지를 직접 변환해 써넣는다.
  * 터치 raw 값은 0~4095이고 rotate=270 때문에 화면 축과 일치하지 않는다.
    그래서 첫 실행 때 좌/우를 눌러보게 해서 어느 축·어느 부호인지 직접 알아낸다.
"""
from __future__ import annotations

import glob
import json
import os
import random
import struct
import sys
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

def log(msg: str) -> None:
    """journalctl -u poop-dodge 로 보이는 진단 로그."""
    print(f"[poop] {msg}", flush=True)


DRIVER = "fb_ili9486"
TOUCH_NAME = "ADS7846"
CALIB_PATH = os.path.expanduser("~/.poop-dodge-calib.json")
# DejaVu 에는 한글 글리프가 없어 네모(□)로 나온다. 한국 로케일로 설치하면
# fonts-nanum 이 함께 깔리므로 나눔을 쓴다. (fc-list :lang=ko 로 확인)
FONT_DIR = "/usr/share/fonts/truetype/nanum"
FONT_REGULAR = "NanumGothic.ttf"
FONT_BOLD = "NanumGothicBold.ttf"
FONT_MONO = "NanumGothicCoding.ttf"      # 숫자 폭이 고정돼 HUD 가 안 흔들린다
FONT_MONO_BOLD = "NanumGothicCodingBold.ttf"
FALLBACK_DIR = "/usr/share/fonts/truetype/dejavu"

# linux/input-event-codes.h
EV_KEY, EV_ABS = 0x01, 0x03
ABS_X, ABS_Y, ABS_PRESSURE = 0x00, 0x01, 0x18
BTN_TOUCH = 0x14A
# struct input_event = struct timeval + __u16 type + __u16 code + __s32 value.
# timeval 은 long 2개라 크기가 userland 비트수에 따라 다르다 — 32bit armhf 16바이트,
# 64bit 24바이트. 그래서 "q"(고정 8바이트)로 박으면 안 되고 native long "l" 을 써야
# 양쪽에서 자동으로 맞는다. (이 Pi 는 arm64 커널 + armv7l 32bit userland 조합이다)
EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

BG = (18, 18, 24)
WHITE = (245, 245, 245)
DIM = (120, 125, 138)
RASPBERRY = (196, 24, 71)
POOP = (110, 72, 34)
POOP_HI = (146, 98, 48)
SKIN = (252, 205, 92)
GOLD = (255, 205, 70)
RED = (232, 72, 72)
GREEN = (86, 210, 122)


# ---------------------------------------------------------------- framebuffer
def find_fb() -> tuple[str, int, int]:
    """드라이버 이름으로 프레임버퍼를 찾는다. 번호 하드코딩은 부팅마다 깨진다."""
    for path in sorted(glob.glob("/sys/class/graphics/fb*")):
        try:
            if open(os.path.join(path, "name")).read().strip() != DRIVER:
                continue
            w, h = open(os.path.join(path, "virtual_size")).read().strip().split(",")
        except (OSError, ValueError):
            continue
        return "/dev/" + os.path.basename(path), int(w), int(h)
    raise SystemExit(
        f"{DRIVER} 프레임버퍼가 없다. `dtoverlay=piscreen` 적용 여부와 "
        "`dmesg | grep -i ili9486` 을 확인하라."
    )


class Screen:
    def __init__(self) -> None:
        self.dev, self.w, self.h = find_fb()
        self.fb = open(self.dev, "wb", buffering=0)
        self.img = Image.new("RGB", (self.w, self.h), BG)
        self.draw = ImageDraw.Draw(self.img)
        self._fonts: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}

    def font(self, size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
        key = (size, bold, mono)
        if key not in self._fonts:
            if mono:
                name = FONT_MONO_BOLD if bold else FONT_MONO
            else:
                name = FONT_BOLD if bold else FONT_REGULAR
            candidates = [
                os.path.join(FONT_DIR, name),
                os.path.join(FALLBACK_DIR, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
            ]
            for path in candidates:
                try:
                    self._fonts[key] = ImageFont.truetype(path, size)
                    break
                except OSError:
                    continue
            else:
                self._fonts[key] = ImageFont.load_default()
        return self._fonts[key]

    def clear(self, colour=BG) -> None:
        self.draw.rectangle([0, 0, self.w, self.h], fill=colour)

    def centre_text(self, y: int, text: str, size: int, colour=WHITE, bold=False,
                    mono=False) -> None:
        f = self.font(size, bold, mono)
        x0, y0, x1, y1 = self.draw.textbbox((0, 0), text, font=f)
        self.draw.text(((self.w - (x1 - x0)) // 2 - x0, y), text, font=f, fill=colour)

    def flush(self) -> None:
        a = np.asarray(self.img, dtype=np.uint16)
        rgb565 = ((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)
        self.fb.seek(0)
        self.fb.write(rgb565.astype("<u2").tobytes())


# --------------------------------------------------------------------- touch
def find_touch() -> str:
    for path in sorted(glob.glob("/sys/class/input/event*")):
        try:
            if TOUCH_NAME in open(os.path.join(path, "device", "name")).read():
                return "/dev/input/" + os.path.basename(path)
        except OSError:
            continue
    raise SystemExit(f"{TOUCH_NAME} 터치 장치를 찾을 수 없다.")


class Touch:
    """논블로킹으로 최신 터치 상태만 유지한다. 게임 루프를 막지 않는 게 요점."""

    RAW_MAX = 4095

    def __init__(self) -> None:
        self.fd = os.open(find_touch(), os.O_RDONLY | os.O_NONBLOCK)
        self.raw_x = self.raw_y = 0
        self.raw_pressure = 0
        self.pressed = False
        self.tap_count = 0      # 소비되지 않은 "눌림 시작" 횟수
        self.saw_coords = False  # 좌표 이벤트를 한 번이라도 받았는가 (진단용)

    def poll(self) -> None:
        """대기 중인 이벤트를 모두 소비하고 최신 상태만 남긴다.

        ⚠️ 두 가지 함정을 함께 피한다.
        1. 눌림 판정은 BTN_TOUCH 하나만 믿는다. 이 ADS7846은 `xohms`(x-plate-ohms)
           설정이 없어 ABS_PRESSURE 를 항상 0 으로 보고하는데, 이벤트가
           BTN_TOUCH=1 → ABS_X → ABS_Y → ABS_PRESSURE=0 → SYN 순으로 오기 때문에
           pressure 로 판정하면 누른 즉시 "뗀 것"으로 덮여버린다.
        2. 짧게 톡 누르면 BTN_TOUCH=1 과 =0 이 **한 번의 read 배치에 함께** 들어온다.
           그래서 처리 후 `pressed` 만 보면 항상 False 다. 눌림 시작을 tap_count 로
           latch 해서 호출자가 take_tap() 으로 소비하게 한다.
        """
        while True:
            try:
                data = os.read(self.fd, EVENT_SIZE * 32)
            except BlockingIOError:
                return
            if not data:
                return
            for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                _, _, etype, code, value = struct.unpack_from(EVENT_FMT, data, off)
                if etype == EV_ABS:
                    if code == ABS_X:
                        self.raw_x = value
                        self.saw_coords = True
                    elif code == ABS_Y:
                        self.raw_y = value
                        self.saw_coords = True
                    elif code == ABS_PRESSURE:
                        self.raw_pressure = value  # 기록만. 판정에는 쓰지 않는다
                elif etype == EV_KEY and code == BTN_TOUCH:
                    was = self.pressed
                    self.pressed = value == 1
                    if self.pressed and not was:
                        self.tap_count += 1


    def take_tap(self) -> bool:
        """눌림 시작이 있었으면 True 를 주고 카운터를 비운다."""
        if self.tap_count:
            self.tap_count = 0
            return True
        return False

    def contact(self) -> bool:
        """지금 누르고 있거나, 직전에 톡 눌렀다 뗀 것을 아직 안 읽었다면 True."""
        return self.pressed or self.tap_count > 0


@dataclass
class Calibration:
    axis: str  # 화면 가로축에 대응하는 터치 축 ("x" 또는 "y")
    lo: int    # 화면 왼쪽에 해당하는 raw 값
    hi: int    # 화면 오른쪽에 해당하는 raw 값

    def to_screen_x(self, touch: Touch, width: int) -> float:
        raw = touch.raw_x if self.axis == "x" else touch.raw_y
        span = self.hi - self.lo
        if span == 0:
            return width / 2
        return min(max((raw - self.lo) / span, 0.0), 1.0) * width

    def save(self) -> None:
        with open(CALIB_PATH, "w") as f:
            json.dump({"axis": self.axis, "lo": self.lo, "hi": self.hi}, f)

    @staticmethod
    def load() -> "Calibration | None":
        try:
            with open(CALIB_PATH) as f:
                d = json.load(f)
            return Calibration(d["axis"], int(d["lo"]), int(d["hi"]))
        except (OSError, KeyError, ValueError):
            return None


def wait_release(touch: Touch, what: str = "") -> None:
    """손을 뗀 상태가 될 때까지 기다린다. 이미 뗀 상태면 즉시 반환."""
    spun = 0
    while True:
        touch.poll()
        if not touch.pressed:
            if spun:
                log(f"released{(' ' + what) if what else ''} after {spun} polls")
            touch.take_tap()   # 남은 탭 latch 를 버려 다음 단계로 새지 않게 한다
            return
        spun += 1
        time.sleep(0.01)


def wait_press(touch: Touch, on_idle=None) -> None:
    """누를 때까지 기다린다. 짧은 탭도 놓치지 않는다. on_idle 은 화면 갱신용."""
    ticks = 0
    while True:
        touch.poll()
        if touch.pressed or touch.take_tap():
            return
        # flush 는 SPI 로 307KB 를 밀어 100ms 넘게 걸린다. 매 폴링마다 그리면
        # 터치 반응이 그만큼 늦어지므로 갱신은 0.4초에 한 번만.
        if on_idle is not None and ticks % 40 == 0:
            on_idle()
        ticks += 1
        time.sleep(0.01)


def draw_target(scr: Screen, touch: Touch, label: str, side: int) -> None:
    scr.clear()
    scr.draw.rectangle([0, 0, scr.w, 46], fill=RASPBERRY)
    scr.centre_text(12, "TOUCH CALIBRATION", 20, WHITE, bold=True)
    box_w = scr.w // 3
    x0 = 0 if side == 0 else scr.w - box_w
    scr.draw.rectangle([x0, 70, x0 + box_w, scr.h - 66], fill=(52, 56, 68))
    scr.draw.rectangle([x0, 70, x0 + box_w, scr.h - 66], outline=GOLD, width=3)
    arrow = "<<<" if side == 0 else ">>>"
    f = scr.font(44, bold=True)
    bb = scr.draw.textbbox((0, 0), arrow, font=f)
    scr.draw.text((x0 + (box_w - (bb[2] - bb[0])) // 2 - bb[0], (scr.h - 46) // 2),
                  arrow, font=f, fill=GOLD)
    scr.centre_text(scr.h - 56, f"{label} 칸을 손가락으로 누르세요", 18, WHITE)
    state = "TOUCH" if touch.pressed else "----"
    scr.centre_text(scr.h - 34, f"[{state}]  raw {touch.raw_x:>4},{touch.raw_y:>4}", 15,
                    GREEN if touch.pressed else DIM, mono=True)
    scr.centre_text(scr.h - 14, f"{side + 1} / 2 단계", 13, DIM)
    scr.flush()


def calibrate(scr: Screen, touch: Touch) -> Calibration:
    """좌/우를 차례로 눌러보게 해서 화면 가로축에 대응하는 터치 축과 부호를 찾는다."""
    log("calibration start")
    samples: list[tuple[int, int]] = []
    for label, side in (("왼쪽", 0), ("오른쪽", 1)):
        log(f"step {side + 1}: waiting for release before showing target")
        wait_release(touch, "before target")

        log(f"step {side + 1}: target shown, waiting for press")
        draw_target(scr, touch, label, side)
        wait_press(touch, on_idle=lambda: draw_target(scr, touch, label, side))
        log(f"step {side + 1}: pressed at raw=({touch.raw_x},{touch.raw_y}) "
            f"held={touch.pressed}")

        if touch.pressed:        # 누르고 있으면 좌표가 안정될 시간을 준다
            time.sleep(0.2)
            touch.poll()
        # 톡 눌렀다 뗀 경우엔 raw_x/raw_y 가 접촉 시점 좌표로 남아있으므로 그대로 쓴다
        samples.append((touch.raw_x, touch.raw_y))
        log(f"step {side + 1}: sampled {samples[-1]}")

        draw_target(scr, touch, label, side)   # TOUCH 상태를 화면에 보여준다
        wait_release(touch, f"step {side + 1}")

    (lx, ly), (rx, ry) = samples
    axis = "x" if abs(rx - lx) >= abs(ry - ly) else "y"
    lo, hi = (lx, rx) if axis == "x" else (ly, ry)
    log(f"samples left={samples[0]} right={samples[1]} -> axis={axis} lo={lo} hi={hi}")
    if abs(hi - lo) < 200:
        log("delta too small, falling back to axis=x 0..4095")
        axis, lo, hi = "x", 0, Touch.RAW_MAX
    calib = Calibration(axis, lo, hi)
    calib.save()
    log(f"calibration saved to {CALIB_PATH}")

    scr.clear()
    scr.centre_text(scr.h // 2 - 46, "터치 보정 완료", 30, GREEN, bold=True)
    scr.centre_text(scr.h // 2 - 4, f"axis={axis}  lo={lo}  hi={hi}", 17, WHITE, mono=True)
    scr.centre_text(scr.h // 2 + 30, "화면을 누르면 계속", 17, DIM)
    scr.flush()
    wait_for_tap(touch)
    return calib


def wait_for_tap(touch: Touch) -> None:
    wait_release(touch, "before tap")
    wait_press(touch)


# ---------------------------------------------------------------------- game
@dataclass
class Poop:
    x: float
    y: float
    vy: float
    size: int
    wobble: float


class Game:
    PLAYER_R = 17
    PLAYER_Y_MARGIN = 26
    LIVES = 3

    def __init__(self, scr: Screen, touch: Touch, calib: Calibration) -> None:
        self.scr, self.touch, self.calib = scr, touch, calib
        self.reset()

    def reset(self) -> None:
        self.px = self.scr.w / 2
        self.lives = self.LIVES
        self.score = 0
        self.dodged = 0
        self.poops: list[Poop] = []
        self.spawn_timer = 0.0
        self.elapsed = 0.0
        self.hit_flash = 0.0
        self.over = False

    # 난이도는 경과 시간으로만 올린다 (점수로 올리면 잘하는 사람만 어려워진다)
    @property
    def level(self) -> int:
        return 1 + int(self.elapsed // 12)

    @property
    def spawn_interval(self) -> float:
        return max(0.28, 0.95 - 0.075 * (self.level - 1))

    @property
    def fall_speed(self) -> tuple[float, float]:
        base = 62 + 17 * (self.level - 1)
        return base, base * 1.7

    def player_y(self) -> float:
        return self.scr.h - self.PLAYER_Y_MARGIN - self.PLAYER_R

    def update(self, dt: float) -> None:
        self.elapsed += dt
        self.score += dt * 4  # 생존 점수
        if self.hit_flash > 0:
            self.hit_flash = max(0.0, self.hit_flash - dt)

        # 이동 — 터치한 x를 향해 부드럽게 따라간다
        if self.touch.contact():
            target = self.calib.to_screen_x(self.touch, self.scr.w)
            self.px += (target - self.px) * min(1.0, dt * 11)
        self.px = min(max(self.px, self.PLAYER_R), self.scr.w - self.PLAYER_R)

        self.spawn_timer -= dt
        if self.spawn_timer <= 0:
            self.spawn_timer = self.spawn_interval
            lo, hi = self.fall_speed
            size = random.randint(11, 17)
            self.poops.append(
                Poop(
                    x=random.uniform(size, self.scr.w - size),
                    y=-size * 2,
                    vy=random.uniform(lo, hi),
                    size=size,
                    wobble=random.uniform(0, 6.28),
                )
            )

        py = self.player_y()
        alive: list[Poop] = []
        for p in self.poops:
            p.y += p.vy * dt
            p.wobble += dt * 3.2
            if (p.x - self.px) ** 2 + (p.y - py) ** 2 <= (p.size + self.PLAYER_R - 4) ** 2:
                self.lives -= 1
                self.hit_flash = 0.32
                if self.lives <= 0:
                    self.over = True
                continue
            if p.y - p.size > self.scr.h:
                self.dodged += 1
                self.score += 10
                continue
            alive.append(p)
        self.poops = alive

    # ------------------------------------------------------------- rendering
    def draw_poop(self, d: ImageDraw.ImageDraw, p: Poop) -> None:
        s = p.size
        x = p.x + np.sin(p.wobble) * 2.2
        # 아래에서 위로 점점 작아지는 3단 무더기
        for i, (scale, dy) in enumerate(((1.0, 0.55), (0.72, -0.05), (0.44, -0.55))):
            r = s * scale
            cy = p.y + dy * s
            d.ellipse([x - r, cy - r * 0.72, x + r, cy + r * 0.72],
                      fill=POOP if i % 2 == 0 else POOP_HI)
        # 눈 — 있어야 똥처럼 보인다
        eye = max(1.0, s * 0.11)
        ey = p.y - s * 0.5
        for ex in (x - s * 0.17, x + s * 0.17):
            d.ellipse([ex - eye, ey - eye, ex + eye, ey + eye], fill=(20, 20, 20))

    def draw_player(self, d: ImageDraw.ImageDraw) -> None:
        r, y = self.PLAYER_R, self.player_y()
        hurt = self.hit_flash > 0
        d.ellipse([self.px - r, y - r, self.px + r, y + r],
                  fill=RED if hurt else SKIN)
        for ex in (self.px - r * 0.34, self.px + r * 0.34):
            d.ellipse([ex - 2.4, y - 3.6, ex + 2.4, y + 1.2], fill=(30, 30, 36))
        if hurt:  # 찡그린 입
            d.line([self.px - 6, y + 8, self.px + 6, y + 8], fill=(30, 30, 36), width=2)
        else:
            d.arc([self.px - 7, y - 1, self.px + 7, y + 10], 20, 160,
                  fill=(30, 30, 36), width=2)

    def draw_hud(self, d: ImageDraw.ImageDraw, fps: float) -> None:
        scr = self.scr
        d.rectangle([0, 0, scr.w, 30], fill=(30, 31, 40))
        d.text((10, 6), f"{int(self.score):>5}점", font=scr.font(17, bold=True, mono=True),
               fill=GOLD)
        d.text((150, 7), f"LV {self.level}", font=scr.font(16, mono=True), fill=WHITE)
        d.text((222, 7), f"피함 {self.dodged}", font=scr.font(16, mono=True), fill=DIM)
        for i in range(self.LIVES):
            cx = scr.w - 20 - i * 22
            colour = SKIN if i < self.lives else (62, 64, 74)
            d.ellipse([cx - 8, 7, cx + 8, 23], fill=colour)
        d.text((6, scr.h - 15), f"{fps:4.1f} fps", font=scr.font(12, mono=True),
               fill=(78, 82, 94))

    def render(self, fps: float) -> None:
        scr = self.scr
        scr.clear((30, 22, 26) if self.hit_flash > 0 else BG)
        d = scr.draw
        # 바닥선
        d.line([0, scr.h - 12, scr.w, scr.h - 12], fill=(52, 55, 66), width=2)
        for p in self.poops:
            self.draw_poop(d, p)
        self.draw_player(d)
        self.draw_hud(d, fps)
        scr.flush()

    def render_gameover(self) -> None:
        scr = self.scr
        scr.clear()
        scr.draw.rectangle([0, 0, scr.w, 54], fill=RASPBERRY)
        scr.centre_text(12, "게임 오버", 30, WHITE, bold=True)
        scr.centre_text(80, f"{int(self.score)}점", 38, GOLD, bold=True, mono=True)
        scr.centre_text(136, f"피한 똥 {self.dodged}개 · 레벨 {self.level} · {self.elapsed:.0f}초 생존",
                        16, WHITE)
        scr.centre_text(196, "화면을 누르면 다시 시작", 19, GREEN)
        scr.centre_text(226, "Ctrl+C 또는 systemctl stop 으로 종료", 13, DIM)
        scr.flush()

    def title(self) -> None:
        scr = self.scr
        scr.clear()
        scr.draw.rectangle([0, 0, scr.w, 58], fill=RASPBERRY)
        scr.centre_text(14, "똥피하기", 30, WHITE, bold=True)
        d = scr.draw
        for i, x in enumerate((110, 240, 370)):
            self.draw_poop(d, Poop(x=x, y=120 + (i % 2) * 26, vy=0, size=17, wobble=i))
        scr.centre_text(186, "터치한 곳으로 캐릭터가 움직인다", 18, WHITE)
        scr.centre_text(214, "떨어지는 똥을 피해라 · 목숨 3개", 16, DIM)
        scr.centre_text(258, "화면을 누르면 시작", 20, GREEN)
        scr.flush()


def main() -> None:
    scr = Screen()
    touch = Touch()

    calib = None if "--recalibrate" in sys.argv else Calibration.load()
    if calib is None:
        calib = calibrate(scr, touch)

    log(f"calib in use: axis={calib.axis} lo={calib.lo} hi={calib.hi}")
    game = Game(scr, touch, calib)
    game.title()
    log("title shown, waiting for tap")
    wait_for_tap(touch)
    log("game start")

    last = time.monotonic()
    fps = 0.0
    try:
        while True:
            now = time.monotonic()
            dt = min(now - last, 0.12)  # 큰 dt로 프레임이 튀면 충돌 판정이 뚫린다
            last = now
            fps = fps * 0.85 + (1.0 / dt) * 0.15 if dt > 0 else fps

            touch.poll()
            game.update(dt)
            if game.over:
                log(f"game over: score={int(game.score)} dodged={game.dodged} "
                    f"level={game.level} fps={fps:.1f}")
                game.render_gameover()
                wait_for_tap(touch)
                game.reset()
                last = time.monotonic()
                continue
            game.render(fps)
    except KeyboardInterrupt:
        scr.clear()
        scr.centre_text(scr.h // 2 - 12, "BYE", 30, DIM, bold=True)
        scr.flush()


if __name__ == "__main__":
    main()
