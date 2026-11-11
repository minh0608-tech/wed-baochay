"""
Web dashboard báo cháy ESP32 - BẢN CHẠY TRÊN INTERNET (Render, Railway...).

ESP32 gửi HTTPS POST JSON tới /api/data, kèm header X-API-Key.
Khóa nằm trong biến môi trường API_KEY của server (đặt trên trang Render).

Lệnh khởi động trên Render:
    gunicorn --workers 1 --threads 4 --bind 0.0.0.0:$PORT app:app
(Phải dùng đúng 1 worker, vì dữ liệu được giữ trong bộ nhớ của tiến trình.)

Biến môi trường (đặt ở mục Environment trên Render):
    API_KEY            khóa cho ESP32 gửi dữ liệu (bắt buộc khi chạy thật)
    TELEGRAM_TOKEN     token bot Telegram (tùy chọn, để nhận thông báo về điện thoại)
    TELEGRAM_CHAT_ID   mã chat nhận thông báo (tùy chọn)
    NTFY_TOPIC         tên chủ đề ntfy để điện thoại kêu chuông khẩn cấp (tùy chọn)
    NTFY_SERVER        máy chủ ntfy, mặc định https://ntfy.sh (tùy chọn)
    PROJECT_TITLE      tên đề tài hiện trên đầu trang (tùy chọn)
    TEAM_NAME          tên nhóm / tác giả hiện dưới chân trang (tùy chọn)

Thử trên máy tính:
    python app.py --demo
"""

import argparse
import hmac
import json
import math
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

from flask import Flask, jsonify, render_template, request

# Ngưỡng giống firmware (chỉ để hiển thị vạch ngưỡng trên biểu đồ)
TEMP_ON, TEMP_OFF = 40.0, 37.0
GAS_ON, GAS_OFF = 1200, 1000

app = Flask(__name__)

# Khóa bảo vệ API nhận dữ liệu. Để trống = ai cũng gửi được (chỉ dùng khi thử trên máy).
API_KEY = os.environ.get("API_KEY", "")
VALID_STATES = {"BINH THUONG", "CANH BAO: NHIET", "CANH BAO: GAS", "BAO CHAY!"}

# Thông báo Telegram (để trống = tắt)
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TG_ENABLED = bool(TG_TOKEN and TG_CHAT)

# Thông báo ntfy (app miễn phí, có mức ưu tiên khẩn cấp). Để trống = tắt
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")
NTFY_ENABLED = bool(NTFY_TOPIC)

PROJECT_TITLE = os.environ.get("PROJECT_TITLE", "Hệ thống giám sát báo cháy ESP32")
TEAM_NAME = os.environ.get("TEAM_NAME", "")

OFFLINE_AFTER = 60  # quá 60 giây không có dữ liệu thì coi là mất kết nối

lock = threading.Lock()
history = deque(maxlen=900)  # ~30 phút nếu 2 giây một mẫu
latest = None

# Lệnh bật còi từ web. ESP32 nhận lệnh qua phản hồi của POST /api/data.
# Tự tắt sau REMOTE_BUZZER_MAX giây để còi không kêu mãi nếu quên tắt.
REMOTE_BUZZER_MAX = 60
BOARD_LIVE_WITHIN = 15  # mạch gửi dữ liệu trong 15 giây gần nhất thì coi là đang online
remote = {"buzzer": False, "until": 0.0}
_last_notify = 0.0
events = deque(maxlen=50)  # nhật ký thay đổi trạng thái
stats = {
    "since": time.time(),
    "samples": 0,
    "max_temp": None,
    "min_temp": None,
    "max_gas": None,
    "alarm_count": 0,  # số lần chuyển từ bình thường sang cảnh báo/báo cháy
    "fire_count": 0,  # số lần báo cháy (cả nhiệt và gas)
}

LINE_RE = re.compile(
    r"T=(?P<t>[-\d.]+|nan|-nan)\s*C;\s*"
    r"GAS_ADC=(?P<gas>\d+);\s*"
    r"CO_NHIET=(?P<tf>[01]);\s*"
    r"CO_GAS=(?P<gf>[01]);\s*"
    r"STATE=(?P<state>.+)"
)


def classify(temp_alarm: bool, gas_alarm: bool) -> str:
    if temp_alarm and gas_alarm:
        return "BAO CHAY!"
    if temp_alarm:
        return "CANH BAO: NHIET"
    if gas_alarm:
        return "CANH BAO: GAS"
    return "BINH THUONG"


