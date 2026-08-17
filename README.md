# 똥피하기 (poop-dodge)

라즈베리파이 + **3.5인치 SPI LCD**에서 돌아가는 터치 게임입니다. 떨어지는 똥을 터치로 좌우 이동해 피합니다.

X도, 데스크톱도, 게임 라이브러리도 쓰지 않습니다. **프레임버퍼(`/dev/fbN`)에 직접 그리고, `/dev/input/eventN`을 직접 읽습니다.**

<!-- 스크린샷 자리 -->

## 검증 환경

| 항목 | 값 |
|---|---|
| 보드 | Raspberry Pi 3 Model B Rev 1.2 (1GB) |
| 디스플레이 | Waveshare(SpotPear) 3.5inch RPi LCD (A) V3 — ILI9486, 480×320 |
| 터치 | ADS7846 호환 저항막 |
| OS | Raspbian GNU/Linux 13 (trixie), 커널 6.18, **armhf(32비트) userland** |
| 오버레이 | `dtoverlay=piscreen` |
| 성능 | 약 40 fps |

ILI9486 + ADS7846 조합의 다른 SPI LCD에서도 대체로 동작할 것입니다. 자세한 설정 과정과 함정은 블로그 글에 정리했습니다 — [라즈베리파이에 3.5인치 SPI LCD 붙이기](https://junho85.github.io/posts/raspberry-pi-spi-lcd-waveshare/)

## 준비

LCD가 이미 동작하는 상태여야 합니다. `/boot/firmware/config.txt`에 두 줄이 필요합니다.

```
dtparam=spi=on
dtoverlay=piscreen
```

재부팅 후 프레임버퍼와 터치가 잡혔는지 확인합니다.

```bash
grep -l fb_ili9486 /sys/class/graphics/fb*/name   # 예: /sys/class/graphics/fb0/name
cat /proc/bus/input/devices | grep -A1 ADS7846
```

의존성은 Raspberry Pi OS desktop 이미지에 이미 들어있습니다. Lite에서는 이렇게 설치합니다.

```bash
sudo apt install -y python3-pil python3-numpy fonts-nanum
```

## 실행

```bash
git clone https://github.com/junho85/rpi-poop-dodge.git
cd rpi-poop-dodge
sudo python3 poop-dodge.py
```

프레임버퍼가 `root:video` 소유라 `sudo`가 필요합니다. (또는 사용자를 `video` 그룹에 넣으세요.)

첫 실행에서 **터치 보정**을 합니다. 화면 지시대로 왼쪽 칸과 오른쪽 칸을 한 번씩 누르면 됩니다. 결과는 `~/.poop-dodge-calib.json`에 저장돼 다음 실행부터 건너뜁니다.

```bash
sudo python3 poop-dodge.py --recalibrate    # 방향이 반대면 다시 보정
```

⚠️ LCD에 다른 걸 띄우고 있으면(콘솔이나 상태 표시 스크립트) 프레임버퍼를 다투므로 먼저 멈춰야 합니다.

```bash
sudo systemctl stop lcd-status
```

## 조작

- **화면을 누른 x 위치로 캐릭터가 따라옵니다.** 버튼 없이 터치 하나로만 조작합니다
- 목숨 3개. 똥에 맞으면 화면이 붉게 번쩍입니다
- 점수 = 생존 시간 + 피한 똥 10점씩
- 난이도는 **경과 시간**으로만 올라갑니다 (12초마다 레벨 업). 점수로 올리면 잘하는 사람만 어려워지니까요
- 게임오버 화면에서 누르면 재시작, `Ctrl+C`로 종료

## 같이 들어있는 도구

### `hello-fb.py` — 프레임버퍼 최소 예제

세팅이 끝났는지 확인하는 가장 빠른 방법입니다. LCD에 한글 한 줄을 띄웁니다.

```bash
sudo python3 hello-fb.py
# framebuffer: /dev/fb0  480x320

sudo python3 hello-fb.py "아무 문장이나"
```

프레임버퍼 탐색 → RGB565 변환 → 나눔 폰트까지 세 가지를 한 파일로 보여줍니다.

### `pygame-on-lcd.py` — pygame 게임을 LCD에서 돌리는 런처

**게임 코드를 한 줄도 고치지 않고** pygame 게임을 SPI LCD에서 실행합니다.

```bash
sudo python3 pygame-on-lcd.py your-game.py
sudo python3 pygame-on-lcd.py your-game.py --key J     # 터치를 보낼 문자 (기본 J)
sudo python3 pygame-on-lcd.py your-game.py --no-touch  # 터치 주입 끄기
```

SDL2 에는 fbdev 백엔드가 없어서 pygame 이 `/dev/fbN` 에 직접 못 그립니다. 그래서
`SDL_VIDEODRIVER=dummy` 로 창 없이 띄우고, `display.flip()` 을 후크해 완성된 Surface 를
RGB565 로 변환해 프레임버퍼에 복사합니다. 게임 화면이 LCD 와 크기가 다르면 자동 스케일합니다.

덤으로 세 가지를 더 처리합니다.

| 가로채는 것 | 처리 |
|---|---|
| `serial.Serial('COM4')` | 윈도우 포트명을 `/dev/ttyACM*` 로 자동 교체. 장치가 없으면 터치만으로 플레이 |
| `pygame.image.load()` | 없는 리소스는 자리표시 Surface 로 대체 (그림이 없어도 일단 돌아간다) |
| LCD 터치 | 탭을 시리얼 문자로 주입 → **아두이노 버튼과 동일한 입력**. 실제 시리얼과 병행 |

윈도우에서 만든 pygame 게임을 라즈베리파이 LCD 로 옮길 때 그대로 쓸 수 있습니다.

### `lcd-status.py` — 상태 표시 화면

호스트명·IP·CPU 온도·클록·스로틀링·부하·메모리·디스크·업타임을 LCD에 띄웁니다.

```bash
sudo python3 lcd-status.py           # 1회 그리기
sudo python3 lcd-status.py --loop    # 5초마다 갱신
```

부팅할 때 자동으로 띄우려면 systemd 서비스로 등록합니다.

```ini
# /etc/systemd/system/lcd-status.service
[Unit]
Description=LCD status display
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/YOUR_USER/lcd-status.py --loop
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now lcd-status
```

### `touch-dump.py` — 터치 이벤트 진단

터치가 안 잡힐 때 **추측하지 않고 확인하는** 도구입니다. LCD에 `TOUCH NOW`를 띄우고 지정 시간 동안 raw 이벤트를 배치 단위로 출력합니다.

```bash
sudo python3 touch-dump.py 25        # 25초간 기록
```

한 번의 `read()`에 어떤 이벤트가 함께 들어오는지가 중요해서 배치로 묶어 찍습니다.

```
batch#001 (5) BTN_TOUCH=1 ABS_X=2100 ABS_Y=1850 ABS_PRESSURE=0 SYN_REPORT=0
```

## 구현에서 신경 쓴 것

이 세 가지가 이 코드의 핵심이고, 전부 실제로 한 번씩 틀려본 뒤에 얻은 것입니다.

**1. 프레임버퍼 번호를 하드코딩하지 않습니다**

`/dev/fb1`이라고 박으면 **재부팅 후 조용히 실패**합니다. vc4 KMS와 SPI 드라이버의 프로브 순서 경합 때문에 LCD가 `fb0`으로 잡히는 부팅이 있습니다.

```python
for path in sorted(glob.glob("/sys/class/graphics/fb*")):
    if open(f"{path}/name").read().strip() == "fb_ili9486":
        dev = "/dev/" + os.path.basename(path)
        w, h = open(f"{path}/virtual_size").read().strip().split(",")
```

**2. 터치 눌림은 `BTN_TOUCH`만 신뢰하고, 짧은 탭은 latch 합니다**

`xohms` 설정이 없으면 ADS7846은 `ABS_PRESSURE`를 항상 `0`으로 보고합니다. 이벤트가 `BTN_TOUCH=1 → ABS_X → ABS_Y → ABS_PRESSURE=0 → SYN` 순으로 오기 때문에, 압력으로 판정하면 누른 즉시 "뗌"으로 덮입니다.

그리고 톡 누르면 `BTN_TOUCH=1`과 `=0`이 **한 번의 `read()`에 함께** 들어옵니다. 배치를 다 처리하면 상태는 항상 "뗀 상태"라, `pressed`만 보는 코드는 아무리 눌러도 못 봅니다. 눌림 시작을 카운터로 latch해서 소비합니다.

```python
if self.pressed and not was:
    self.tap_count += 1

def take_tap(self):
    if self.tap_count:
        self.tap_count = 0
        return True
    return False
```

**3. `struct input_event` 크기는 userland 비트수를 따릅니다**

`struct timeval`이 long 2개라서 **32비트는 16바이트, 64비트는 24바이트**입니다. `"qqHHi"`(고정 8바이트)로 박으면 32비트 armhf에서 프레임이 어긋나 쓰레기 값이 나옵니다.

```python
EVENT_FMT = "llHHi"    # native long → 양쪽에서 자동으로 맞는다
```

참고로 **커널이 arm64(`+rpt-rpi-v8`)여도 userland는 32비트일 수 있습니다.** Raspberry Pi OS 32-bit는 Pi 3 이상에서 64비트 커널을 로드합니다. 아키텍처는 `uname -m`이 아니라 `dpkg --print-architecture`로 봐야 합니다.

## 그 외 메모

- 프레임버퍼는 **RGB565(16bpp)** 라 PIL의 RGB 이미지를 직접 변환해야 합니다
  ```python
  a = np.asarray(img, dtype=np.uint16)
  rgb565 = ((a[:,:,0] >> 3) << 11) | ((a[:,:,1] >> 2) << 5) | (a[:,:,2] >> 3)
  fb.write(rgb565.astype("<u2").tobytes())
  ```
- 한글은 DejaVu에 글리프가 없어 네모로 나옵니다. `fonts-nanum`을 쓰고, 숫자가 실시간으로 바뀌는 HUD에는 고정폭 `NanumGothicCoding`을 씁니다
- SPI 24MHz로 307KB 프레임을 초당 40번 보내는 건 대역폭상 불가능한데도 40fps가 나옵니다. **fbtft가 변경된 영역만 전송**하고 `write()`는 커널 버퍼 복사로 끝나기 때문입니다. 실제 화면 갱신은 드라이버의 `fps=33`이 상한입니다
- `dt`를 0.12초로 상한을 뒀습니다. 프레임이 튈 때 똥이 캐릭터를 통과해버리는 걸 막습니다

## 화면 방향·터치 좌표가 안 맞을 때

오버레이 파라미터로 조정합니다.

```
dtoverlay=piscreen,rotate=90            # 화면 방향 (0/90/180/270, 기본 270)
dtoverlay=piscreen,invx,invy,swapxy     # 터치 좌표 반전·교환
dtoverlay=piscreen,speed=16000000       # 화면이 깨지면 SPI 속도를 낮춘다
```

## 라이선스

MIT
