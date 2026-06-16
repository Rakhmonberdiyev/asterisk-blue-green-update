from contextvars import ContextVar
import asyncio, json, re, time
from handover import trigger_handover
from .llm_utils import SKILL_GROUP_IDS, get_df, get_passport_variants, validate_passport, main, get_model_name, get_tools, initialize_mcp, client
from utils.utils import find_best_match, get_client_data




@main.tool()
def analyze_passport_digits(extracted_digits: str, session_buffer: str = "") -> dict:
    """Mijoz aytgan yangi raqamlarni buferga qo'shadi va holatni aniq hisoblab beradi."""
    # Faqat raqamlarni qoldiramiz
    clean_digits = "".join(filter(str.isdigit, extracted_digits))
    
    # Eski buferga yangi kelgan raqamlarni ulaymiz
    updated_buffer = session_buffer + clean_digits
    total_length = len(updated_buffer)
    
    if total_length == 7:
        return {
            "status": "COMPLETE",
            "buffer": updated_buffer,
            "readback": " ".join(list(updated_buffer)),
            "remaining": 0
        }
    elif total_length < 7:
        return {
            "status": "INCOMPLETE",
            "buffer": updated_buffer,
            "readback": " ".join(list(updated_buffer)),
            "remaining": 7 - total_length
        }
    else:
        return {
            "status": "OVERFLOW",
            "buffer": "",
            "message": "Raqamlar soni 7 tadan oshib ketdi."
        }



@main.tool(description="Ushbu tool foydalanuvchining goal_id, birthday, full_name hamda passport ma'lumotlaridan foydalangan holda mavjud ma'lumotlar bazasidan kun.oy.yil sifatida javob qaytaradi. which_date (MM_YYYY, masalan 06_2026) — qaysi oy uchun qidirish. Ko'rsatilmasa joriy oy ishlatiladi.")
def check_ihma_tool(goal_id: str, birthday: str, full_name: str, passport: str, which_date: str = "") -> str:
    print("Incoming arguments_passport: ", goal_id, birthday, full_name, "and", passport, "which_date:", which_date)
    t = time.time()

    valid_passport = validate_passport(passport)
    if valid_passport is None:
        return json.dumps({
            "match": False,
            "error": "INVALID_PASSPORT_FORMAT",
            "message": "Passport formati noto'g'ri. 2 ta harf va 7 ta raqam bo'lishi kerak."
        }, ensure_ascii=False)

    result = asyncio.run(get_client_data(goal_id, birthday, which_date or None))

    # API jadval topa olmasa: {"count":0,"data":[],"detail":"... jadvali topilmadi ..."}
    if result.get("data") is None or len(result["data"]) == 0:
        detail = result.get("detail", "")
        # Jadval umuman mavjud emas (o'sha davr sync qilinmagan) yoki shunchaki topilmadi
        return json.dumps({
            "found": False,
            "which_date": which_date,
            "detail": detail
        }, ensure_ascii=False)

    match = find_best_match(full_name, result["data"])
    print("Match is", match)

    if match is None:
        return json.dumps({"found": False, "which_date": which_date}, ensure_ascii=False)

    api_result = json.dumps([record for record, score in match], ensure_ascii=False)

    print("Valid passport: ", valid_passport)
    passport_variants = get_passport_variants(valid_passport)
    print(f"Checking passport variants: {passport_variants}", flush=True)

    try:
        records = json.loads(api_result)
        for record in records:
            if not isinstance(record, dict):
                continue
            record_passport = re.sub(r'[\s\-_]', '', record.get("passport", "")).upper()
            if record_passport in passport_variants:
                print(f"Matched record passport {record_passport} with variant of {valid_passport}", flush=True)
                card_last_number = record.get("card_last_number", "")
                pinfl = record.get("pinfl", "0")
                gender = "male" if pinfl[0] in ['1', '3', '5', '7', '9'] else "female" if pinfl[0] in ['0', '2', '4', '6', '8'] else "unknown"

                print("pinfl is", pinfl, "gender is", gender, flush=True)

                return json.dumps({
                    "match": True,
                    "PAYMENT_DATE": record.get("payment_date", ""),
                    "CLIENT_FULL_NAME": str(record.get("client_full_name", "")).lower(),
                    "AMOUNT": record.get("amount", ""),
                    "CARD_TYPE": record.get("card_type", ""),
                    "CARD_LAST_NUMBER": card_last_number[:2] + "-" + card_last_number[2:],
                    "BXM_CODE": record.get("bxm_code", ""),
                    "GENDER": gender
                }, ensure_ascii=False)

        return json.dumps({"match": False})

    except Exception as e:
        return json.dumps({"match": False, "error": str(e)})
    finally:
        print(f"Passport check completed in {time.time() - t:.2f} seconds", flush=True)


