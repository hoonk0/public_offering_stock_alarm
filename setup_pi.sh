#!/bin/bash
# 라즈베리파이 셋업 스크립트.
# 사용: cd ~/ipo_bot && bash setup_pi.sh
set -e

cd "$(dirname "$0")"

echo "=== 1) Python venv 생성 + 패키지 설치 ==="
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt --quiet
echo "  ✓ 패키지 설치 완료"

echo ""
echo "=== 2) .env 파일 확인 ==="
if [ ! -f .env ]; then
    echo "  ⚠️  .env 파일이 없습니다."
    echo "  다음 명령으로 만들고 토큰/챗ID를 채운 뒤 다시 실행하세요:"
    echo ""
    echo "    cp .env.example .env"
    echo "    nano .env"
    echo ""
    exit 1
fi
echo "  ✓ .env 존재"

echo ""
echo "=== 3) 동작 확인 (dry-run, 텔레그램 발송 X) ==="
.venv/bin/python main.py --mode day1 --target-date 2026-05-06 --dry-run
echo "  ✓ 크롤링/등급 산정 정상"

echo ""
echo "=== 4) systemd 서비스 등록 ==="
sudo cp ipo-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ipo-bot
sudo systemctl restart ipo-bot
sleep 2
sudo systemctl status ipo-bot --no-pager
echo ""
echo "=== 완료! ==="
echo ""
echo "로그 보기:    sudo journalctl -u ipo-bot -f"
echo "재시작:       sudo systemctl restart ipo-bot"
echo "정지:         sudo systemctl stop ipo-bot"
echo "상태:         sudo systemctl status ipo-bot"
