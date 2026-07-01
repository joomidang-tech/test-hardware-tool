# 시린지펌프 USB 브라우저 테스트 (라즈베리파이 4B / 64bit)

"USB로 시린지펌프(SY-01B)를 구동할 수 있는가?"를 라즈베리파이 브라우저에서
버튼으로 바로 확인하는 최소 Python 툴. 프로토콜은 hey-senlyt **v1.0.0 앱의 기기 제어
로직**(`pump_service.dart`)을 그대로 이식했다.

## ⚠️ 먼저 이해할 것 — USB의 역할

| 항목 | USB로 되나? |
|------|:----:|
| 펌프에 **명령 전송**(회전/흡입/배출) | ✅ USB→FT232R(USB-시리얼)→펌프 |
| 펌프 **모터 구동 전원**(24V) | ❌ **별도 SMPS 필요** — USB로는 못 돌림 |

즉 USB는 "통신선"이다. 이 툴로 명령이 나가도 펌프에 **별도 전원**이 없으면
물리적으로 움직이지 않는다. 신호만 확인하려면 상태조회(`?`) 응답으로 통신 성립을 검증할 수 있다.

## 실행 — 원샷 (라즈베리파이 4B / 64bit)

이 `pump_web_test/` 폴더를 Pi로 옮긴 뒤(git clone · scp · USB 아무거나) **명령 하나**면 끝:

```bash
cd pump_web_test
chmod +x run.sh
./run.sh
```

`run.sh` 가 알아서: **가상환경(venv) 생성 → flask·pyserial 설치 → 시리얼 권한 확인 → 앱 실행**.
(최신 Pi OS Bookworm의 시스템 pip 차단(PEP 668)을 venv로 자동 우회 — 별도 설정 불필요.)

실행되면 브라우저에서 접속:
- **Pi 자체 화면**: http://localhost:8000
- **같은 네트워크 폰/노트북**: http://<라즈베리파이IP>:8000  (주소는 실행 시 콘솔에 표시됨)

> 순수 파이썬(Flask + pyserial)이라 ARM64에서 **빌드 없이 그대로** 동작한다. Pi 4B(2GB도) 충분.
> USB-시리얼 어댑터(CH340/FT232R)는 **자동 인식**하므로 포트를 손으로 고를 필요 없다.
> 처음 실행 시 시리얼 권한이 없으면 `sudo usermod -a -G dialout $USER` 후 재로그인.

### 외부 공개가 필요하면 (Cloudflare 임시 터널)

```bash
./run_with_tunnel.sh    # 앱 + https://*.trycloudflare.com 공개 URL 동시 실행
```
(cloudflared 필요 — 아래 "외부에서 접속" 참고. ⚠️ 인증 없음, 테스트용으로만.)

## 테스트 순서

1. **🔍 기기 자동 인식 & 연결** → 연결 직후 펌프 상태 자동 점검 표시
2. **제품 종류** 선택(음료/향수) → 나머지는 자동 분기
3. **기기설정**에서 용량·mL·속도 확인/조정
4. **초기화** → 원점 잡기 (펌프 24V 전원 있어야 물리 동작)
5. **① 밸브 → ② 흡입/배출** 순서로 구동 확인
6. 이상 시 **긴급정지(TR)**

로그 창에 송신(`>>>`)·수신(`<<<`) 원문과 상태(error_code, ready)가 표시된다.

## 외부에서 접속 (Cloudflare 임시 터널)

Pi 밖(휴대폰/다른 노트북)에서 접속하려면 Cloudflare 임시 터널을 쓴다. 계정·도메인 불필요.

```bash
# 1) cloudflared 설치 (라즈베리파이 64bit / ARM64, 한 번만)
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared-linux-arm64.deb

# 2) 앱 + 터널 동시 실행 → 콘솔에 공개 URL 출력
chmod +x run_with_tunnel.sh
./run_with_tunnel.sh
```

실행하면 `https://<랜덤단어>.trycloudflare.com` 형태의 URL이 콘솔에 찍힌다. 그 주소를 브라우저로 열면 끝. Ctrl+C 로 앱·터널 모두 종료된다.

> 수동으로 하려면: 터미널 A에서 `python3 app.py`, 터미널 B에서 `cloudflared tunnel --url http://localhost:8000`.

### ⚠️ 보안 경고 (반드시 읽을 것)

이 터널 URL은 **인증이 없다.** URL을 아는 누구나 브라우저로 들어와 **펌프를 물리적으로 구동**할 수 있다.
- **임시 브링업 테스트에만** 쓰고, 끝나면 즉시 Ctrl+C 로 터널을 닫는다.
- URL을 공개 채널(공유 문서·오픈 채팅)에 남기지 않는다.
- 상시 노출이 필요하면 앱에 토큰 인증을 추가하거나(요청 시 넣어줌), Cloudflare Access로 보호한다.
- 이 도구는 **진단 전용** — 운영 데몬(senlytd)에는 포함하지 않는다(infra §2.1).

## 응답 해석 (상태 바이트)

응답 `/0<상태>...` 에서 상태 바이트 기준:
- `error_code = byte & 0x0F` → `0` 정상, `15` Busy(작업중), 그 외 하드웨어 에러
- `ready = byte & 0x20` → `true` 면 대기/완료

## 명령 요약 (Raw 입력용)

| 명령 | 의미 |
|------|------|
| `?` | 상태 조회 |
| `U200,5R` | 스톨 전류 설정 |
| `ZR` / `Z1R` | 원점 복귀(Full / Half force) |
| `I{1~12}R` | 밸브를 해당 포트로 회전 |
| `P{steps}R` | 흡입 (24000 steps = 1mL) |
| `D{steps}R` | 배출 |
| `A{steps}R` | 플런저 절대 위치 이동 |
| `v500V6000c500L14R` | 속도 설정(start/top/cutoff/slope) |
| `TR` | 정지(Terminate) |

> 원문 규격: `developer/hey_senlyt/v1.0.0/code/hey-senlyt/hey-senlyt-app/lib/core/hardware/pump_service.dart`