@main.tool(description="bxm_code bo'yicha filial manzilini qaytaradi. Faqat МПК (Baraka) kartali mijoz jismoniy kartani olmoqchi bo'lganda chaqiriladi.")
async def get_branch_by_bxm(bxm_code: int) -> str:
    df = get_df()
    df = df.dropna(subset=["name", "bxm_code"])
    result = df[df["bxm_code"] == bxm_code]
    print(f"Looking for BXM code {bxm_code}, Result: {result}", flush=True)
    return result


@main.tool(description=(
    "Mijozni boshqa bo'limga yo'naltiradi. "
    "ivr_number: '1'=Konsultativ, '2'=Bank amaliyotlari, "
    "'3'=Shikoyat, '4'=Texnik, '5'=Ijtimoiy/Baraka (default)."
))
async def operator_call(ivr_number: str = "5"):
    pass


@main.tool(description="Call this tool when user ready to end call or say it")
def end_call():
    return "End"


@main.tool(description="Use this tool to get current date in Tashkent timezone. (Your time zone is also this). No need to call it more than once per session, as the date won't change during the call.")
def current_date():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now().astimezone()
    tashkent_tz = ZoneInfo("Asia/Tashkent")
    if now.tzinfo == tashkent_tz:
        tashkent_time = now.date()
    else:
        tashkent_time = now.astimezone(tashkent_tz).date()
    return tashkent_time









@main.tool(description="Agar mijozning savoli: (Ijtimoiy baraka kartamning SVV kodini qanday va qayerdan olaman?) bo'lsa, ushbu tool chaqiriladi va mijozga SVV kodini qanday topish haqida ma'lumot beradi.")
def baraka_card_svv_info():    
    return "Ijtimoiy baraka kartangizning SVV kodini Ijtimoiy ximoya milliy agentliginig 'Baraka' mobil ilovasi orqali olishingiz mumkin."


@main.tool(description="Agar mijozning savoli: (Ijtimoiy baraka kartaga ariza berdim. Qachon tayyor bo‘ladi?) bo'lsa, ushbu tool chaqiriladi va karta tayyor bo‘lish jarayoni haqida ma'lumot beradi.")
def baraka_card_ready_time_info():
    return "Sizga Xalq bank tomonidan telefon raqamingizga kartangiz tayyor bo‘lishi bilan qachon va qayerdan olib ketishingiz mumkinligi to‘g‘risida SMS xabarnoma yuboriladi. Agar SMS xabarnoma kelmagan bo‘lsa, kutib turishingiz tavsiya etiladi."


@main.tool(description="Agar mijozning savoli: (Ijtimoiy baraka kartasi qaysi mobil ilovalarda ishlaydi?) bo'lsa, ushbu tool chaqiriladi va mos mobil ilovalar haqida ma'lumot beradi.")
def baraka_card_apps_info():
    return "Baraka kartangizni faqat Ijtimoiy ximoya milliy agentligining 'Baraka' mobil ilovasi va Xalq bankning 'Xazna' mobil ilovalari orqali foydalansangiz bo‘ladi."


@main.tool(description="Agar mijozning savoli: (Baraka kartamdagi mablag‘ni bankomatdan naqdlashtirishim mumkinmi va necha foiz bankomat xizmati olib qolinadi?) bo'lsa, ushbu tool chaqiriladi va naqdlashtirish shartlari haqida ma'lumot beradi.")
def baraka_card_cash_withdrawal_info():
    return "Bankomat orqali baraka kartangizni naqdlashtirishingiz mumkin va bankomatdan naqd pul yechishda 1% xizmat haqi olinadi."


@main.tool(description="Agar mijozning savoli: (Baraka kartamni yo‘qolib qoldi. Qanday qilib qayta tiklashim mumkin?) bo'lsa, ushbu tool chaqiriladi va kartani qayta tiklash haqida ma'lumot beradi.")
def baraka_card_reissue_info():
    return "Baraka kartangizni qayta tiklash uchun Ijtimoiy ximoya milliy agentligining 'Baraka' mobil ilovasi orqali yangi karta uchun ariza topshirishingiz mumkin."


@main.tool(description="Agar mijozning savoli: (Baraka kartamda pul mablag‘im bor edi lekin hozir nimaga balansim '0' bo‘lib qoldi?) bo'lsa, ushbu tool chaqiriladi va balans muammosi haqida ma'lumot beradi.")
def baraka_card_balance_issue_info():
    return "Hozirda texnik ishlar olib borilayotganligi sababli kartangiz balansi vaqtincha '0' ko‘rinmoqda. Tez orada muammo bartaraf etilib, balansingiz tiklanadi. Biroz kutib turishingiz tavsiya etiladi."


