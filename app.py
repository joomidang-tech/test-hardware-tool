#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SY-01B 시린지펌프 기기설정 테스트 툴 (라즈베리파이 4B / 64bit)
================================================================

hey-senlyt v1.0.0 앱의 기기 제어(PumpService)를 브라우저 버튼으로 재현.
포트를 손으로 고르지 않고 **기기 ID(VID/PID)로 자동 인식**한다(findDevicePort 방식).

프로토콜 출처: developer/hey_senlyt/v1.0.0/.../lib/core/hardware/pump_service.dart
  - 시리얼: 9600 8N1, DTR=1/RTS=1
  - 프레임: "/{주소}{명령}\r" (ASCII), 응답 ETX(0x03)까지
  - 상태바이트: '/' 다음+2  →  err=byte&0x0F(0정상/15busy), ready=byte&0x20
  - 기기인식: CH340 VID=0x1A86 PID=0x7523 (Bluetooth/debug 포트 제외) [findDevicePort]
  - 속도(PumpGuard): v 1~1000 / V 1~6000 / c 1~5400 / L 1~20 / steps 0~12000, 제약 v≤c≤V
  - 환산: 전행정 12000 steps = 시린지 용량 mL (용량 기반, 모드별 변경 가능)
          음료 1.25mL → 9600 steps/mL · 향수 0.5mL → 24000 steps/mL

⚠️ USB는 통신 신호만. 펌프 모터 구동 전원(24V)은 별도 SMPS 필수(USB로 구동 불가).