def parse_line(line: str):
    """Trả về dict hoặc None nếu dòng không phải dữ liệu."""
    m = LINE_RE.search(line)
    if not m:
        return None
    try:
        temp = float(m["t"])
        if math.isnan(temp):
            temp = None
    except ValueError:
        temp = None
    return {
        "temp": temp,
        "gas": int(m["gas"]),
        "temp_alarm": m["tf"] == "1",
        "gas_alarm": m["gf"] == "1",
        "state": m["state"].strip(),
    }


STATE_TEXT = {
    "BAO CHAY!": "🚨 BÁO CHÁY! Nhiệt độ và gas cùng vượt ngưỡng.",
    "CANH BAO: NHIET": "⚠️ Cảnh báo: nhiệt độ cao.",
    "CANH BAO: GAS": "⚠️ Cảnh báo: phát hiện gas/khói.",
    "BINH THUONG": "✅ Đã trở lại bình thường.",
}


def send_telegram(text: str):
    """Gửi tin Telegram ở luồng riêng, lỗi mạng không làm chậm việc nhận dữ liệu."""
    if not TG_ENABLED:
        return

    def run():
        try:
            data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data
            )
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:  # không in URL để khỏi lộ token
            print("[telegram] gửi lỗi:", type(e).__name__, e)

    threading.Thread(target=run, daemon=True).start()


# Tiêu đề, mức ưu tiên (1-5) và biểu tượng cho ntfy. 5 = khẩn cấp, kêu to
NTFY_INFO = {
    "BAO CHAY!": ("BÁO CHÁY!", 5, ["rotating_light", "fire"]),
    "CANH BAO: NHIET": ("Cảnh báo nhiệt độ cao", 4, ["warning", "thermometer"]),
    "CANH BAO: GAS": ("Cảnh báo gas/khói", 4, ["warning", "dash"]),
    "BINH THUONG": ("Đã trở lại bình thường", 3, ["white_check_mark"]),
}


