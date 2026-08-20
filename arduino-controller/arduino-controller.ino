/*
 * controller.ino — 라즈베리파이 게임용 버튼 조종기
 *
 * 버튼을 읽어 USB 시리얼로 문자 하나를 보낸다. 게임 쪽은 그 문자를 읽는다.
 *
 *   [왼쪽 버튼] D2 ─┐
 *   [오른쪽 버튼] D3 ─┴→ 아두이노 → USB 시리얼 9600bps → 라즈베리파이
 *
 * 배선은 버튼마다 두 가닥뿐이다. 한쪽 다리를 D2(또는 D3), 다른쪽 다리를 GND 로.
 * INPUT_PULLUP 을 쓰므로 저항이 필요 없다 — 평소 HIGH, 누르면 LOW.
 *
 * 프로토콜. 누를 때와 뗄 때를 각각 보낸다.
 *
 *   왼쪽  누름 'L'   뗌 'l'
 *   오른쪽 누름 'R'   뗌 'r'
 *
 * 왜 뗌도 보내는가 — "누르고 있는 동안 이동"을 하려면 게임이 상태를 알아야 한다.
 * 누름만 보내면 게임은 언제 멈춰야 할지 알 수 없다.
 *
 * 문자 하나만 받는 게임(예: 점프 한 종류)에 붙이려면 release 를 0 으로 두고
 * press 를 그 문자로 바꾼다. 0 이면 뗄 때 아무것도 보내지 않는다.
 */

struct Button {
  const uint8_t pin;
  const char press;             // 누를 때 보낼 문자
  const char release;           // 뗄 때 보낼 문자 (0 = 보내지 않음)
  int lastReading;              // 직전에 읽은 원값 (디바운스용)
  int stableState;              // 확정된 상태
  unsigned long lastChangeMs;   // 원값이 마지막으로 바뀐 시각
};

Button buttons[] = {
  {2, 'L', 'l', HIGH, HIGH, 0},
  {3, 'R', 'r', HIGH, HIGH, 0},
};

const size_t BUTTON_COUNT = sizeof(buttons) / sizeof(buttons[0]);

// 기계식 스위치는 접점이 붙는 순간 수 ms 동안 떨린다. 이 시간만큼 값이
// 유지돼야 상태 변화로 인정한다. 너무 크게 잡으면 연타가 씹힌다.
const unsigned long DEBOUNCE_MS = 25;

void setup() {
  Serial.begin(9600);
  for (size_t i = 0; i < BUTTON_COUNT; i++) {
    pinMode(buttons[i].pin, INPUT_PULLUP);
  }
  pinMode(LED_BUILTIN, OUTPUT);

  // 부팅 배너. 꽂자마자 이게 오면 업로드·통신·보드레이트가 전부 확인된다.
  // 남는 변수는 배선 하나뿐이라 진단이 훨씬 빨라진다.
  // ⚠️ 배너에는 프로토콜 문자(L l R r J)를 쓰지 않는다 — 게임이 입력으로 오해한다.
  Serial.write("# btn2 ok\n");
}

void loop() {
  bool anyPressed = false;

  for (size_t i = 0; i < BUTTON_COUNT; i++) {
    Button &b = buttons[i];
    const int reading = digitalRead(b.pin);

    if (reading != b.lastReading) {          // 떨림 시작 — 타이머만 리셋
      b.lastReading = reading;
      b.lastChangeMs = millis();
    }

    // 값이 DEBOUNCE_MS 동안 유지됐고, 확정 상태와 다르면 그때 한 번만 보낸다.
    if (millis() - b.lastChangeMs >= DEBOUNCE_MS && reading != b.stableState) {
      b.stableState = reading;
      if (b.stableState == LOW) {            // 눌림 (풀업이라 LOW 가 눌린 상태)
        Serial.write(b.press);
      } else if (b.release) {                // 뗌
        Serial.write(b.release);
      }
    }

    if (b.stableState == LOW) {
      anyPressed = true;
    }
  }

  // 하나라도 눌려 있으면 보드 LED 를 켠다. 배선이 맞는지 눈으로 바로 확인된다.
  digitalWrite(LED_BUILTIN, anyPressed ? HIGH : LOW);
}
