/*
 * controller.ino — 라즈베리파이 게임용 버튼 조종기
 *
 * 버튼을 누르면 시리얼로 문자 하나를 보낸다. 라즈베리파이 쪽 게임은
 * `ser.read()` 로 그 한 글자를 받아 점프에 쓴다.
 *
 * 배선 (버튼 하나, 저항 없이)
 *   버튼 한쪽 → D2
 *   버튼 다른쪽 → GND
 *   ※ INPUT_PULLUP 을 쓰므로 풀업 저항을 따로 달지 않는다.
 *      누르지 않으면 HIGH, 누르면 LOW 다.
 *
 * 라즈베리파이와 연결
 *   아두이노 USB 케이블을 Pi 에 꽂으면 /dev/ttyACM0 으로 잡힌다(UNO 는 CDC ACM).
 *   보드레이트는 게임과 반드시 같아야 한다 — 9600.
 *
 * 확인 (Pi 에서)
 *   sudo apt install -y python3-serial
 *   python3 -c "import serial; s=serial.Serial('/dev/ttyACM0',9600,timeout=5); print(s.read(4))"
 *   → 버튼을 네 번 누르면 b'JJJJ'
 */

const int  BUTTON_PIN  = 2;
const char JUMP_CHAR   = 'J';   // 게임이 기다리는 문자
const unsigned long DEBOUNCE_MS = 25;

// 버튼 접점은 누르는 순간 수 ms 동안 값이 튄다(bouncing).
// 그대로 읽으면 한 번 눌러도 여러 번 눌린 것으로 잡히므로,
// 값이 DEBOUNCE_MS 동안 유지될 때만 상태 변화로 인정한다.
int lastReading = HIGH;         // 방금 읽은 값
int stableState = HIGH;         // 흔들림이 가라앉은 값
unsigned long lastChangeMs = 0;

void setup() {
  Serial.begin(9600);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  pinMode(LED_BUILTIN, OUTPUT);  // 눌릴 때 보드 LED 로 눈으로 확인
}

void loop() {
  int reading = digitalRead(BUTTON_PIN);

  if (reading != lastReading) {          // 흔들리기 시작한 시점을 기록
    lastReading = reading;
    lastChangeMs = millis();
  }

  if (millis() - lastChangeMs >= DEBOUNCE_MS && reading != stableState) {
    stableState = reading;

    // LOW = 눌림. **누르는 순간에만** 보낸다.
    // 누르고 있는 동안 계속 보내면 게임에서 연타로 처리된다.
    if (stableState == LOW) {
      Serial.write(JUMP_CHAR);
      digitalWrite(LED_BUILTIN, HIGH);
    } else {
      digitalWrite(LED_BUILTIN, LOW);
    }
  }
}
