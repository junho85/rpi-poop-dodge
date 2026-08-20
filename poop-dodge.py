#!/usr/bin/env python3
"""똥피하기 — Waveshare 3.5" SPI LCD(fbtft) + ADS7846 터치로 하는 프레임버퍼 게임.

떨어지는 똥을 좌우로 움직여 피한다. 조작은 두 가지 —
  * 터치. 화면을 누른 x 위치로 캐릭터가 따라온다.
  * 아두이노 버튼 조종기(있으면 자동 인식). 좌/우 버튼을 누르고 있는 동안 움직인다.

실행.
    sudo systemctl stop lcd-status     # 상태 화면과 프레임버퍼를 다투므로 먼저 정지
    sudo python3 poop-dodge.py

설계 메모.
  * fb 번호(fb0/fb1)는 부팅마다 바뀐다 → /sys/class/graphics/fb*/name 으로 찾는다.
  * fbtft 프레임버퍼는 RGB565(16bpp)라 PIL RGB 이미지를 직접 변환해 써넣는다.
  * 터치는 픽셀이 아니라 raw ADC 값(0~4095)으로 온다. 커널이 알려주는 0~4095는
    드라이버 기본값이고 이 패널 실측은 281~3742 다. 실측값을 기본으로 두고,
    그보다 넓은 값이 들어오면 그때그때 넓힌다(패널이 달라도 몇 번 누르면 맞는다).
  * 화면 가로축이 ABS_Y 인 패널이라면 오버레이에 swapxy 를 준다.
"""
from __future__ import annotations

import glob
import math
import os
import random
import struct
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

def log(msg: str) -> None:
    """journalctl -u poop-dodge 로 보이는 진단 로그."""
    print(f"[poop] {msg}", flush=True)