실행:  pip install flask pyserial  →  python3 app.py  →  http://<Pi IP>:8000
"""

import threading
import time

from flask import Flask, request, jsonify

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    raise SystemExit("pyserial 필요:  pip install pyserial flask")

app = Flask(__name__)

# ── 전역 시리얼 상태 ────────────────────────────────────────────────────────
_ser = None
_lock = threading.Lock()
_positions = {}          # 주소별 플런저 위치(steps) 추적
_estopped = False        # 긴급정지 플래그
_device_info = {}        # 마지막 연결 기기 정보

# ── 물리/프로토콜 상수 (v1.0.0 정합) ────────────────────────────────────────
STEPS_PER_ML = 24000
MAX_STEPS = 12000
DEF = dict(startSpeed=500, topAspirate=5000, topDispense=6000, cutoff=500, slope=14)

# 기기설정(PurgeSettingsDialog 정합) — 런타임 조정 가능한 설정값
CONFIG = {
    "aspirateSpeedHz": 5000,   # 흡입 속도 (500~5000)
    "aspirateSlope": 14,       # 흡입 가속 경사 (1~20)
    "dispenseSpeedHz": 6000,   # 배출 속도 (500~6000)
    "dispenseSlope": 14,       # 배출 가속 경사 (1~20)
    "defaultMl": 0.1,          # 기본 흡입/배출량 mL
    "tubeFillMl": 0.05,        # 튜브 필링 1회 주입량 mL (포트당)
    "diagMl": 0.5,             # 진단 흡입량 mL
    "purgeCount": 2,           # 에어 퍼지 횟수
    # 시린지 용량(mL) = 전행정 12000 steps 에 해당하는 mL. 모드별로 다름(변경 가능).
    "capacityFlavorMl": 0.5,   # 음료 시린지 용량 (실제 하드웨어 0.5mL, 변경 가능)
    "capacityAromaMl": 0.5,    # 향수 시린지 용량 (기본 0.5mL)
}


def cap_for(mode):
    return CONFIG["capacityAromaMl"] if mode == "aroma" else CONFIG["capacityFlavorMl"]


def ml_to_steps(ml, mode):
    """용량 기반 환산: 전행정(12000 steps) = 용량 mL. steps = ml/용량 × 12000."""
    cap = cap_for(mode) or 0.5
    return _clamp(round(float(ml) / cap * MAX_STEPS), 0, MAX_STEPS)

# 알려진 USB-시리얼 칩 (findDevicePort: CH340 우선, 설계 타깃 FT232R 병행)
KNOWN_CHIPS = {
    (0x1A86, 0x7523): "CH340 (현행)",
    (0x0403, 0x6001): "FT232R (설계 타깃)",
    (0x0403, 0x6015): "FT231X (FTDI)",
    (0x10C4, 0xEA60): "CP210x (SiLabs)",
}


def _log_line(d, t):
    print(f"[{time.strftime('%H:%M:%S')}] {d} {t}", flush=True)


def _clamp(x, lo, hi):
    return max(lo, min(int(x), hi))


def enforce_speed(v, V, c):
    """PumpGuard.enforceSpeedConstraint: 개별 클램프 후 v ≤ c ≤ V."""
    v = _clamp(v, 1, 1000)
    V = _clamp(V, 1, 6000)
    c = _clamp(_clamp(c, 1, 5400), v, V)
    return v, V, c


# ── 저수준 통신 (pump_service.dart _sendCommand 이식) ───────────────────────
def send_command(cmd, address="1", timeout_s=3.0):
    global _ser
    if _ser is None or not _ser.is_open:
        raise RuntimeError("연결되지 않았습니다. 먼저 기기 인식/연결을 하세요.")
    full = f"/{address}{cmd}\r"
    with _lock:
        _ser.reset_input_buffer()
        _ser.write(full.encode("ascii"))
        _log_line(">>>", repr(full))
        # 브로드캐스트(주소 '_')는 응답이 없음 — 짧게 대기 후 리턴 (Dart _sendBroadcast 정합)
        if address == "_":
            time.sleep(0.05)
            _log_line("<<<", "(broadcast — 응답 없음)")
            return {"raw": "", "raw_hex": "", "ok": True,
                    "error_code": None, "ready": None, "busy": None, "broadcast": True}
        buf = bytearray()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            n = _ser.in_waiting
            if n:
                buf.extend(_ser.read(n))
                if 0x03 in buf:
                    break
            else:
                time.sleep(0.01)
    raw = bytes(buf)
    s = raw.decode("ascii", errors="replace")
    _log_line("<<<", repr(s))
    res = {"raw": s, "raw_hex": raw.hex(" "), "ok": len(raw) > 0,
           "error_code": None, "ready": None, "busy": None}
    i = s.find("/")
    if i != -1 and len(s) > i + 2:
        b = ord(s[i + 2])
        res["error_code"] = b & 0x0F
        res["ready"] = (b & 0x20) != 0
        res["busy"] = (b & 0x0F) == 15
    return res


def poll_until_ready(address="1", timeout_s=30.0):
    if address == "_":
        return poll_all(timeout_s)  # 브로드캐스트는 전체 펌프 폴링
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = send_command("?", address=address, timeout_s=1.5)
        ec = last.get("error_code")
        if last.get("ready") and ec == 0:
            return {"done": True, "last": last}
        if ec not in (None, 0, 15):
            return {"done": False, "last": last, "hw_error": ec}
        time.sleep(0.15)
    return {"done": False, "last": last, "timeout": True}


def poll_all(timeout_s=30.0):
    """전체 펌프(1/2/3) 중 응답하는 것만 Ready까지 폴링 (Dart _pollAllUntilReady 정합).
    미연결 펌프(무응답)는 기다리지 않는다."""
    present = [a for a in ("1", "2", "3")
               if send_command("?", address=a, timeout_s=1.0)["ok"]]
    if not present:
        return {"done": False, "present": [], "note": "응답 펌프 없음"}
    deadline = time.time() + timeout_s
    pending = set(present)
    while time.time() < deadline and pending:
        for a in list(pending):
            r = send_command("?", address=a, timeout_s=1.0)
            ec = r.get("error_code")
            if (r.get("ready") and ec == 0) or ec not in (None, 0, 15):
                pending.discard(a)
        time.sleep(0.1)
    return {"done": not pending, "present": present, "pending": sorted(pending)}


def set_speed(v, V, c, L, address="1"):
    v, V, c = enforce_speed(v, V, c)
    L = _clamp(L, 1, 20)
    return send_command(f"v{v}V{V}c{c}L{L}R", address=address)


def _require_ready():
    if _estopped:
        raise RuntimeError("긴급정지 상태입니다. 먼저 '긴급정지 해제'를 누르세요.")


# ── 기기 자동 인식 (findDevicePort 이식) ────────────────────────────────────
def scan_devices():
    """VID/PID로 펌프 후보를 찾는다. Bluetooth/debug 포트는 제외."""
    cands, skipped = [], []
    for p in list_ports.comports():
        desc = (p.description or "")
        dev = p.device or ""
        low = (desc + " " + dev).lower()
        if "bluetooth" in low or "debug" in low:
            skipped.append({"device": dev, "desc": desc, "why": "bluetooth/debug"})
            continue
        vid, pid = p.vid, p.pid
        chip = KNOWN_CHIPS.get((vid, pid)) if (vid and pid) else None
        cands.append({
            "device": dev,
            "desc": desc,
            "vid": f"0x{vid:04X}" if vid else None,
            "pid": f"0x{pid:04X}" if pid else None,
            "serial": p.serial_number,
            "chip": chip,
            "matched": chip is not None,
        })
    # 매칭된 기기 우선 정렬
    cands.sort(key=lambda c: (not c["matched"]))
    best = next((c["device"] for c in cands if c["matched"]), None)
    return cands, skipped, best


# ── API: 기기 인식/연결 ─────────────────────────────────────────────────────
@app.route("/api/detect")
def api_detect():
    cands, skipped, best = scan_devices()
    return jsonify({"candidates": cands, "skipped": skipped, "best": best})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    global _ser, _device_info
    data = request.get_json(force=True)
    device = data.get("device")
    # device 미지정 → 자동 인식된 best 사용
    detected = None
    if not device:
        cands, _, best = scan_devices()
        device = best
        detected = next((c for c in cands if c["device"] == best), None)
        if not device:
            return jsonify({"ok": False,
                            "msg": "펌프 기기를 자동 인식하지 못했습니다. USB 연결/드라이버 확인 (CH340 VID=0x1A86)"}), 400
    else:
        cands, _, _ = scan_devices()
        detected = next((c for c in cands if c["device"] == device), None)
    try:
        with _lock:
            if _ser is not None and _ser.is_open:
                _ser.close()
            _ser = serial.Serial(port=device, baudrate=9600,
                                 bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                                 stopbits=serial.STOPBITS_ONE, timeout=0.1,
                                 dsrdtr=False, rtscts=False)
            _ser.dtr = True
            _ser.rts = True
        time.sleep(0.1)
        _device_info = detected or {"device": device}
        label = device
        if detected and detected.get("chip"):
            label += f"  [{detected['chip']}]"
        return jsonify({"ok": True, "msg": f"연결됨: {label} @ 9600 8N1",
                        "device": _device_info})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"연결 실패: {e}"}), 400


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    global _ser
    with _lock:
        if _ser is not None and _ser.is_open:
            _ser.close()
        _ser = None
    return jsonify({"ok": True, "msg": "연결 해제됨"})


# ── API: 초기화 / 안전 ──────────────────────────────────────────────────────
@app.route("/api/init", methods=["POST"])
def api_init():
    data = request.get_json(force=True)
    addr = str(data.get("address", "1"))
    mode = data.get("mode", "flavor")
    home = "Z1R" if mode == "aroma" else "ZR"
    safe = "I12R"  # 공기(Air) 포트 — 양 모드 안전포트(최신 dev 정합, 기존 aroma I9R은 누액 위험)
    steps = []
    try:
        _require_ready()
        # Step 0: 상태 리셋 TR (Dart initialize Step 0 정합) — best-effort
        try:
            steps.append({"cmd": "TR", "result": send_command("TR", address=addr)})
            poll_until_ready(addr, 10)
        except Exception as e:
            steps.append({"cmd": "TR", "result": {"warn": str(e)}})
        # Step 1: 스톨 전류
        steps.append({"cmd": "U200,5R", "result": send_command("U200,5R", address=addr)})
        time.sleep(0.3)
        # Step 2~3: 원점 복귀 + 완료 대기
        steps.append({"cmd": home, "result": send_command(home, address=addr)})
        steps.append({"cmd": "poll", "result": poll_until_ready(addr, 30).get("last")})
        # Step 4: 안전 포트(공기12)
        steps.append({"cmd": safe, "result": send_command(safe, address=addr)})
        poll_until_ready(addr, 10)
        _positions[addr] = 0
        return jsonify({"ok": True, "steps": steps, "position": 0})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "steps": steps}), 400


@app.route("/api/terminate", methods=["POST"])
def api_terminate():
    """강제 재초기화: TR → home → safe port (terminateAndInitialize)."""
    data = request.get_json(force=True)
    addr = str(data.get("address", "1"))
    mode = data.get("mode", "flavor")
    home = "Z1R" if mode == "aroma" else "ZR"
    safe = "I12R"  # 공기(Air) 안전포트 (최신 dev 정합)
    steps = []
    try:
        steps.append({"cmd": "TR", "result": send_command("TR", address=addr)})
        time.sleep(0.5)
        steps.append({"cmd": home, "result": send_command(home, address=addr)})
        poll_until_ready(addr, 30)
        steps.append({"cmd": safe, "result": send_command(safe, address=addr)})
        poll_until_ready(addr, 5)
        _positions[addr] = 0
        return jsonify({"ok": True, "steps": steps, "position": 0})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "steps": steps}), 400


@app.route("/api/estop", methods=["POST"])
def api_estop():
    """긴급정지 = TR + 플래그 설정 (emergencyStop)."""
    global _estopped
    data = request.get_json(force=True)
    addr = str(data.get("address", "1"))
    try:
        r = send_command("TR", address=addr)
        _estopped = True
        return jsonify({"ok": True, "result": r, "estopped": True})
    except Exception as e:
        _estopped = True
        return jsonify({"ok": False, "msg": str(e), "estopped": True}), 400


@app.route("/api/estop_clear", methods=["POST"])
def api_estop_clear():
    """긴급정지 해제 + 위치/초기화 상태 리셋 (clearEmergencyStop)."""
    global _estopped
    _estopped = False
    _positions.clear()
    return jsonify({"ok": True, "msg": "긴급정지 해제 및 위치 리셋", "estopped": False})


# ── API: 밸브 ───────────────────────────────────────────────────────────────
@app.route("/api/valve", methods=["POST"])
def api_valve():
    data = request.get_json(force=True)
    addr = str(data.get("address", "1"))
    port = int(data.get("port", 1))
    if not (1 <= port <= 12):
        return jsonify({"ok": False, "msg": "port는 1~12"}), 400
    try:
        _require_ready()
        r = send_command(f"I{port}R", address=addr)
        w = poll_until_ready(addr, 5)
        return jsonify({"ok": True, "result": r, "wait": w.get("last")})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


# ── API: 시린지 플런저 (올리기=흡입 P / 내리기=배출 D) ──────────────────────
@app.route("/api/plunger", methods=["POST"])
def api_plunger():
    """action: 'up'(올리기=흡입 P) | 'down'(내리기=배출 D). ml 또는 steps."""
    data = request.get_json(force=True)
    addr = str(data.get("address", "1"))
    action = data.get("action", "down")
    mode = data.get("mode", "flavor")
    if data.get("steps") not in (None, ""):
        steps = int(data["steps"])
    elif data.get("ml") not in (None, ""):
        steps = ml_to_steps(data["ml"], mode)
    else:
        steps = 0
    steps = _clamp(steps, 0, MAX_STEPS)
    if steps == 0:
        return jsonify({"ok": False, "msg": "steps/ml 가 0"}), 400
    up = (action == "up")
    letter = "P" if up else "D"
    top = int(data.get("speedHz") or (CONFIG["aspirateSpeedHz"] if up else CONFIG["dispenseSpeedHz"]))
    slope = CONFIG["aspirateSlope"] if up else CONFIG["dispenseSlope"]
    port = data.get("port")
    cap = cap_for(mode)
    try:
        _require_ready()
        # 흡입=소스 포트 / 배출=출력 포트로 밸브를 먼저 이동(포트 지정 시)
        if port not in (None, ""):
            send_command(f"I{int(port)}R", address=addr)
            poll_until_ready(addr, 5)
        set_speed(DEF["startSpeed"], top, DEF["cutoff"], slope, address=addr)
        r = send_command(f"{letter}{steps}R", address=addr)
        w = poll_until_ready(addr, 40)
        cur = _positions.get(addr, 0)
        cur = _clamp(cur + steps if up else cur - steps, 0, MAX_STEPS)
        _positions[addr] = cur
        return jsonify({"ok": True, "action": action, "port": port,
                        "cmd": (f"I{port}R→" if port else "") + f"{letter}{steps}R",
                        "steps": steps, "ml": round(steps / MAX_STEPS * cap, 4),
                        "result": r, "done": w.get("done"), "position": cur})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


@app.route("/api/plunger_abs", methods=["POST"])
def api_plunger_abs():
    """절대 위치 이동 (A{steps}R, movePlungerAbs)."""
    data = request.get_json(force=True)
    addr = str(data.get("address", "1"))
    steps = _clamp(int(data.get("steps", 0)), 0, MAX_STEPS)
    try:
        _require_ready()
        set_speed(DEF["startSpeed"], DEF["topAspirate"], DEF["cutoff"], DEF["slope"], address=addr)
        r = send_command(f"A{steps}R", address=addr)
        w = poll_until_ready(addr, 40)
        _positions[addr] = steps
        return jsonify({"ok": True, "cmd": f"A{steps}R", "position": steps,
                        "ml": round(steps / STEPS_PER_ML, 4), "result": r, "done": w.get("done")})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


@app.route("/api/empty", methods=["POST"])
def api_empty():
    """전량 배출 = 절대위치 0 (dispenseAll)."""
    data = request.get_json(force=True)
    addr = str(data.get("address", "1"))
    try:
        _require_ready()
        r = send_command("A0R", address=addr)
        poll_until_ready(addr, 40)
        _positions[addr] = 0
        return jsonify({"ok": True, "cmd": "A0R", "position": 0, "result": r})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


# ── API: 속도/파라미터 설정 (setDefault* / _setSpeed) ───────────────────────
@app.route("/api/setspeed", methods=["POST"])
def api_setspeed():
    data = request.get_json(force=True)
    addr = str(data.get("address", "1"))
    v = int(data.get("v", DEF["startSpeed"]))
    V = int(data.get("V", DEF["topDispense"]))
    c = int(data.get("c", DEF["cutoff"]))
    L = int(data.get("L", DEF["slope"]))
    try:
        ev, eV, ec = enforce_speed(v, V, c)
        eL = _clamp(L, 1, 20)
        r = send_command(f"v{ev}V{eV}c{ec}L{eL}R", address=addr)
        return jsonify({"ok": True, "applied": {"v": ev, "V": eV, "c": ec, "L": eL},
                        "result": r})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


# ── API: 기기설정 (mL·속도·퍼지 등 — PurgeSettingsDialog 정합) ──────────────
@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify({"ok": True, "config": CONFIG})
    data = request.get_json(force=True)
    # 허용 키만 갱신 + 범위 클램프
    if "aspirateSpeedHz" in data: CONFIG["aspirateSpeedHz"] = _clamp(data["aspirateSpeedHz"], 500, 5000)
    if "aspirateSlope" in data:   CONFIG["aspirateSlope"] = _clamp(data["aspirateSlope"], 1, 20)
    if "dispenseSpeedHz" in data: CONFIG["dispenseSpeedHz"] = _clamp(data["dispenseSpeedHz"], 500, 6000)
    if "dispenseSlope" in data:   CONFIG["dispenseSlope"] = _clamp(data["dispenseSlope"], 1, 20)
    # 시린지 용량(모드별) — 저장 시 서버에도 반영해야 ml_to_steps 정합
    if "capacityFlavorMl" in data: CONFIG["capacityFlavorMl"] = max(0.05, min(float(data["capacityFlavorMl"]), 2.0))
    if "capacityAromaMl" in data:  CONFIG["capacityAromaMl"] = max(0.05, min(float(data["capacityAromaMl"]), 2.0))
    # mL 값은 용량(최대 2.0mL)까지 허용 — 기존 0.5 하드코딩 상한 제거
    if "defaultMl" in data:  CONFIG["defaultMl"] = max(0.0, min(float(data["defaultMl"]), 2.0))
    if "tubeFillMl" in data: CONFIG["tubeFillMl"] = max(0.0, min(float(data["tubeFillMl"]), 2.0))
    if "diagMl" in data:     CONFIG["diagMl"] = max(0.0, min(float(data["diagMl"]), 2.0))
    if "purgeCount" in data: CONFIG["purgeCount"] = _clamp(data["purgeCount"], 1, 10)
    return jsonify({"ok": True, "config": CONFIG})


@app.route("/api/tubefill", methods=["POST"])
def api_tubefill():
    """튜브 필링: 밸브를 포트로 → 지정 mL 흡입 (설정 tubeFillMl 기본)."""
    data = request.get_json(force=True)
    addr = str(data.get("address", "1"))
    mode = data.get("mode", "flavor")
    port = int(data.get("port", 1))
    ml = float(data.get("ml", CONFIG["tubeFillMl"]))
    steps = ml_to_steps(ml, mode)
    try:
        _require_ready()
        send_command(f"I{port}R", address=addr); poll_until_ready(addr, 5)
        set_speed(DEF["startSpeed"], CONFIG["aspirateSpeedHz"], DEF["cutoff"],
                  CONFIG["aspirateSlope"], address=addr)
        r = send_command(f"P{steps}R", address=addr)
        w = poll_until_ready(addr, 40)
        cur = _clamp(_positions.get(addr, 0) + steps, 0, MAX_STEPS)
        _positions[addr] = cur
        return jsonify({"ok": True, "cmd": f"I{port}R→P{steps}R", "ml": ml,
                        "position": cur, "result": r, "done": w.get("done")})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


@app.route("/api/purge", methods=["POST"])
def api_purge():
    """에어 퍼지: 공기포트(12)로 → N회 반복 흡입/배출 (설정 purgeCount 기본)."""
    data = request.get_json(force=True)
    addr = str(data.get("address", "1"))
    count = _clamp(data.get("count", CONFIG["purgeCount"]), 1, 10)
    mode = "aroma" if addr == "_" else "flavor"  # 브로드캐스트=향수
    steps = ml_to_steps(0.1, mode)               # 용량 기반 환산
    done = []
    try:
        _require_ready()
        send_command("I12R", address=addr); poll_until_ready(addr, 5)  # 공기 포트
        for i in range(count):
            set_speed(DEF["startSpeed"], CONFIG["aspirateSpeedHz"], DEF["cutoff"], CONFIG["aspirateSlope"], address=addr)
            send_command(f"P{steps}R", address=addr); poll_until_ready(addr, 20)
            set_speed(DEF["startSpeed"], CONFIG["dispenseSpeedHz"], DEF["cutoff"], CONFIG["dispenseSlope"], address=addr)
            send_command(f"D{steps}R", address=addr); poll_until_ready(addr, 20)
            done.append(i + 1)
        _positions[addr] = 0
        return jsonify({"ok": True, "count": count, "cycles": done})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "cycles": done}), 400


# ── API: 상태/진단 ──────────────────────────────────────────────────────────
@app.route("/api/status", methods=["POST"])
def api_status():
    data = request.get_json(force=True)
    addr = str(data.get("address", "1"))
    try:
        r = send_command("?", address=addr)
        return jsonify({"ok": True, "result": r, "position": _positions.get(addr, 0),
                        "estopped": _estopped})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


@app.route("/api/statuscheck", methods=["POST"])
def api_statuscheck():
    """연결 직후 전체 펌프 상태 자동 점검 (flutter _pollAllUntilReady 대응).
    향수=A/B/C(1,2,3), 음료=단일(1) 을 각각 '?'로 핑."""
    data = request.get_json(force=True)
    mode = data.get("mode", "flavor")
    addrs = ["1", "2", "3"] if mode == "aroma" else ["1"]
    pumps = []
    for a in addrs:
        try:
            r = send_command("?", address=a, timeout_s=1.2)
            pumps.append({"address": a, "responded": r["ok"],
                          "ready": r["ready"], "error_code": r["error_code"]})
        except Exception as e:
            pumps.append({"address": a, "responded": False, "error": str(e)})
    return jsonify({"ok": True, "pumps": pumps})


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(force=True)
    cmd = (data.get("cmd") or "").strip()
    addr = str(data.get("address", "1"))
    if not cmd:
        return jsonify({"ok": False, "msg": "명령이 비었습니다"}), 400
    try:
        return jsonify({"ok": True, "result": send_command(cmd, address=addr)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


# ── API: 전체(3대 브로드캐스트, 주소 '_') ───────────────────────────────────
@app.route("/api/broadcast", methods=["POST"])
def api_broadcast():
    """action: init | valve | plunger_abs | empty  (initializeAll 등)."""
    data = request.get_json(force=True)
    action = data.get("action")
    mode = data.get("mode", "flavor")
    try:
        _require_ready()
        if action == "init":
            home = "Z1R" if mode == "aroma" else "ZR"
            safe = "I9R" if mode == "aroma" else "I12R"
            send_command("U200,5R", address="_", timeout_s=0.5)
            time.sleep(0.3)
            send_command(home, address="_", timeout_s=0.5)
            time.sleep(2.0)
            send_command(safe, address="_", timeout_s=0.5)
            for a in ("1", "2", "3"):
                _positions[a] = 0
            return jsonify({"ok": True, "msg": f"전체 초기화(broadcast) {home}"})
        elif action == "valve":
            port = int(data.get("port", 1))
            send_command(f"I{port}R", address="_", timeout_s=0.5)
            return jsonify({"ok": True, "msg": f"전체 밸브 → {port}"})
        elif action == "plunger_abs":
            steps = _clamp(int(data.get("steps", 0)), 0, MAX_STEPS)
            send_command(f"v{DEF['startSpeed']}V{DEF['topAspirate']}c{DEF['cutoff']}L{DEF['slope']}R",
                         address="_", timeout_s=0.5)
            time.sleep(0.05)
            send_command(f"A{steps}R", address="_", timeout_s=0.5)
            for a in ("1", "2", "3"):
                _positions[a] = steps
            return jsonify({"ok": True, "msg": f"전체 플런저 → {steps} steps"})
        elif action == "empty":
            send_command("A0R", address="_", timeout_s=0.5)
            for a in ("1", "2", "3"):
                _positions[a] = 0
            return jsonify({"ok": True, "msg": "전체 전량 배출"})
        return jsonify({"ok": False, "msg": f"알 수 없는 action: {action}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 400


# ── HTML ────────────────────────────────────────────────────────────────────
PAGE = r"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>시린지펌프 기기설정 테스트</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#0f1115;color:#e6e6e6}
  h1{font-size:18px;margin:0 0 4px} .sub{color:#8a94a6;font-size:12px;margin-bottom:12px}
  .card{background:#1a1d24;border:1px solid #2a2f3a;border-radius:10px;padding:14px;margin-bottom:12px}
  .card h2{font-size:14px;margin:0 0 10px;color:#b6c0d0}
  .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}
  label{font-size:13px;color:#b6c0d0} input,select{background:#0f1115;color:#e6e6e6;border:1px solid #333a47;border-radius:6px;padding:8px;font-size:14px}
  input[type=number]{width:90px}
  button{background:#2b6cf6;color:#fff;border:0;border-radius:8px;padding:10px 14px;font-size:14px;cursor:pointer}
  button:active{transform:translateY(1px)} button.gray{background:#39404d} button.red{background:#e5484d} button.green{background:#30a46c} button.amber{background:#d9822b}
  #log{background:#05070a;border:1px solid #222;border-radius:8px;padding:10px;height:220px;overflow:auto;font-family:ui-monospace,monospace;font-size:12px;white-space:pre-wrap}
  .warn{background:#3a2a12;border:1px solid #6b4a1a;color:#ffce85;padding:10px;border-radius:8px;font-size:12.5px;margin-bottom:12px}
  .ml{color:#8a94a6;font-size:12px} .pill{display:inline-block;padding:2px 8px;border-radius:20px;font-size:12px;background:#222836}
  #devinfo{font-size:12px;color:#8a94a6}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  hr{border:0;border-top:1px solid #2a2f3a;margin:10px 0}
  .topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
  #manual{display:none;position:fixed;top:0;right:0;bottom:0;width:min(460px,94vw);background:#12151b;border-left:1px solid #2a2f3a;padding:18px;overflow:auto;z-index:50;box-shadow:-10px 0 30px rgba(0,0,0,.55)}
  #manual h3{margin:14px 0 3px;font-size:13.5px;color:#7db3ff} #manual h3:first-of-type{margin-top:4px}
  #manual p{margin:2px 0;font-size:12.5px;color:#cdd6e4;line-height:1.55} #manual .b{color:#fff;font-weight:600}
</style></head><body>
<div class="topbar">
  <div><h1>🧪 시린지펌프 기기설정 테스트</h1>
  <div class="sub">SY-01B · 9600 8N1 · /{주소}{명령}R · 기기 자동인식(CH340 VID 0x1A86) — hey-senlyt v1.0.0 규격</div></div>
  <button class="gray" onclick="toggleManual()">📖 매뉴얼</button>
</div>
<div class="warn">⚠️ USB는 <b>통신 신호만</b>. 펌프 모터 구동 전원(24V)은 <b>별도 SMPS</b> 필요 — 없으면 명령은 나가도 펌프가 안 움직입니다.</div>

<div id="manual">
  <button class="gray" style="float:right" onclick="toggleManual()">✕ 닫기</button>
  <h1 style="font-size:16px">📖 버튼 설명서</h1>
  <p style="font-size:12px;color:#8a94a6">각 버튼이 실제로 펌프에 무슨 명령을 보내는지 문맥과 함께 설명합니다.</p>

  <h3>먼저 — 제품 종류(음료/향수)</h3>
  <p>이 선택 하나로 전부 분기됩니다. <span class="b">음료</span>=펌프 1대·초기화 ZR(풀포스). <span class="b">향수</span>=펌프 3대(A/B/C)를 <b>브로드캐스트로 한꺼번에</b> 제어·초기화 Z1R(하프포스). 밸브 포트 이름·기본 용량도 여기 따라 바뀝니다(용량은 기기설정에서 변경).</p>

  <h3>1. 연결</h3>
  <p><span class="b">기기 자동 인식 &amp; 연결</span>: USB에 꽂힌 CH340/FT232R 어댑터를 VID/PID로 찾아 9600 8N1로 엽니다(Bluetooth 포트는 제외). <b>연결 직후 전체 펌프(향수 A/B/C, 음료 1대) 상태를 자동 점검</b>해 표시합니다. <span class="b">기기 목록 스캔</span>: 인식 후보를 로그에 나열. <span class="b">Disconnect</span>: 연결 해제. 펌프를 개별로 고를 필요 없이, 향수는 3대가 함께 동작합니다.</p>

  <h3>2. 기기설정 (조작 전 먼저)</h3>
  <p><span class="b">시린지 용량 mL</span>: 전행정 12000스텝이 몇 mL인지(음료·향수 각각, 기본 0.5). 바꾸면 mL↔스텝 환산·최대 주입량 즉시 반영. 그 외 기본 mL·튜브필링 mL·진단 mL·에어퍼지 횟수·흡입/배출 속도(Hz=최고속도)·slope(가속). <span class="b">저장</span>하면 이후 동작에 반영. 에어퍼지는 횟수만 여기서 정하고 실행은 3번에서.</p>

  <h3>3. 펌프 제어 (초기화 · 밸브 · 흡입/배출)</h3>
  <p><b>초기화 &amp; 안전</b> — <span class="b">초기화</span>: TR(리셋)→U200,5R(스톨전류)→ZR/Z1R(원점 복귀)→안전포트(공기12)로 기준점 잡기(동작 전 필수). <span class="b">강제 재초기화</span>: 꼬였을 때 TR 후 다시 원점. <span class="b">긴급정지(TR)</span>: 즉시 중단. <span class="b">긴급정지 해제</span>: 플래그·위치 리셋(재초기화 필요).</p>
  <p><b>① 밸브</b> — 12포트 밸브를 선택 포트로 회전(I명령). 이름은 제품 기준(음료: 딸기·감미료10·세척액11·공기12 / 향수: 알코올·향료1~9·배출11·공기12).</p>
  <p><b>② 흡입/배출</b> — <span class="b">흡입(뽑아올리기 P)</span>: ①에서 고른 포트에서 시린지로 빨아들임. <span class="b">배출(내려 짜내기 D)</span>: 출력포트(음료2/향수11)로 밀어냄. <span class="b">전량 비우기(A0)</span>: 끝까지 밀어 비움.</p>
  <p><b>보조/고급</b> — <span class="b">관 채우기</span>: 선택 포트에서 튜브필링 mL 흡입. <span class="b">에어 퍼지</span>: 밸브를 공기포트(12)로 자동 이동 후 설정 횟수만큼 흡입·배출 반복해 잔여물 제거. <span class="b">직접 위치</span>: 0~12000 스텝 직접 이동(A명령), 12000=전행정=용량 전체.</p>

  <h3>4. 상태 &amp; Raw</h3>
  <p><span class="b">상태 조회(?)</span>: 상태바이트를 읽어 준비/오류코드를 표시. <span class="b">Raw</span>: ZR·I3R·D2400R 같은 명령을 직접 전송(디버깅).</p>

  <h3>위치 표시 · 전원</h3>
  <p>상단 <span class="b">위치</span>=현재 플런저 스텝과 환산 mL. ⚠️ USB는 통신 신호만 — 펌프가 실제로 움직이려면 24V 별도 전원(SMPS)이 필요합니다.</p>
</div>

<!-- 연결 -->
<div class="card"><h2>1. 연결 (기기 자동 인식)</h2>
  <div class="row">
    <button class="green" onclick="autoConnect()">🔍 기기 자동 인식 &amp; 연결</button>
    <button class="gray" onclick="detect()">기기 목록 스캔</button>
    <button class="gray" onclick="disconnect()">Disconnect</button>
    <span>상태: <b id="status" style="color:#e5484d">미연결</b></span>
  </div>
  <div class="row"><span id="devinfo">USB에 꽂힌 CH340/FT232R 어댑터를 자동 탐색합니다 (Bluetooth 포트 제외).</span></div>
  <div class="row"><label>제품 종류</label>
    <select id="mode">
      <option value="flavor">🥤 음료 (펌프 1대 · ZR)</option>
      <option value="aroma">🌸 향수 (펌프 3대 · Z1R)</option>
    </select>
    <span class="pill">위치: <b id="pos">0</b> steps (<span id="posml">0</span> mL)</span>
  </div>
  <div class="row"><span id="pumpStatus" class="ml">연결하면 펌프 상태를 자동 점검합니다. (향수는 3대를 한꺼번에 제어)</span></div>
</div>

<!-- 기기설정 -->
<div class="card"><h2>2. 기기설정 (용량 · mL · 속도 · 퍼지) — 조작 전에 먼저 설정</h2>
  <div class="row grid">
    <span><label>음료 시린지 용량 mL</label><br><input id="cfgCapFlavor" type="number" value="0.5" step="0.05" min="0.05" max="2"></span>
    <span><label>향수 시린지 용량 mL</label><br><input id="cfgCapAroma" type="number" value="0.5" step="0.05" min="0.05" max="2"></span>
    <span><label>기본 흡입/배출 mL</label><br><input id="cfgDefaultMl" type="number" value="0.1" step="0.05" min="0"></span>
    <span><label>튜브 필링 mL/포트</label><br><input id="tubeMl" type="number" value="0.05" step="0.01" min="0"></span>
  </div>
  <div class="row grid">
    <span><label>진단 흡입량 mL</label><br><input id="cfgDiagMl" type="number" value="0.5" step="0.05" min="0.1"></span>
    <span><label>에어 퍼지 횟수</label><br><input id="cfgPurge" type="number" value="2" min="1" max="10"></span>
    <span><label>흡입 속도 Hz</label><br><input id="cfgAspHz" type="number" value="5000"></span>
    <span><label>흡입 slope</label><br><input id="cfgAspL" type="number" value="14"></span>
  </div>
  <div class="row grid">
    <span><label>배출 속도 Hz</label><br><input id="cfgDisHz" type="number" value="6000"></span>
    <span><label>배출 slope</label><br><input id="cfgDisL" type="number" value="14"></span>
  </div>
  <div class="row">
    <button class="green" onclick="saveConfig()">💾 설정 저장</button>
    <button class="gray" onclick="loadConfig()">불러오기</button>
    <span class="ml">에어 퍼지 <b>횟수</b>만 여기서 설정 · 실행은 3번(초기화·안전)</span>
  </div>
  <div class="row"><span class="ml">용량을 바꾸면 mL↔steps 환산·최대 주입량이 즉시 반영됩니다. 흡입/배출 속도(Hz)가 곧 펌프 최고속도, slope는 가속입니다.</span></div>
</div>

<!-- 3. 펌프 제어 (초기화 + 밸브 + 흡입/배출) -->
<div class="card"><h2>3. 펌프 제어 — 초기화 · 밸브 · 흡입/배출</h2>

  <div class="row"><span style="font-weight:600;color:#e5a04b">초기화 &amp; 안전</span></div>
  <div class="row">
    <button onclick="api('/api/init',{address:addr(),mode:mode()})">⚙️ 초기화</button>
    <button class="amber" onclick="api('/api/terminate',{address:addr(),mode:mode()})">🔁 강제 재초기화</button>
    <button class="red" onclick="api('/api/estop',{address:addr()})">■ 긴급정지(TR)</button>
    <button class="gray" onclick="api('/api/estop_clear',{})">긴급정지 해제</button>
  </div>

  <hr>

  <div class="row"><span style="font-weight:600;color:#7db3ff">① 밸브 — 액을 뽑거나 짜낼 포트로 돌리기</span></div>
  <div class="row"><label>포트</label>
    <select id="valvePort" style="min-width:170px"></select>
    <button onclick="api('/api/valve',{address:addr(),port:parseInt(val('valvePort'))})">↻ 이 포트로 밸브 돌리기</button>
    <span class="ml">먼저 포트를 고르고 밸브를 돌립니다</span>
  </div>

  <hr>

  <div class="row"><span style="font-weight:600;color:#7db3ff">② 흡입 / 배출 — 시린지로 뽑고 짜기</span></div>
  <div class="row"><label>양</label>
    <input id="ml" type="number" value="0.1" step="0.05" min="0">
    <span class="ml">mL (= <span id="stepPreview">2400</span> steps · 최대 <span id="capLbl">0.5</span>mL)</span>
    <label>속도</label><input id="speed" type="number" value="6000" min="1" max="6000"><span class="ml">Hz</span>
  </div>
  <div class="row">
    <button class="green" onclick="plungerUp()">⬆ 흡입 (뽑아올리기)</button>
    <button onclick="plungerDown()">⬇ 배출 (내려 짜내기)</button>
    <button class="gray" onclick="api('/api/empty',{address:addr()})">🚽 전량 비우기</button>
  </div>
  <div class="row"><span class="ml">흡입 = ①에서 고른 포트에서 시린지로 빨아들임 · 배출 = 출력포트(음료 2 · 향수 11)로 밀어냄</span></div>

  <hr>

  <div class="row"><span style="font-weight:600;color:#8a94a6">보조 / 고급</span></div>
  <div class="row"><label>튜브 필링</label>
    <button class="gray" onclick="api('/api/tubefill',{address:addr(),mode:mode(),port:parseInt(val('valvePort')),ml:val('tubeMl')})">🧵 관 채우기</button>
    <span class="ml">선택 포트에서 설정의 '튜브 필링 mL'만큼 흡입해 관을 채웁니다</span>
  </div>
  <div class="row"><label>에어 퍼지</label>
    <button class="gray" onclick="api('/api/purge',{address:addr(),count:val('cfgPurge')})">💨 에어 퍼지</button>
    <span class="ml">밸브를 자동으로 공기포트(12)로 돌린 뒤, 설정 횟수만큼 흡입·배출 반복 (관 잔여물 제거)</span>
  </div>
  <div class="row"><label>직접 위치</label>
    <input id="absSteps" type="range" min="0" max="12000" value="0" step="100" style="flex:1;min-width:160px" oninput="absLbl.textContent=this.value">
    <span class="ml"><b id="absLbl">0</b> / 12000 steps</span>
    <button onclick="api('/api/plunger_abs',{address:addr(),steps:parseInt(val('absSteps'))})">이동</button>
  </div>
</div>

<!-- 상태/Raw -->
<div class="card"><h2>4. 상태 &amp; Raw</h2>
  <div class="row">
    <button class="gray" onclick="api('/api/status',{address:addr()})">? 상태 조회</button>
    <input id="raw" placeholder="Raw: ZR, ?, I3R, D2400R" style="flex:1;min-width:160px">
    <button class="gray" onclick="doRaw()">전송</button>
  </div>
</div>

<div class="card"><div class="row"><h2 style="margin:0">로그</h2><button class="gray" onclick="log_.textContent=''">지우기</button></div>
  <div id="log"></div></div>

<script>
const $=id=>document.getElementById(id); const log_=$('log');
let CAP={flavor:0.5, aroma:0.5};
function log(m){log_.textContent+=m+"\n";log_.scrollTop=log_.scrollHeight;}
function mode(){return $('mode').value;}
function addr(){return mode()==='aroma' ? '_' : '1';}  // 향수=브로드캐스트(3대 동시), 음료=단일
function val(id){return $(id).value;}
function activeCap(){return CAP[mode()]||0.5;}
function mlToSteps(ml){return Math.max(0,Math.min(Math.round((parseFloat(ml)||0)/activeCap()*12000),12000));}
function stepsToMl(s){return s/12000*activeCap();}
function outputPort(){return mode()==='aroma'?11:2;}
const PORTS={
  flavor:{1:'딸기',2:'배출',3:'피스타치오',4:'망고',5:'코코넛',6:'솔향',7:'복숭아',8:'블루베리',9:'레몬',10:'감미료',11:'세척액',12:'공기(Air)'},
  aroma:{1:'알코올',2:'향료1',3:'향료2',4:'향료3',5:'향료4',6:'향료5',7:'향료6',8:'향료7',9:'향료8',10:'향료9',11:'배출',12:'공기(Air)'}
};
function toggleManual(){const o=$('manual');o.style.display=(o.style.display==='block')?'none':'block';}
function setPos(p){ if(p===undefined||p===null)return; $('pos').textContent=p; $('posml').textContent=stepsToMl(p).toFixed(4);}
async function api(url,body){
  try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
    const j=await r.json(); log((j.ok?'✅ ':'❌ ')+url.replace('/api/','')+' → '+JSON.stringify(j));
    if('position' in j) setPos(j.position);
    if('estopped' in j){$('status').textContent=j.estopped?'긴급정지':'연결됨';$('status').style.color=j.estopped?'#e5484d':'#30a46c';}
    return j;}catch(e){log('❌ '+url+' → '+e);}
}
async function detect(){
  const r=await fetch('/api/detect'); const j=await r.json();
  log('🔎 스캔: 후보 '+j.candidates.length+'개'+(j.skipped.length?(' / 제외 '+j.skipped.length+'개(BT)'):''));
  j.candidates.forEach(c=>log('  '+(c.matched?'✅':'  ')+' '+c.device+'  '+(c.chip||'(미인식칩)')+'  VID '+(c.vid||'?')+' PID '+(c.pid||'?')+(c.serial?(' S/N '+c.serial):'')));
  j.skipped.forEach(s=>log('  ⛔ 제외 '+s.device+' ('+s.why+') '+s.desc));
  if(j.best) log('👉 자동 선택 대상: '+j.best); else log('⚠️ 인식된 펌프 없음 (CH340/FT232R 연결 확인)');
  return j;
}
async function autoConnect(){
  await detect();
  const j=await api('/api/connect',{});
  if(j&&j.ok){$('status').textContent='연결됨';$('status').style.color='#30a46c';$('devinfo').textContent=j.msg;
    await statusCheck();}
}
async function statusCheck(){
  const j=await api('/api/statuscheck',{mode:mode()});
  if(!j||!j.ok)return;
  const nm={'1':'A(TOP)','2':'B(MID)','3':'C(BASE)'};
  const parts=j.pumps.map(p=>{
    let s = !p.responded ? '⚠️ 무응답' : (p.error_code===0?'✅ Ready':(p.error_code===15?'⏳ Busy':'❌ err'+p.error_code));
    return (mode()==='aroma'?nm[p.address]:'펌프')+' '+s;
  });
  $('pumpStatus').textContent='펌프 상태 — '+parts.join('  ·  ');
}
async function disconnect(){await api('/api/disconnect',{});$('status').textContent='미연결';$('status').style.color='#e5484d';}
async function doRaw(){await api('/api/send',{address:addr(),cmd:$('raw').value});}
function plungerUp(){api('/api/plunger',{address:addr(),mode:mode(),action:'up',steps:mlToSteps(val('ml')),port:parseInt(val('valvePort')),speedHz:val('speed')});}
function plungerDown(){api('/api/plunger',{address:addr(),mode:mode(),action:'down',steps:mlToSteps(val('ml')),port:outputPort(),speedHz:val('speed')});}
function fillSel(id){const map=PORTS[mode()],sel=$(id),keep=sel.value;sel.innerHTML='';for(let p=1;p<=12;p++){const o=document.createElement('option');o.value=p;o.textContent=p+' · '+map[p];sel.appendChild(o);}if(keep)sel.value=keep;}
function updateCapUI(){const c=activeCap();$('capLbl').textContent=c;$('ml').max=c;$('cfgDefaultMl').max=c;$('tubeMl').max=c;$('stepPreview').textContent=mlToSteps(val('ml'));setPos(parseInt($('pos').textContent)||0);}
function applyMode(){fillSel('valvePort');updateCapUI();}
async function loadConfig(){
  const r=await fetch('/api/config'); const j=await r.json(); const c=j.config;
  CAP.flavor=c.capacityFlavorMl; CAP.aroma=c.capacityAromaMl;
  $('cfgCapFlavor').value=c.capacityFlavorMl; $('cfgCapAroma').value=c.capacityAromaMl;
  $('cfgDefaultMl').value=c.defaultMl; $('tubeMl').value=c.tubeFillMl; $('cfgDiagMl').value=c.diagMl; $('cfgPurge').value=c.purgeCount;
  $('cfgAspHz').value=c.aspirateSpeedHz; $('cfgAspL').value=c.aspirateSlope; $('cfgDisHz').value=c.dispenseSpeedHz; $('cfgDisL').value=c.dispenseSlope;
  $('ml').value=c.defaultMl; updateCapUI();
  log('⚙️ 설정 불러옴: '+JSON.stringify(c));
}
async function saveConfig(){
  const body={capacityFlavorMl:val('cfgCapFlavor'),capacityAromaMl:val('cfgCapAroma'),defaultMl:val('cfgDefaultMl'),tubeFillMl:val('tubeMl'),
    diagMl:val('cfgDiagMl'),purgeCount:val('cfgPurge'),aspirateSpeedHz:val('cfgAspHz'),aspirateSlope:val('cfgAspL'),dispenseSpeedHz:val('cfgDisHz'),dispenseSlope:val('cfgDisL')};
  const j=await api('/api/config',body);
  if(j&&j.ok){CAP.flavor=j.config.capacityFlavorMl;CAP.aroma=j.config.capacityAromaMl;$('ml').value=j.config.defaultMl;updateCapUI();}
}
$('mode').addEventListener('change',applyMode);
$('ml').addEventListener('input',()=>{$('stepPreview').textContent=mlToSteps(val('ml'));});
applyMode(); loadConfig(); detect();
</script>
</body></html>
"""


@app.route("/")
def index():
    return PAGE


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
