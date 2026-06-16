#!/usr/bin/env bash
# =====================================================================
#  deploy_v2.sh — v2 (GREEN) ni ko'taradi va trafikni v2'ga o'tkazadi
# =====================================================================
#  Qadamlar:
#    1. v2 stack'ni build qilib ishga tushiradi (v1 hali ishlab turibdi)
#    2. v2 worker'lar HEALTHY bo'lguncha kutadi
#    3. Cutover: AstDB orqali yangi call'larni v2'ga yo'naltiradi
#
#  Eski call'lar UZILMAYDI — ular v1'da tugaguncha qoladi.
#  v1'ni o'chirish uchun keyin ./drain_v1.sh ishlatiladi.
# =====================================================================
set -euo pipefail

# Skript joylashgan papkadan repo root'ga o'tamiz (compose fayllar shu yerda)
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.yml -f blue-green/docker-compose.v2.yml"
V2_SERVICES="ari-gateway-v2 nginx-workers-v2 ai-worker-v2"
NEW_APP="ai-callcenter-v2"

echo "==> [1/3] v2 stack build & up ..."
$COMPOSE up -d --build $V2_SERVICES

echo "==> [2/3] v2 worker'lar healthy bo'lishini kutyapmiz ..."
# Compose healthcheck'iga tayanamiz: barcha ai-worker-v2 replica'lari
# 'healthy' bo'lmaguncha kutamiz (max ~3 daqiqa).
deadline=$(( $(date +%s) + 180 ))
while true; do
  # Har bir v2 worker konteynerining health holatini yig'amiz
  ids=$($COMPOSE ps -q ai-worker-v2)
  if [ -z "$ids" ]; then
    echo "    ... konteynerlar hali yaratilmadi, kutamiz"
  else
    total=0; healthy=0
    for id in $ids; do
      total=$((total+1))
      st=$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null || echo "none")
      [ "$st" = "healthy" ] && healthy=$((healthy+1))
    done
    echo "    ... healthy: $healthy/$total"
    [ "$total" -gt 0 ] && [ "$healthy" -eq "$total" ] && break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "!! v2 worker'lar vaqtida healthy bo'lmadi. Cutover BEKOR qilindi." >&2
    echo "   Loglarni tekshiring:  $COMPOSE logs ai-worker-v2" >&2
    exit 1
  fi
  sleep 5
done

echo "==> [3/3] Cutover: yangi call'lar -> $NEW_APP"
# AstDB'ga yozamiz. Shu lahzadan keyingi YANGI call'lar v2 gateway'ga boradi.
# Mavjud call'larga ta'sir qilmaydi.
$COMPOSE exec -T asterisk asterisk -rx "database put bluegreen stasis_app $NEW_APP"
echo "    AstDB holati:"
$COMPOSE exec -T asterisk asterisk -rx "database get bluegreen stasis_app"

cat <<EOF

✅ v2 LIVE. Yangi qo'ng'iroqlar endi v2'ga ketyapti.
   Eski (v1) call'lar tabiiy ravishda tugaydi.

Keyingi qadam — v1'ni xavfsiz o'chirish:
   ./blue-green/drain_v1.sh

Rollback (agar v2 da muammo bo'lsa):
   $COMPOSE exec asterisk asterisk -rx "database put bluegreen stasis_app ai-callcenter"
EOF