def send_ntfy(title: str, text: str, priority: int = 3, tags=None):
    """Gửi thông báo ntfy ở luồng riêng. Dùng JSON để gửi được tiếng Việt có dấu."""
    if not NTFY_ENABLED:
        return

    def run():
        try:
            payload = json.dumps(
                {
                    "topic": NTFY_TOPIC,
                    "title": title,
                    "message": text,
                    "priority": priority,
                    "tags": tags or [],
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                NTFY_SERVER,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print("[ntfy] gửi lỗi:", type(e).__name__, e)

    threading.Thread(target=run, daemon=True).start()


def notify_all(text: str, title: str, priority: int = 3, tags=None):
    send_telegram(text)
    send_ntfy(title, text, priority, tags)


def describe(sample: dict) -> str:
    t = "--" if sample["temp"] is None else f"{sample['temp']:.1f}°C"
    return f"{STATE_TEXT.get(sample['state'], sample['state'])}\nNhiệt độ: {t} | Gas (ADC): {sample['gas']}"


def push(sample: dict, source: str):
    global latest
    now = time.time()
    sample = dict(sample)
    sample["ts"] = now
    sample["source"] = source
    sample["buzzer"] = sample["temp_alarm"] or sample["gas_alarm"]
    notify = None
    with lock:
        prev = latest["state"] if latest else None
        latest = sample
        history.append(sample)

        # Thống kê
        stats["samples"] += 1
        t = sample["temp"]
        if t is not None:
            if stats["max_temp"] is None or t > stats["max_temp"]:
                stats["max_temp"] = t
            if stats["min_temp"] is None or t < stats["min_temp"]:
                stats["min_temp"] = t
        if stats["max_gas"] is None or sample["gas"] > stats["max_gas"]:
            stats["max_gas"] = sample["gas"]

        if prev != sample["state"]:
            events.appendleft({"ts": now, "state": sample["state"]})
            if sample["state"] != "BINH THUONG" and prev in (None, "BINH THUONG"):
                stats["alarm_count"] += 1
            if sample["state"] == "BAO CHAY!":
                stats["fire_count"] += 1
            # Lần đầu nhận mẫu mà đã bình thường thì không cần báo
            if not (prev is None and sample["state"] == "BINH THUONG"):
                notify = sample
    if notify:
        title, prio, tags = NTFY_INFO.get(
            notify["state"], (notify["state"], 3, [])
        )
        notify_all(describe(notify), title, prio, tags)


def remote_buzzer_active() -> bool:
    """Lệnh bật còi từ web còn hiệu lực không (tự hết hạn sau REMOTE_BUZZER_MAX giây)."""
    with lock:
        if remote["buzzer"] and time.time() > remote["until"]:
            remote["buzzer"] = False
        return remote["buzzer"]


def stats_snapshot():
    with lock:
        s = dict(stats)
    s["uptime"] = time.time() - s["since"]
    s["telegram"] = TG_ENABLED
    s["ntfy"] = NTFY_ENABLED
    return s


_offline_notified = False


def watcher():
    """Báo về điện thoại khi mạch ngừng gửi dữ liệu quá lâu, và khi nối lại."""
    global _offline_notified
    while True:
        time.sleep(10)
        with lock:
            ts = latest["ts"] if latest else None
        if ts is None:
            continue
        age = time.time() - ts
        if age > OFFLINE_AFTER and not _offline_notified:
            _offline_notified = True
            notify_all("📡 Mất kết nối: mạch ESP32 không gửi dữ liệu hơn 1 phút.", "Mất kết nối", 4, ["satellite"])
        elif age <= OFFLINE_AFTER and _offline_notified:
            _offline_notified = False
            notify_all("📡 Mạch ESP32 đã kết nối lại.", "Đã kết nối lại", 3, ["satellite"])


if TG_ENABLED or NTFY_ENABLED:
    threading.Thread(target=watcher, daemon=True).start()
    notify_all(
        "🔔 Hệ thống giám sát báo cháy đã khởi động. Thông báo điện thoại hoạt động.",
        "Hệ thống đã khởi động",
        3,
        ["bell"],
    )


# ---------------- Nguồn dữ liệu ----------------
def serial_worker(port: str, baud: int):
    import serial  # import muộn để --no-serial không cần pyserial

    while True:
        try:
            ser = serial.Serial()
            ser.port = port
            ser.baudrate = baud
            ser.timeout = 2
            # Tắt DTR/RTS trước khi mở: nếu không, ESP32 bị giữ ở trạng thái reset
            ser.dtr = False
            ser.rts = False
            ser.open()
            with ser:
                print(f"[serial] đã mở {port} @ {baud}")
                shown = 0
                while True:
                    raw = ser.readline().decode("utf-8", errors="ignore").strip()
                    if not raw:
                        continue
                    sample = parse_line(raw)
                    if sample:
                        push(sample, "serial")
                    elif shown < 10:
                        # In vài dòng không đọc được để dễ kiểm tra khi có lỗi
                        print(f"[serial] bỏ qua dòng: {raw}")
                        shown += 1
        except Exception as e:  # rút cáp, sai cổng, ...
            print(f"[serial] lỗi: {e} - thử lại sau 3 giây")
            time.sleep(3)


def demo_worker():
    """Dữ liệu giả: chu kỳ ~60 giây đi qua đủ 4 trạng thái."""
    t0 = time.time()
    temp_alarm = gas_alarm = False
    while True:
        phase = (time.time() - t0) % 60
        temp = 28 + random.uniform(-0.3, 0.3)
        gas = 600 + random.randint(-40, 40)
        if 15 <= phase < 30:
            temp += (phase - 15) * 1.1
        if 30 <= phase < 45:
            gas += int((phase - 30) * 70)
        if 45 <= phase < 55:
            temp += 14
            gas += 800
        if temp >= TEMP_ON:
            temp_alarm = True
        elif temp <= TEMP_OFF:
            temp_alarm = False
        if gas >= GAS_ON:
            gas_alarm = True
        elif gas <= GAS_OFF:
            gas_alarm = False
        push(
            {
                "temp": round(temp, 1),
                "gas": gas,
                "temp_alarm": temp_alarm,
                "gas_alarm": gas_alarm,
                "state": classify(temp_alarm, gas_alarm),
            },
            "demo",
        )
        time.sleep(1)


# ---------------- Routes ----------------
@app.route("/")
def index():
    cfg = {
        "title": PROJECT_TITLE,
        "team": TEAM_NAME,
        "tempOn": TEMP_ON,
        "tempOff": TEMP_OFF,
        "gasOn": GAS_ON,
        "gasOff": GAS_OFF,
    }
    return render_template("index.html", cfg=cfg, title=PROJECT_TITLE)


@app.route("/api/latest")
def api_latest():
    with lock:
        cur = latest
    return jsonify(
        {
            "latest": cur,
            "stats": stats_snapshot(),
            "server_time": time.time(),
            "remote_buzzer": remote_buzzer_active(),
        }
    )


@app.route("/api/history")
def api_history():
    n = request.args.get("n", default=300, type=int)
    n = max(10, min(n, 900))
    with lock:
        hist = list(history)[-n:]
        ev = list(events)
    return jsonify({"history": hist, "events": ev, "server_time": time.time()})


@app.route("/api/data", methods=["POST"])
def api_data():
    """ESP32 gửi JSON: {"temp": 30.5, "gas": 850, "temp_alarm": false, "gas_alarm": false}"""
    if API_KEY:
        sent = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(sent.encode(), API_KEY.encode()):
            return jsonify({"ok": False, "error": "sai khóa API"}), 401
    body = request.get_json(silent=True) or {}
    try:
        gas = int(body["gas"])
        temp = body.get("temp")
        temp = None if temp is None else float(temp)
        if temp is not None and math.isnan(temp):
            temp = None
        ta = bool(body.get("temp_alarm", False))
        ga = bool(body.get("gas_alarm", False))
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "JSON không hợp lệ"}), 400
    state = body.get("state")
    if state not in VALID_STATES:  # chỉ nhận 4 trạng thái hợp lệ
        state = classify(ta, ga)
    push(
        {
            "temp": temp,
            "gas": gas,
            "temp_alarm": ta,
            "gas_alarm": ga,
            "state": state,
        },
        "wifi",
    )
    # Trả kèm lệnh còi để ESP32 biết có cần bật còi hay không
    return jsonify({"ok": True, "buzzer": remote_buzzer_active()})


