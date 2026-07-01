#!/usr/bin/env bash
# =============================================================================
# 원클릭 설치기 — 라즈베리파이에서 이 한 줄이면 다운로드+설치+부팅자동시작+실행.
#
#   curl -fsSL https://raw.githubusercontent.com/joomidang-tech/test-hardware-tool/main/bootstrap.sh | bash
#
# 하는 일:
#   1) 레포 다운로드(clone/pull, public 이라 자격증명 불필요)
#   2) 가상환경(.venv) + flask·pyserial 설치
#   3) 시리얼 권한(dialout) 부여
#   4) systemd 서비스 등록 → 부팅 때마다 자동 시작 (죽으면 자동 재시작)
#   5) 지금 바로 시작
#
# 옵션:  ... | bash -s -- --no-boot   (부팅 자동시작 없이 1회 설치만)
# =============================================================================
set -euo pipefail

REPO_URL="https://github.com/joomidang-tech/test-hardware-tool.git"
DIR="$HOME/test-hardware-tool"
SVC_NAME="test-hardware-tool"
NO_BOOT=0
[ "${1:-}" = "--no-boot" ] && NO_BOOT=1

echo "▶ 시린지펌프 테스트 툴 — 원클릭 설치"

# 0) 필수 패키지
if ! command -v git >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  echo "▶ git·python3 설치..."
  sudo apt-get update -qq && sudo apt-get install -y -qq git python3 python3-venv python3-pip
fi

# 1) 다운로드 (public → 인증 불필요)
if [ -d "$DIR/.git" ]; then
  echo "▶ 기존 설치 최신화 (git pull)..."
  git -C "$DIR" pull --ff-only
else
  echo "▶ 다운로드 (git clone)..."
  git clone --depth 1 "$REPO_URL" "$DIR"
fi
cd "$DIR"

# 2) venv + 의존성
echo "▶ 가상환경·의존성 설치..."
python3 -m venv .venv 2>/dev/null || { sudo apt-get install -y -qq python3-venv; python3 -m venv .venv; }
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q flask pyserial

# 3) 시리얼 권한
if ! id -nG "$USER" 2>/dev/null | grep -qw dialout; then
  echo "▶ 시리얼 권한(dialout) 부여 — 다음 부팅부터 적용..."
  sudo usermod -a -G dialout "$USER" || true
fi

# 4) systemd 서비스 (부팅 자동시작)
if [ "$NO_BOOT" -eq 0 ]; then
  echo "▶ 부팅 자동시작 서비스 등록..."
  sudo tee "/etc/systemd/system/${SVC_NAME}.service" >/dev/null <<EOF
[Unit]
Description=시린지펌프 하드웨어 테스트 툴
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now "${SVC_NAME}.service"
  STATE="$(systemctl is-active ${SVC_NAME}.service 2>/dev/null || echo unknown)"
else
  echo "▶ (--no-boot) 부팅 등록 생략, 지금 1회 실행..."
fi

# 5) 접속 안내
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
echo "======================================================"
echo "  ✅ 설치 완료!"
[ "$NO_BOOT" -eq 0 ] && echo "  · 부팅 시 자동 시작됨 (현재 상태: ${STATE:-실행})"
echo "  · 접속:  http://localhost:8000"
[ -n "${IP:-}" ] && echo "           http://$IP:8000  (같은 네트워크)"
echo "  · 로그:  sudo journalctl -u ${SVC_NAME} -f"
echo "  · 중지:  sudo systemctl stop ${SVC_NAME}"
echo "  · 제거:  sudo systemctl disable --now ${SVC_NAME}"
echo "======================================================"

# --no-boot 이면 포그라운드로 1회 실행
[ "$NO_BOOT" -eq 1 ] && exec ./.venv/bin/python app.py
