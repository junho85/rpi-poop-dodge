#!/usr/bin/env python3
"""SPI LCD 프레임버퍼에 "안녕 라즈베리파이"를 띄우는 최소 예제.

이 파일 하나로 세 가지를 확인한다.
  1. 프레임버퍼를 번호가 아니라 드라이버 이름으로 찾기 (fb0/fb1 은 부팅마다 바뀐다)
  2. PIL RGB 이미지를 RGB565(16bpp)로 변환해 써넣기
  3. 나눔 폰트로 한글 출력 (DejaVu 에는 한글 글리프가 없어 □ 로 나온다)

실행.
    sudo python3 hello-fb.py
    sudo python3 hello-fb.py "아무 문장이나"

프레임버퍼가 root:video 소유라 sudo 가 필요하다.
(또는 `sudo usermod -aG video $USER` 후 재로그인)
"""
import glob
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

DRIVER = "fb_ili9486"          # 다른 패널이면 여기만 바꾼다 (dmesg 로 확인)
FONT_KO = "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"
FONT_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def find_fb(driver: str = DRIVER) -> tuple[str, int, int]:
    """드라이버 이름으로 프레임버퍼를 찾는다.

    ⚠️ /dev/fb1 로 하드코딩하면 안 된다. vc4 KMS 와 SPI 드라이버의 프로브 순서
    경합 때문에 LCD 가 fb0 으로 잡히는 부팅이 있고, 그때 조용히 실패한다.
    """
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
        "  - /boot/firmware/config.txt 에 dtoverlay=piscreen 이 있는지\n"
        "  - dmesg | grep -i ili9486 에 frame buffer 등록 로그가 있는지 확인하라."
    )


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in (FONT_KO, FONT_FALLBACK):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def to_rgb565(img: Image.Image) -> bytes:
    """PIL RGB → RGB565 little endian. fbtft 프레임버퍼는 16bpp 다."""
    a = np.asarray(img, dtype=np.uint16)
    rgb565 = ((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5) | (a[:, :, 2] >> 3)
    return rgb565.astype("<u2").tobytes()


def main() -> None:
    text = sys.argv[1] if len(sys.argv) > 1 else "안녕 라즈베리파이"
    dev, w, h = find_fb()
    print(f"framebuffer: {dev}  {w}x{h}")

    img = Image.new("RGB", (w, h), (18, 18, 24))
    d = ImageDraw.Draw(img)
    d.text((24, 30), text, font=load_font(30), fill=(255, 205, 70))
    d.text((24, h - 40), f"{dev}  {w}x{h}  RGB565",
           font=load_font(15), fill=(120, 125, 138))

    with open(dev, "wb") as fb:
        fb.write(to_rgb565(img))
    print("done")


if __name__ == "__main__":
    main()
