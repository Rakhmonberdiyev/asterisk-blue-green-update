#!/usr/bin/env bash
# =====================================================================
#  drain_v1.sh — v1 (BLUE) ni xavfsiz o'chiradi
# =====================================================================
#  Mantiq:
#    - Cutover bo'lgan, yangi call'lar v2'ga ketyapti.
#    - v1 worker'larida hali eski active call'lar bo'lishi mumkin.
#    - Har bir v1 worker'ning /health -> active_sessions ni o'qiymiz.
#    - YIG'INDI 0 ga teng bo'lib, BARQAROR turganda (bir necha marta
#      ketma-ket 0), v1 stack'ni o'chiramiz.
#
#  Hech qanday call majburan uzilmaydi — faqat to'liq bo'shaganda
#  o'chadi.
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.yml -f blue-green/docker-compose.v2.yml"
V1_WORKER="ai-worker"                 # v1 worker service nomi (asosiy compose)
V1_SERVICES="ari-gateway nginx-workers ai-worker"
POLL_INTERVAL=10                      # sekund
STABLE_CHECKS=3                       # ketma-ket necha marta 0 bo'lsa o'chiramiz

# Bitta konteyner ichidan active_sessions ni o'qiydigan yordamchi.
# worker /health JSON qaytaradi: {"active_sessions": N, ...}
read_active() {
  local cid="$1"
  docker exec "$cid" python -c \
"import urllib.request,json;print(json.load(urllib.request.urlopen('http://localhost:8766/health',timeout=2))['active_sessions'])" \
    2>/dev/null || echo "ERR"
}

echo "==> v1 drain boshlandi. active_sessions 0 bo'lguncha kutamiz ..."
zero_streak=0
while true; do
  ids=$($COMPOSE ps -q "$V1_WORKER" || true)
  if [ -z "$ids" ]; then
    echo "    v1 worker konteyner topilmadi — allaqachon o'chgan ko'rinadi."
    break
  fi

  total=0; err=0
  for id in $ids; do
    v=$(read_active "$id")
    if [ "$v" = "ERR" ]; then
      err=$((err+1))           # o'qib bo'lmadi — ehtiyot uchun 0 deb hisoblamaymiz
    else
      total=$((total + v))
    fi
  done

  ts=$(date '+%H:%M:%S')
  echo "    [$ts] v1 active_sessions yig'indisi: $total (o'qilmagan: $err)"

  # Faqat hammasi o'qildi VA yig'indi 0 bo'lsa, streak oshiramiz
  if [ "$err" -eq 0 ] && [ "$total" -eq 0 ]; then
    zero_streak=$((zero_streak+1))
    echo "      -> 0 streak: $zero_streak/$STABLE_CHECKS"
    [ "$zero_streak" -ge "$STABLE_CHECKS" ] && break
  else
    zero_streak=0
  fi
  sleep "$POLL_INTERVAL"
done

echo "==> v1 bo'sh. Stack'ni o'chiramiz ..."
# stop + rm faqat v1 servislari. Shared (asterisk/postgres/redis) va v2 tegilmaydi.
$COMPOSE stop $V1_SERVICES
$COMPOSE rm -f $V1_SERVICES

cat <<EOF

✅ v1 o'chirildi. Endi faqat v2 ishlamoqda.

Tekshirish:
   $COMPOSE ps
   $COMPOSE exec asterisk asterisk -rx "database get bluegreen stasis_app"   # -> ai-callcenter-v2
EOF
