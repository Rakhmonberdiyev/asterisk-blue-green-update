#!/usr/bin/env bash
# =====================================================================
#  status.sh — v1 (BLUE) va v2 (GREEN) holatini yonma-yon ko'rsatadi
# =====================================================================
#  Ko'rsatadi:
#    - Hozir qaysi versiya yangi call oladi (AstDB) + ARI'dagi faol app'lar
#    - Har versiya servislari holati (Up/healthy)
#    - Har worker'dagi active_sessions (drain kuzatuvi uchun)
#    - Asterisk'dagi jonli kanallar soni
# =====================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.yml -f blue-green/docker-compose.v2.yml"

line() { printf '%s\n' "----------------------------------------------------------"; }

# Bitta worker konteyneridan active_sessions ni o'qiydi
active_of() {
  docker exec "$1" python -c \
"import urllib.request,json;print(json.load(urllib.request.urlopen('http://localhost:8766/health',timeout=2))['active_sessions'])" \
    2>/dev/null || echo "?"
}

# Bir worker service uchun: har replica holati + active_sessions + jami
worker_block() {
  local svc="$1"
  local ids; ids=$($COMPOSE ps -q "$svc" 2>/dev/null)
  if [ -z "$ids" ]; then
    printf "  (ishlamayapti)\n"
    return
  fi
  local total=0
  for id in $ids; do
    local name health act
    name=$(docker inspect -f '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##')
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null)
    act=$(active_of "$id")
    printf "  %-45s %-10s sessions=%s\n" "$name" "$health" "$act"
    [[ "$act" =~ ^[0-9]+$ ]] && total=$((total+act))
  done
  printf "  %-45s %-10s JAMI=%s\n" "" "" "$total"
}

echo
line
echo " ROUTING — hozir yangi call qayerga ketyapti"
line
astdb=$($COMPOSE exec -T asterisk asterisk -rx "database get bluegreen stasis_app" 2>/dev/null | sed -n 's/.*: //p')
[ -z "$astdb" ] && astdb="(bo'sh → default ai-callcenter)"
echo "  AstDB stasis_app : $astdb"
echo "  ARI faol app'lar :"
$COMPOSE exec -T asterisk asterisk -rx "ari show apps" 2>/dev/null | sed '1d;/^$/d' | sed 's/^/    - /'
chans=$($COMPOSE exec -T asterisk asterisk -rx "core show channels count" 2>/dev/null | grep -i "active channel" || true)
echo "  Asterisk         : ${chans:-n/a}"

echo
line
echo " v1 (BLUE) — ai-callcenter"
line
echo " gateway:"; worker_block ari-gateway
echo " nginx:";   worker_block nginx-workers
echo " workers:"; worker_block ai-worker

echo
line
echo " v2 (GREEN) — ai-callcenter-v2"
line
echo " gateway:"; worker_block ari-gateway-v2
echo " nginx:";   worker_block nginx-workers-v2
echo " workers:"; worker_block ai-worker-v2
echo
