#!/usr/bin/env bash
# 本機開發服務的啟停（不需要 Docker）。
#
# 這台機器沒有 Docker，所以用 Homebrew 裝的 postgres@16 與 minio 直接跑，
# 提供的服務、連接埠、帳密都跟 docker/docker-compose.yml 一模一樣：
#   postgres  localhost:5433  postgres/postgres  資料庫 cxr
#   minio     localhost:9000（console 9001）minioadmin/minioadmin
#
# 有 Docker 的機器直接用 `docker compose -f docker/docker-compose.yml up -d`，
# 不需要這個腳本。
set -euo pipefail

export PATH="/usr/local/opt/postgresql@16/bin:/opt/homebrew/opt/postgresql@16/bin:$PATH"
export LC_ALL=C          # 少了這行 postgres 會在 macOS 上 "became multithreaded during startup"

DATA_DIR="${CXR_DEV_DIR:-$HOME/.axir-cxr-dev}"
PGDATA="$DATA_DIR/pgdata"
MINIO_DIR="$DATA_DIR/minio"

start() {
  mkdir -p "$PGDATA" "$MINIO_DIR"
  if [ ! -f "$PGDATA/PG_VERSION" ]; then
    echo "初始化 postgres…"
    initdb -D "$PGDATA" -U postgres --auth=trust -E UTF8 >/dev/null
  fi
  if ! pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
    pg_ctl -D "$PGDATA" -o "-p 5433 -k /tmp" -l "$DATA_DIR/pg.log" start
    sleep 2
  else
    echo "postgres 已在執行"
  fi
  psql -h localhost -p 5433 -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='cxr'" postgres \
    | grep -q 1 || createdb -h localhost -p 5433 -U postgres cxr

  if ! curl -sf http://127.0.0.1:9000/minio/health/live >/dev/null; then
    nohup minio server "$MINIO_DIR" --address ":9000" --console-address ":9001" \
      > "$DATA_DIR/minio.log" 2>&1 &
    sleep 3
  else
    echo "minio 已在執行"
  fi
  echo "✓ postgres localhost:5433 · minio localhost:9000（console http://localhost:9001）"
}

stop() {
  pg_ctl -D "$PGDATA" stop 2>/dev/null || true
  pkill -f "minio server $MINIO_DIR" 2>/dev/null || true
  echo "✓ 已停止"
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  *) echo "用法: $0 {start|stop|restart}"; exit 1 ;;
esac