@main.tool(description="Agar mijozning savoli: (Baraka kartamni vaqtinchalik qanday va qayerdan bloklatib qo‘ysam bo‘ladi?) bo'lsa, ushbu tool chaqiriladi va bloklash jarayoni haqida ma'lumot beradi.")
def baraka_card_block_info():
    return "Baraka kartangizni vaqtinchalik Xalq bankning 'Xazna' ilovasi yoki 'Baraka' ilovasi orqali bloklashingiz mumkin. Kartani qayta aktivlashtirish uchun esa karta ochilgan Xalq bank filialiga murojaat qilishingiz kerak."


@main.tool(description="Agar mijozning savoli: (Baraka kartamni SMS xabarnomaga qanday ulasam yoki ulangan telefon raqamni o‘zgartirsam bo‘ladi?) bo'lsa, ushbu tool chaqiriladi va SMS xabarnoma ulash haqida ma'lumot beradi.")
def baraka_card_sms_info():
    return "Baraka kartangizni SMS xabarnomaga ulash yoki telefon raqamni o‘zgartirish uchun kartani olgan Xalq bank filialiga murojaat qilishingiz kerak."


@main.tool(description="Agar mijozning savoli: (Baraka kartamni 'Baraka' mobil ilovasi orqali bloklagandim, kartamni blokdan ocholmadim, qanday qilib blokdan ochtirsam bo‘ladi?) bo'lsa, ushbu tool chaqiriladi va blokdan chiqarish haqida ma'lumot beradi.")
def baraka_card_unblock_info():
    return "Kartangizni blokdan chiqarish uchun 1070 qisqa raqami orqali Ijtimoiy himoya milliy agentligiga murojaat qilishingiz kerak."


@main.tool(description="Agar mijozning savoli: (Baraka kartam visa tizimida ishlaydi ekan. Chet davlatlarda foydalansam bo‘ladimi?) bo'lsa, ushbu tool chaqiriladi va xalqaro foydalanish haqida ma'lumot beradi.")
def baraka_card_international_usage_info():
    return "Baraka kartangiz Visa tizimida ishlaydi va uni chet davlatlarda ham bemalol ishlatishingiz mumkin."


@main.tool(description="Agar mijozning savoli: (Baraka kartamning kirim-chiqimini qanday olsam bo‘ladi?) bo'lsa, ushbu tool chaqiriladi va hisoboti haqida ma'lumot beradi.")
def baraka_card_statement_info():
    return "Kartangizning kirim-chiqim ma'lumotlarini 'Xazna' ilovasi orqali (PDF yoki Excel shaklida) yoki karta berilgan bank filialidan olishingiz mumkin."


@main.tool(description="Agar mijozning savoli: (Baraka kartamni PIN-kodni 3 marotaba xato terib yubordim, qanday ochtirsam bo‘ladi?) bo'lsa, ushbu tool chaqiriladi va PIN blokdan chiqarish haqida ma'lumot beradi.")
def baraka_card_pin_block_info():
    return "Kartangiz PIN kodi bloklangan bo‘lsa, uni ochish uchun karta ochilgan Xalq bank filialiga murojaat qilishingiz kerak."


@main.tool(description="Agar mijozning savoli: (Baraka kartamni jamoat transportlarida ya'ni avtobus va metrolarda NFC orqali foydalansam bo‘ladimi?) bo'lsa, ushbu tool chaqiriladi va NFC foydalanish haqida ma'lumot beradi.")
def baraka_card_nfc_info():
    return "Baraka kartangizdan avtobus va metrolarda NFC orqali foydalanishingiz mumkin."


@main.tool(description="Agar mijozning savoli: (Baraka kartamga bankomatlar orqali kirim qilsam bo‘ladimi?) bo'lsa, ushbu tool chaqiriladi va kirim imkoniyati haqida ma'lumot beradi.")
def baraka_card_atm_deposit_info():
    return "Baraka kartaga bankomatlar orqali mablag‘ kiritib bo‘lmaydi."


@main.tool(description="Agar mijozning savoli: (Baraka kartamdan kafilligim yoki o‘zimning kredit qarzdorligim uchun pul yechib olinadimi?) bo'lsa, ushbu tool chaqiriladi va mablag‘ yechib olinishi haqida ma'lumot beradi.")
def baraka_card_debt_protection_info():
    return "Kafilligingiz yoki o‘zingizning kredit qarzdorligingiz uchun baraka kartangizdan mablag‘ yechib olinmaydi."


@main.tool(description="Agar mijozning savoli: (Men hozir boshqa viloyatdaman, baraka kartamni PIN kodini qanday o‘zgartirib olsam bo‘ladi?) bo'lsa, ushbu tool chaqiriladi va PIN kodni o‘zgartirish haqida ma'lumot beradi.")
def baraka_card_pin_change_info():
    return "Baraka kartangiz PIN kodini o‘zgartirish uchun kartani qayerdan olgan bo‘lsangiz, o‘sha bank filialiga murojaat qilishingiz kerak."





@main.tool(description="If user say hi to sherlock, call this tool")
def hi_sherlock():
    return "Sherlock is busy solving mysteries, but he will answer you as soon as possible!"