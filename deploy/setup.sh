#!/usr/bin/env bash
# EC2(Ubuntu)에서 한 번 실행: uv 설치 → 의존성 설치 → systemd 서비스 → nginx 연결
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

uv sync --frozen --no-dev
uv run --frozen --no-dev python -c "import app.main; print('import OK')"

sudo apt-get update -y
sudo apt-get install -y nginx

sudo cp deploy/daytrade-lab.service /etc/systemd/system/daytrade-lab.service
sudo systemctl daemon-reload
sudo systemctl enable --now daytrade-lab
sudo systemctl restart daytrade-lab

sudo cp deploy/nginx.conf /etc/nginx/sites-available/daytrade-lab
sudo ln -sf /etc/nginx/sites-available/daytrade-lab /etc/nginx/sites-enabled/daytrade-lab
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

sleep 2
curl -fsS http://127.0.0.1/api/health && echo && echo "배포 완료: http://$(curl -fsS https://checkip.amazonaws.com)/"
