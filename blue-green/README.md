# Asterisk Call-Center — Blue-Green Deployment

v1 (blue) ishlab turganda v2 (green) ni deploy qilish, **active call'larni uzmasdan**.
v1 da active sessiya 0 ga tushganda v1 to'liq o'chiriladi.

## Nega bu setup oson blue-green'ga moslashadi

Eng muhim arxitektura fakti: **nginx bu yerda SIP/RTP'ni boshqarmaydi.** U faqat
*yangi sessiyani worker'ga biriktirish* (`/start-session`) uchun ishlatiladigan
ichki balancer. Call boshlanganidan keyin media oqimi **worker ↔ Asterisk
o'rtasida to'g'ridan WebSocket** orqali ketadi.

Oqim:

```
Qo'ng'iroq → Asterisk (SIP 5060 / RTP 10000-10100)
          → ari-gateway   (ARI WebSocket, Stasis app'ga obuna)
          → nginx-workers  (/start-session, least_conn)
          → ai-worker      (STT / LLM / TTS, media WS to'g'ridan Asterisk bilan)
```

Natija: agar nginx'dan yoki Stasis app'dan v1 ni "uzsak", **mavjud call'lar
uzilmaydi**, chunki ular allaqachon shu yo'naltirgichlarni o'tib bo'lgan va
to'g'ridan media oqimida.

## Ikkita mustaqil yo'naltirgich (har ikkalasi eski call'ni saqlaydi)

| Yo'naltirgich | v1 → v2 ga o'tish | Eski call'larga ta'siri |
|---|---|---|
| **Dialplan / AstDB** (qaysi gateway yangi call oladi) | `database put bluegreen stasis_app ai-callcenter-v2` | Yo'q — `Stasis()` ni o'tgan call v1'da tugaydi |
| **nginx-workers** (qaysi worker sessiya oladi) | v2 ning o'z nginx'i bor; cutover dialplan orqali | Yo'q — har versiyaning o'z stack'i |

`worker_server.py` allaqachon `/health` da `active_sessions` qaytaradi — drain'ni
shu bilan kuzatamiz, qo'shimcha kod kerak emas.

## Shared vs versiyalangan qatlamlar

- **Shared (o'zgarmaydi):** `asterisk`, `postgres`, `redis`
- **v1 (blue):** `ari-gateway` (app=`ai-callcenter`), `nginx-workers`, `ai-worker`
- **v2 (green):** `ari-gateway-v2` (app=`ai-callcenter-v2`), `nginx-workers-v2`, `ai-worker-v2`

> ⚠️ Nega alohida Stasis app nomi muhim: agar v1 va v2 gateway bir xil app nomiga
> ulansa, Asterisk har call event'ni **ikkala** gateway'ga yuboradi → double
> bridge, double worker. Shuning uchun v2 = `ai-callcenter-v2`.

## Bir martalik tayyorgarlik (faqat birinchi safar)

1. **Dialplan'ni AstDB'dan o'qiydigan qiling.** `extensions.conf.bluegreen` ni
   asterisk config'iga qo'llang (`Stasis(${STASIS_APP}, incoming)`). Configlar
   image ichiga baked bo'lsa, `ast_configs/` ni asterisk servisiga volume qilib
   mount qiling yoki image'ni qayta build qiling. Default qiymat `ai-callcenter`
   (v1), shuning uchun AstDB bo'sh bo'lsa ham hammasi avvalgidek ishlaydi.

2. AstDB boshlang'ich holatini o'rnating (ixtiyoriy, default baribir v1):
   ```bash
   docker compose exec asterisk asterisk -rx "database put bluegreen stasis_app ai-callcenter"
   ```

## Deploy jarayoni

```bash
# 1) v2 ni ko'tar va trafikni v2'ga o'tkaz (v1 hali ishlaydi)
./blue-green/deploy_v2.sh

# 2) v1 bo'shaguncha kut va o'chir (active_sessions 0 bo'lganda)
./blue-green/drain_v1.sh
```

`deploy_v2.sh`: v2 stack'ni build qiladi → worker'lar `healthy` bo'lguncha kutadi
→ AstDB orqali cutover qiladi. Worker'lar healthy bo'lmasa, cutover **bekor**
qilinadi (xavfsizlik).

`drain_v1.sh`: har v1 worker'ning `active_sessions` ni polling qiladi; yig'indi
ketma-ket 3 marta 0 bo'lganda v1 stack'ni `stop` + `rm` qiladi. Shared va v2
tegilmaydi.

## Rollback

v2 da muammo sezsangiz, drain'dan **oldin**, bitta buyruq bilan v1'ga qaytasiz:

```bash
docker compose exec asterisk asterisk -rx "database put bluegreen stasis_app ai-callcenter"
```

Yangi call'lar darhol yana v1'ga ketadi. (v1 hali o'chmagani uchun ishlaydi —
shuning uchun `drain_v1.sh` ni faqat v2 ishonchli ekaniga amin bo'lgach ishga
tushiring.)

## Tekshirish buyruqlari

```bash
# Hozir qaysi versiya yangi call oladi
docker compose exec asterisk asterisk -rx "database get bluegreen stasis_app"

# v1 da qancha active call qolgan (har worker)
for c in $(docker compose ps -q ai-worker); do
  docker exec "$c" python -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://localhost:8766/health'))['active_sessions'])"
done

# Asterisk darajasida tirik kanallar
docker compose exec asterisk asterisk -rx "core show channels"
```

## Fayllar

| Fayl | Vazifasi |
|---|---|
| `docker-compose.v2.yml` | v2 parallel stack (gateway-v2, nginx-v2, worker-v2) |
| `nginx.v2.conf` | v2 worker balancer config |
| `extensions.conf.bluegreen` | AstDB orqali app tanlaydigan dialplan (bir martalik) |
| `deploy_v2.sh` | v2 deploy + cutover |
| `drain_v1.sh` | v1 drain + teardown |
