#!/usr/bin/env bash
# =============================================================================
# 시린지펌프 테스트 툴 — 라즈베리파이(4B / 64bit) 원샷 설치+실행
#
#   chmod +x run.sh
#   ./run.sh
#
# 하는 일: (1) 파이썬 가상환경(venv) 생성  (2) flask·pyserial 자동 설치
#          (3) 시리얼 권한 확인  (4) 접속 주소 안내  (5) 앱 실행
# 최신 Pi OS(Bookworm)의 시스템 pip 차단(PEP 668)을 venv로 우회한다.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo "▶ 시린지펌프 테스트 툴 — Pi 설치/실행"

# 1) python3 확인
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ python3 가 없습니다:  sudo apt-get update && sudo apt-get install -y python3 python3-venv"
  exit 1
fi

# 2) venv 생성 (PEP 668 대응)
if [ ! -d ".venv" ]; then
  echo "▶ 가상환경 생성 중 (.venv)..."
  if ! python3 -m venv .venv 2>/dev/null; then
    echo "❌ venv 생성 실패 — 패키지 설치 후 다시 실행:  sudo apt-get install -y python3-venv"
    exit 1
  fi
fi
PY="$HERE/.venv/bin/python"
PIP="$HERE/.venv/bin/pip"

# 3) 의존성 설치 (이미 있으면 건너뜀)
if ! "$PY" -c "import flask, serial" >/dev/null 2>&1; then
  echo "▶ flask, pyserial 설치 중..."
  "$PIP" install --quiet --upgrade pip
  "$PIP" install --quiet flask pyserial
fi

# 4) 시리얼 권한 확인
if ! id -nG "$USER" 2>/dev/null | grep -qw dialout; then
  echo "⚠️  시리얼 권한(dialout) 없음 — 실제 펌프 제어하려면 아래 실행 후 재로그인:"
  echo "      sudo usermod -a -G dialout $USER"
fi

# 5) 접속 주소 안내
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
echo "======================================================"
echo "  브라우저에서 접속:"
echo "    · 이 Pi 화면:   http://localhost:8000"
[ -n "${IP:-}" ] && echo "    · 다른 기기:     http://$IP:8000"
echo "  종료: Ctrl+C"
echo "======================================================"
echo ""

# 6) 실행
exec "$PY" app.py
