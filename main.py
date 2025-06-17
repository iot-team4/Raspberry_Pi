import RPi.GPIO as GPIO
import serial
import time
import threading
import json
import requests
import board
import adafruit_dht

# --- 설정 ---
# GPIO 핀 번호 (BCM 모드 기준)
DHT_PIN = 4
MQ135_PIN = 17
LED_PINS = {'good': 27, 'moderate': 22, 'bad': 23, 'off': -1}
FAN_PIN = 18

# 백엔드 서버 주소
BACKEND_API_URL = "http://127.0.0.1:3000/api/sensors"
# [변경] 최신 제어 명령을 가져올 API 엔드포인트
CONTROL_API_URL = "http://127.0.0.1:3000/api/logs/control/latest"

# --- 전역 변수 ---
# 이전 센서 값 저장용
last_sent_temp = None
last_sent_humid = None
last_sent_pm2_5 = None

# 기능 활성화 상태 관리용 (기본값)
auto_fan_enabled = True
led_enabled = True

# 자동 팬 제어 조건
AUTO_FAN_TEMP_THRESHOLD = 30.0
AUTO_FAN_PM25_THRESHOLD = 75

# --- GPIO 및 센서 관련 함수 (이전과 동일) ---
def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(MQ135_PIN, GPIO.IN)
    for pin in LED_PINS.values():
        if pin != -1:
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.LOW)
    GPIO.setup(FAN_PIN, GPIO.OUT)
    GPIO.output(FAN_PIN, GPIO.LOW)
    print("✅ GPIO가 성공적으로 설정되었습니다.")

try:
    dht_device = adafruit_dht.DHT11(board.D4)
    ser = serial.Serial('/dev/serial0', baudrate=9600, timeout=2)
except Exception as e:
    print(f"🚨 센서 초기화 중 오류 발생: {e}")
    dht_device = None
    ser = None

def read_dht11():
    if not dht_device: return None, None
    try:
        return float(dht_device.temperature), float(dht_device.humidity)
    except Exception: return None, None

def read_pms7003():
    if not ser: return None
    try:
        if ser.in_waiting >= 32:
            data = ser.read(32)
            if data[0] == 0x42 and data[1] == 0x4d:
                return int.from_bytes(data[12:14], byteorder='big')
    except Exception: return None
    return None

def read_mq135(): return GPIO.input(MQ135_PIN)

def send_to_backend(sensor_type, value):
    payload = {"sensorType": sensor_type, "value": str(value)}
    try:
        response = requests.post(BACKEND_API_URL, json=payload, timeout=5)
        if response.status_code == 201:
            print(f"✅ [{sensor_type}] 데이터 전송 성공: {value}")
        else:
            print(f"🚨 [{sensor_type}] 데이터 전송 실패: {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"🚨 [{sensor_type}] 백엔드 연결 오류: {e}")

def set_led(status):
    if status == 'off':
        for pin in LED_PINS.values():
            if pin != -1: GPIO.output(pin, GPIO.LOW)
    else:
        for name, pin in LED_PINS.items():
            if pin != -1: GPIO.output(pin, GPIO.HIGH if name == status else GPIO.LOW)
    print(f"💡 LED 상태 변경: {status}")

def control_fan(state):
    GPIO.output(FAN_PIN, GPIO.HIGH if state else GPIO.LOW)
    print(f"💨 팬 상태 변경: {'ON' if state else 'OFF'}")

# --- [신규] 제어 명령 폴링 및 상태 업데이트 함수 ---
def apply_latest_commands():
    """서버에서 최신 제어 명령을 가져와 전역 상태 변수를 업데이트합니다."""
    global auto_fan_enabled, led_enabled
    
    try:
        response = requests.get(CONTROL_API_URL, timeout=5)
        if response.status_code != 200:
            print(f"🚨 제어 명령 조회 실패: {response.status_code}")
            return

        commands = response.json()
        if not commands:
            print("ℹ️ 수신된 새 제어 명령이 없습니다.")
            return

        # 최신 명령을 기준으로 상태 결정 (목록의 첫번째 항목이 가장 최신이라고 가정)
        latest_command = commands[0]
        target = latest_command.get("target")
        action = latest_command.get("action")
        
        print(f"📥 최신 제어 명령 수신: {latest_command}")

        if target == "led":
            led_enabled = (action == "on")
        elif target == "autoFan":
            auto_fan_enabled = (action == "enable")
        elif target == "fan" and latest_command.get("source") == "user":
            auto_fan_enabled = False # 수동 팬 조작 시 자동 모드 비활성화
            control_fan(action == "on") # 수동 팬 명령은 즉시 실행
            print("ℹ️ 수동 팬 제어 명령을 즉시 실행합니다.")

    except requests.exceptions.RequestException as e:
        print(f"🚨 제어 명령 조회 중 네트워크 오류: {e}")
    except Exception as e:
        print(f"🚨 제어 명령 처리 중 오류: {e}")

# --- 메인 루프 ---
def main_loop():
    """주기적으로 제어명령과 센서 값을 처리하는 메인 루프입니다."""
    global last_sent_temp, last_sent_humid, last_sent_pm2_5

    while True:
        # 1. 서버로부터 최신 제어 명령을 가져와 상태 업데이트
        apply_latest_commands()

        # 2. 센서 값 읽기
        temp, humid = read_dht11()
        pm2_5 = read_pms7003()
        air_quality_digital = read_mq135()

        # 3. 변경된 센서 값을 서버로 전송
        if temp is not None and last_sent_temp != temp:
            send_to_backend("temperature", temp)
            last_sent_temp = temp
        if humid is not None and last_sent_humid != humid:
            send_to_backend("humidity", humid)
            last_sent_humid = humid
        if pm2_5 is not None and last_sent_pm2_5 != pm2_5:
            send_to_backend("pm25", pm2_5)
            last_sent_pm2_5 = pm2_5

        # 4. 상태 변수에 따른 자동 제어 로직 실행
        # (수동 팬 제어는 apply_latest_commands에서 이미 처리됨)
        if auto_fan_enabled:
            is_hot = temp is not None and temp >= AUTO_FAN_TEMP_THRESHOLD
            is_dusty = pm2_5 is not None and pm2_5 >= AUTO_FAN_PM25_THRESHOLD
            control_fan(is_hot or is_dusty)

        if led_enabled:
            if pm2_5 is None: set_led('off')
            elif air_quality_digital == 0 and pm2_5 < 35: set_led('good')
            elif air_quality_digital == 0 or pm2_5 < 75: set_led('moderate')
            else: set_led('bad')
        else:
            set_led('off')

        print(f"🌡️ 현재 상태: 온도={temp}°C, 습도={humid}%, PM2.5={pm2_5}µg/m³, 자동팬={auto_fan_enabled}, LED={led_enabled}")
        
        # 5. 다음 루프까지 5초 대기
        time.sleep(5)

# --- 메인 실행 ---
if __name__ == "__main__":
    try:
        setup_gpio()
        main_loop() # 메인 루프 직접 실행
    except KeyboardInterrupt:
        print("\n🚫 프로그램을 종료합니다.")
    finally:
        GPIO.cleanup()
