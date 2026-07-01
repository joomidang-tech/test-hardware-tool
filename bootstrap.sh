#!/usr/bin/env bash
# =============================================================================
# 원샷 부트스트랩 — 라즈베리파이에서 이 한 파일이면 다운로드+설치+실행까지.
#
# 사용 (레포가 public 이면 자격증명 불필요):
#   curl -fsSL https://raw.githubusercontent.com/joomidang-tech/tool-hardware-test/main/bootstrap.sh | bash
#
# private 이면 Pi에 gh 로그인(gh auth login) 후:
#   gh repo clone joomidang-tech/tool-hardware-test && cd tool-hardware-test && ./bootstrap.sh
#
# 하는 일: 레포를 받아(clone) 최신화하고 run.sh(venv 설치+실행)를 띄운다.
# =============================================================================
set -euo pipefail
REPO="joomidang-tech/tool-hardware-test"
DIR="${1:-$HOME/tool-hardware-test}"

echo "▶ 부트스트랩 — $REPO → $DIR"

if [ -d "$DIR/.git" ]; then
  echo "▶ 기존 설치 최신화 (git pull)..."
  git -C "$DIR" pull --ff-only
else
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    gh repo clone "$REPO" "$DIR"
  else
    git clone "https://github.com/$REPO.git" "$DIR"   # public 이면 자격증명 없이 동작
  fi
fi

cd "$DIR"
chmod +x run.sh
exec ./run.sh