@app.route("/api/control", methods=["POST"])
def api_control():
    """Lệnh từ web: {"action":"buzzer","on":true} hoặc {"action":"notify"}"""
    global _last_notify
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    with lock:
        cur = latest
    age = (time.time() - cur["ts"]) if cur else None

    if action == "buzzer":
        on = bool(body.get("on"))
        if on and (age is None or age > BOARD_LIVE_WITHIN):
            return jsonify({"ok": False, "error": "Mạch đang offline, không bật được còi"}), 409
        with lock:
            remote["buzzer"] = on
            remote["until"] = time.time() + REMOTE_BUZZER_MAX
        if on:
            notify_all(
                f"🔔 Còi trên mạch vừa được bật từ web (tự tắt sau {REMOTE_BUZZER_MAX} giây).",
                "Bật còi từ web",
                3,
                ["bell"],
            )
        return jsonify({"ok": True, "buzzer": on})

    if action == "notify":
        if not (NTFY_ENABLED or TG_ENABLED):
            return jsonify({"ok": False, "error": "Chưa cấu hình NTFY_TOPIC trên Render"}), 503
        if cur is None:
            return jsonify({"ok": False, "error": "Chưa có dữ liệu từ mạch"}), 409
        now = time.time()
        if now - _last_notify < 5:  # chống bấm liên tục
            return jsonify({"ok": False, "error": "Vui lòng đợi vài giây rồi gửi lại"}), 429
        _last_notify = now
        t = "--" if cur["temp"] is None else f"{cur['temp']:.1f}°C"
        label = {"BINH THUONG": "Bình thường"}.get(cur["state"], cur["state"])
        notify_all(
            f"📋 Báo cáo từ web\nTrạng thái: {label}\nNhiệt độ: {t} | Gas (ADC): {cur['gas']}",
            "Báo cáo từ web",
            3,
            ["clipboard"],
        )
        return jsonify({"ok": True})

    return jsonify({"ok": False, "error": "lệnh không hợp lệ"}), 400


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="cổng Serial, ví dụ COM3 hoặc /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--no-serial", action="store_true")
    ap.add_argument("--demo", action="store_true", help="dữ liệu giả để thử giao diện")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--web-port", type=int, default=5000)
    args = ap.parse_args()

    if args.demo:
        threading.Thread(target=demo_worker, daemon=True).start()
    elif args.port and not args.no_serial:
        threading.Thread(
            target=serial_worker, args=(args.port, args.baud), daemon=True
        ).start()
    elif not args.no_serial:
        print("Chưa có --port. Chạy ở chế độ chỉ nhận WiFi (hoặc dùng --demo).")

    app.run(host=args.host, port=args.web_port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