DRIVER = "fb_ili9486"
TOUCH_NAME = "ADS7846"
# 이 패널(Waveshare 3.5" A V3) 물리 가장자리 실측값. 관측값이 더 넓으면 자동으로 넓힌다.
RAW_LO, RAW_HI = 281, 3742
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
    """프레임버퍼에 직접 그리는 화면.

    ⚠️ 전체 프레임을 매번 쓰면 화면이 찢어진다(tearing). fbtft 는 vsync 가 없고
    자체 주기(드라이버 fps)로 dirty 영역을 SPI 로 밀어내는데, 그 전송 중에 앱이
    다음 프레임을 덮어쓰면 위/아래가 서로 다른 프레임이 되어 캐릭터 머리가
    잘린 것처럼 보인다. 그래서 두 가지를 한다.
      1. 이전 프레임과 비교해 **변경된 행 구간만** seek + write (전송량 급감)
      2. 드라이버 fps 이상으로 그리지 않는다 (page_flip 호출 측에서 페이싱)
    """

    def __init__(self) -> None:
        self.dev, self.w, self.h = find_fb()
        self.fb = open(self.dev, "wb", buffering=0)
        self.img = Image.new("RGB", (self.w, self.h), BG)
        self.draw = ImageDraw.Draw(self.img)
        self._fonts: dict[tuple[int, bool, bool], ImageFont.FreeTypeFont] = {}
        self._prev: "np.ndarray | None" = None
        self.pushed_rows = 0   # 마지막 flush 에서 실제로 전송한 행 수 (진단용)

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
        cur = (((a[:, :, 0] >> 3) << 11)
               | ((a[:, :, 1] >> 2) << 5)
               | (a[:, :, 2] >> 3)).astype("<u2")

        if self._prev is None:                      # 첫 프레임은 전체 전송
            self.fb.seek(0)
            self.fb.write(cur.tobytes())
            self.pushed_rows = self.h
        else:
            rows = np.flatnonzero(np.any(cur != self._prev, axis=1))
            if len(rows):
                # 가까운 구간은 합쳐서 보낸다. seek 왕복보다 몇 줄 더 보내는 게 싸다.
                splits = np.flatnonzero(np.diff(rows) > 12) + 1
                pushed = 0
                for part in np.split(rows, splits):
                    y0, y1 = int(part[0]), int(part[-1]) + 1
                    self.fb.seek(y0 * self.w * 2)
                    self.fb.write(cur[y0:y1].tobytes())
                    pushed += y1 - y0
                self.pushed_rows = pushed
            else:
                self.pushed_rows = 0
        self._prev = cur


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
        self.lo, self.hi = RAW_LO, RAW_HI   # 관측되는 대로 넓어진다

    def poll(self) -> None:
        """대기 중인 이벤트를 모두 소비하고 최신 상태만 남긴다.

        ⚠️ 두 가지 함정을 함께 피한다.
        1. 눌림 판정은 BTN_TOUCH 하나만 믿는다. 이벤트가
           BTN_TOUCH=1 → ABS_X → ABS_Y → ABS_PRESSURE → SYN 순으로 오기 때문에,
           pressure 로 판정하면 BTN_TOUCH 로 잡은 눌림이 뒤따르는 값에 덮인다.
           BTN_TOUCH 만 보면 임계값을 고를 일도 없다.
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
                        if value < self.lo:
                            self.lo = value
                        elif value > self.hi:
                            self.hi = value
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


    def screen_x(self, width: int) -> float:
        """raw ADC 값을 화면 x 로 옮긴다. 관측 범위를 쓰므로 별도 보정이 필요 없다."""
        span = self.hi - self.lo
        if span <= 0:
            return width / 2
        return min(max((self.raw_x - self.lo) / span, 0.0), 1.0) * width

    def take_tap(self) -> bool:
        """눌림 시작이 있었으면 True 를 주고 카운터를 비운다."""
        if self.tap_count:
            self.tap_count = 0
            return True
        return False

    def contact(self) -> bool:
        """지금 누르고 있거나, 직전에 톡 눌렀다 뗀 것을 아직 안 읽었다면 True."""
        return self.pressed or self.tap_count > 0


class Controller:
    """아두이노 버튼 조종기. 없으면 없는 대로 굴러간다(터치로 플레이).

    스케치가 버튼을 누를 때 'L'/'R', 뗄 때 'l'/'r' 을 보낸다. 누름·뗌을 둘 다
    보내는 이유는 "누르고 있는 동안 이동"을 하려면 상태가 필요하기 때문이다.
    문자 하나만 보내는 1버튼 조종기('J')도 탭으로 받아준다.
    """

    PORT_GLOBS = ("/dev/ttyACM*", "/dev/ttyUSB*")
    BAUD = 9600

    def __init__(self) -> None:
        self.ser = None
        self.left = False
        self.right = False
        self.tap_count = 0
        try:
            import serial
        except ImportError:
            log("pyserial 없음 → 조종기 비활성 (터치로 플레이)")
            return
        for pattern in self.PORT_GLOBS:
            for path in sorted(glob.glob(pattern)):
                try:
                    self.ser = serial.Serial(path, self.BAUD, timeout=0)
                except OSError as exc:
                    log(f"조종기 {path} 열기 실패: {exc}")
                    continue
                # UNO 는 시리얼 연결 때 오토리셋이 걸려 부트로더가 잠깐 잡는다.
                log(f"조종기 {path} 연결 (부팅 대기 중)")
                time.sleep(1.6)
                self.ser.reset_input_buffer()
                return
        log("조종기 없음 → 터치로 플레이")

    def poll(self) -> None:
        if self.ser is None:
            return
        try:
            data = self.ser.read(64)
        except OSError:
            return
        for ch in data.decode("ascii", "ignore"):
            if ch == "L":
                self.left = True
                self.tap_count += 1
            elif ch == "l":
                self.left = False
            elif ch == "R":
                self.right = True
                self.tap_count += 1
            elif ch == "r":
                self.right = False
            elif ch == "J":          # 1버튼 조종기
                self.tap_count += 1

    def dx(self) -> int:
        """-1 왼쪽 · +1 오른쪽 · 0 정지. 양쪽을 같이 누르면 0."""
        return (1 if self.right else 0) - (1 if self.left else 0)

    def take_tap(self) -> bool:
        if self.tap_count:
            self.tap_count = 0
            return True
        return False


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


def wait_for_tap(touch: Touch, ctrl: "Controller | None" = None) -> None:
    wait_release(touch, "before tap")
    if ctrl is not None:
        ctrl.poll()
        ctrl.take_tap()          # 대기 전에 남은 입력을 버린다
    while True:
        touch.poll()
        if touch.pressed or touch.take_tap():
            return
        if ctrl is not None:
            ctrl.poll()
            if ctrl.take_tap():
                return
        time.sleep(0.01)


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
    # 손가락 추종 속도. 지수 보간이라 프레임레이트가 흔들려도 체감이 같다.
    # 값이 클수록 즉각적. 11 로 두면 "늦게 따라오는" 느낌이 난다.
    FOLLOW_K = 26.0
    # 아무리 멀어도 이 속도 이상으로는 따라온다 (px/s). 먼 거리에서의 답답함 제거.
    FOLLOW_MIN_SPEED = 900.0
    # 조종기 버튼을 누르고 있을 때의 이동 속도 (px/s). 화면 폭을 약 1.1초에 횡단.
    BUTTON_SPEED = 430.0

    def __init__(self, scr: Screen, touch: Touch, ctrl: "Controller | None" = None) -> None:
        self.scr, self.touch, self.ctrl = scr, touch, ctrl
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
        self._fps_shown = 0.0
        self._fps_at = 0.0

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

        # 이동 — 조종기 버튼이 우선, 없으면 터치한 x를 향해 따라간다.
        # 지수 보간(1 - e^-kt)이라 dt 가 흔들려도 추종 체감이 일정하다.
        dx = self.ctrl.dx() if self.ctrl is not None else 0
        if dx:
            self.px += dx * self.BUTTON_SPEED * dt
            # ⚠️ 조종기로 조작하는 동안 남은 터치 latch 를 버린다. 안 버리면 버튼을
            # 뗀 순간 아래 분기가 살아나 캐릭터가 마지막 터치 위치로 끌려간다.
            self.touch.take_tap()
        elif self.touch.contact():
            target = self.touch.screen_x(self.scr.w)
            gap = target - self.px
            step = gap * (1.0 - math.exp(-self.FOLLOW_K * dt))
            floor = self.FOLLOW_MIN_SPEED * dt          # 최소 속도 보장
            if abs(step) < floor:
                step = math.copysign(min(floor, abs(gap)), gap)
            self.px += step
            # 톡 눌렀다 뗀 탭은 목표에 도달하면 소비한다. 안 하면 latch 가 영구히
            # 남아 손을 뗀 뒤에도 contact() 가 계속 True 다.
            if not self.touch.pressed and abs(gap) < 2.0:
                self.touch.take_tap()
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

    def hud_fps(self, fps: float) -> float:
        """표시용 fps. 매 프레임 바꾸면 그 행이 늘 dirty 가 되어 전송량이 는다."""
        if self.elapsed - self._fps_at > 0.5:
            self._fps_shown = fps
            self._fps_at = self.elapsed
        return self._fps_shown

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
        d.text((6, scr.h - 15), f"{self.hud_fps(fps):4.1f} fps",
               font=scr.font(12, mono=True), fill=(78, 82, 94))

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

    ctrl = Controller()
    log(f"touch range: {touch.lo}..{touch.hi} (관측되는 대로 넓어진다)")
    game = Game(scr, touch, ctrl)
    game.title()
    log("title shown, waiting for tap")
    wait_for_tap(touch, ctrl)
    log("game start")

    # fbtft 드라이버가 실제로 화면을 갱신하는 상한(로그의 fps=33)보다 빠르게 그려도
    # 화면에는 반영되지 않고 tearing 만 늘어난다. 그 위에 맞춰 페이싱한다.
    TARGET_FPS = 33.0
    budget = 1.0 / TARGET_FPS

    last = time.monotonic()
    fps = 0.0
    try:
        while True:
            frame_start = time.monotonic()
            dt = min(frame_start - last, 0.12)  # 큰 dt로 프레임이 튀면 충돌 판정이 뚫린다
            last = frame_start
            fps = fps * 0.85 + (1.0 / dt) * 0.15 if dt > 0 else fps

            touch.poll()
            ctrl.poll()
            game.update(dt)
            if game.over:
                log(f"game over: score={int(game.score)} dodged={game.dodged} "
                    f"level={game.level} fps={fps:.1f}")
                game.render_gameover()
                wait_for_tap(touch, ctrl)
                game.reset()
                last = time.monotonic()
                continue
            game.render(fps)

            spare = budget - (time.monotonic() - frame_start)
            if spare > 0:
                time.sleep(spare)
    except KeyboardInterrupt:
        scr.clear()
        scr.centre_text(scr.h // 2 - 12, "BYE", 30, DIM, bold=True)
        scr.flush()


if __name__ == "__main__":
    main()
