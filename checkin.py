"""Check-in/checkout xabarini parse qilish va backend'ga yuborish.

3 layer:
1. Filter: advisory (qoidalar), strong dispatch (yangi load) — skip
2. Parse: regex (fast path) → Groq LLM (fallback)
3. Send: load ID ni cache/API orqali aniqlab, backend checkin-checkout endpoint'iga POST

Public funksiyalar:
- parse_checkin_checkout, parse_checkin_checkout_llm
- send_checkin_checkout
- process_checkin_checkout_text (yuqori darajadagi orchestrator)
- is_advisory_text, is_strong_dispatch, is_trailer_only_reference (filters)
- parse_time_to_iso, extract_timezone, extract_tz_token, is_valid_load_id (utilities)
- resolve_shared_region, build_time_fields (TZ birlashtirish + payload vaqtlari)
- extract_doc_numbers_from_text, build_checkin_checkout_text (reply enrichment)

Timezone modeli: abbreviatura = region (IANA zona), DST-aware; checkin/checkout
bitta umumiy regionda talqin qilinadi. Batafsil: "Timezone model" bo'limi quyida.
"""
import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import types
from config import (
    CHECKIN_CHECKOUT_URL,
    TRAILER_REPORT_SKIP,
    ssl_context,
)
from db import get_company_permissions, get_load_from_cache
from external import get_api_token, get_loads_from_api, invalidate_token
from groups import get_or_fetch_company_id
from messaging import send_error_to_group
from state import cerebras_client, groq_client
from stats import record_checkin_event

logger = logging.getLogger(__name__)


def _llm_create(llm, **kwargs):
    """asyncio.to_thread uchun sync LLM chaqiruv o'rovi (audit v3 #11).

    Overloaded `.create` ni to'g'ridan-to'g'ri to_thread'ga uzatish mypy'ni
    chalg'itadi, lambda esa loop-o'zgaruvchida ruff B023 beradi — oddiy funksiya
    ikkalasini ham chetlab o'tadi va bloklovchi chaqiruvni thread'ga chiqaradi.
    """
    return llm.chat.completions.create(**kwargs)


# ====== Timezone model ======
#
# TZ abbreviatura REGIONNI bildiradi (EST ham EDT ham = Eastern) — qish/yoz
# offsetni IANA zonaning o'zi hal qiladi. Bu backend TimeZoneMapper.cs va front
# formatDate.js (dayjs.tz) bilan AYNAN bir xil semantika: enum 0..3 → IANA zona,
# DST-aware. Fixed-offset jadval (est=-5...) displayda yozda +1h xato berardi.
_TZ_TOKEN_PATTERN = r'E[SD]T|C[SD]T|M[SD]T|P[SD]T|ET|CT|MT|PT'
_TZ_REGION = {
    "est": "America/New_York", "edt": "America/New_York", "et": "America/New_York",
    "cst": "America/Chicago", "cdt": "America/Chicago", "ct": "America/Chicago",
    "mst": "America/Denver", "mdt": "America/Denver", "mt": "America/Denver",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles", "pt": "America/Los_Angeles",
}
# Backend ETimeZone enum: Est=0, Cst=1, Mst=2, Pst=3
_REGION_ENUM = {"America/New_York": 0, "America/Chicago": 1, "America/Denver": 2, "America/Los_Angeles": 3}
# TZ hech qayerda ko'rsatilmagan bo'lsa — tarixiy default Pacific (enum 3).
DEFAULT_REGION = "America/Los_Angeles"


# ====== Regex patterns ======

# DIQQAT: AM/PM guruhi oldidagi \s* guruh ICHIDA turishi shart. Tashqarida bo'lsa
# u bo'shliqni yutadi va TZ guruhi (\s+ talab qiladi) hech qachon match bo'lmaydi —
# checkout oxirgi capture bo'lgani uchun backtrack ham bo'lmay, "15:33 CDT" dan
# CDT tashlab yuborilardi (prod +2/3h siljish bugining asosiy sababi).
_TIME_CAPTURE = r'(?:\d{1,2}[:.\-]\d{2}|\d{4})(?:\s*[AaPp]\.?\s*[Mm]\.?)?(?:\s+[A-Za-z]{2,4}\b)?'
# Load ID — kamida 3 ta raqam bo'lishi shart (alfanumerik prefiks/suffiks ruxsat: "L260504-01027")
_LOAD_ID = r'[A-Za-z0-9\-]*\d{3,}[A-Za-z0-9\-]*'

# Forward: BOL/POD #12345 ... check in ... check out
CHECKIN_CHECKOUT_PATTERN = re.compile(
    r'(BOL|POD)\b[^\n]{0,60}?\b(' + _LOAD_ID + r')\b'
    r'.*?check(?:ed|ing)?\s*[\-_]?\s*in[:\s]*(' + _TIME_CAPTURE + r')\s+'
    r'.*?check(?:ed|ing)?\s*[\-_]?\s*out[:\s]*(' + _TIME_CAPTURE + r')',
    re.IGNORECASE | re.DOTALL,
)

# Reverse: <load_id> ... BOL/POD ... check in ... check out
CHECKIN_CHECKOUT_PATTERN_REV = re.compile(
    r'\b(' + _LOAD_ID + r')\b\W{0,30}?(BOL|POD)\b'
    r'.*?check(?:ed|ing)?\s*[\-_]?\s*in[:\s]*(' + _TIME_CAPTURE + r')\s+'
    r'.*?check(?:ed|ing)?\s*[\-_]?\s*out[:\s]*(' + _TIME_CAPTURE + r')',
    re.IGNORECASE | re.DOTALL,
)

# Faqat driver harakati qilingan (verb) formalarni match qiladi.
CHECKIN_CHECKOUT_KEYWORDS = re.compile(
    r'check(?:ed|ing)?[\s\-_]*(?:in|out)'
    r'|deliver(?:ed|ing)'
    r'|delivery\s+(?:complet(?:e|ed)|done|finished)'
    r'|picked[\s\-]*up|picking[\s\-]*up'
    r'|pickup\s+(?:complet(?:e|ed)|done|finished)'
    r'|dropped[\s\-]*off|dropping[\s\-]*off'
    r'|drop\s*off\s+(?:complet(?:e|ed)|done|finished)'
    r'|loaded|unloaded'
    r'|arrived|departed'
    r'|completed\s+(?:pickup|delivery)'
    r'|load\s+(?:is\s+|has\s+been\s+)?finish(?:ed)?'
    r'|the\s+load\s+(?:is\s+|has\s+been\s+)?finish(?:ed)?'
    r'|load\s+(?:is\s+|has\s+been\s+)?complet(?:e|ed)'
    r'|finish(?:ed)?\s+(?:load|unload)ing',
    re.IGNORECASE,
)

DISPATCH_MESSAGE_PATTERN = re.compile(
    r'(?:rate\s*\$|stop\s*\d+[\.\:]?\s*(?:pickup|delivery|live\s*load)|'
    r'\d+\s*miles|\bshipper\b|\breceiver\b|customer\s*ref\s*#|hazmat|'
    r'stop\s*1\b)',
    re.IGNORECASE,
)

# Advisory iboralar — driver harakati EMAS, balki ko'rsatma/qoida/eslatma.
ADVISORY_CHECKIN_PATTERN = re.compile(
    r'\bwill\s+need\s+to\b'
    r'|\bbefore\s+(?:he|she|they|driver)\b'
    r'|\bbefore\s+arriv'
    r'|\bgetting\s+check(?:ed|ing)?[\s\-_]*in'
    r'|\bcheck(?:ed|ing)?[\s\-_]*in\s+with\s+(?:gs|guard|shipper|receiver|dispatch|the|you|him|her|them|me|us)\b'
    r'|\bcheck(?:ed|ing)?[\s\-_]*in\s+at\s+(?:the\s+)?(?:gs|guard|gate)\b'
    r"|\b(?:i'?ll|i\s+will|we'?ll|we\s+will|driver\s+will)\s+(?:\w+\s+)?check(?:ed|ing)?[\s\-_]*in\b"
    r'|\b\d+\s*minutes?\s+before\b'
    r'|\bto\s+avoid\s+delays\b'
    r'|\bif\s+not[\s\-]*\$?\s*\d+\s*(?:charge|\$)'
    r'|\bmust\s+(?:have|be|provide)',
    re.IGNORECASE,
)

# ====== Trailer report (drop/hook) filter ======
#
# Driver trailerni tashlab ketgani/olganini hisobot qilib yozadi. Bu LOAD check-in emas,
# lekin "picked up" kalit so'zi orqali LLM yo'lagiga tushib, LLM trailer raqamini load_id
# deb qaytarardi -> soxta "vaqtlarni kiriting" javobi + buzuq missing_time statistikasi.
#
# BITTA mexanizm — PROVENANCE: matndagi HAR BIR raqam trailer/seal/truck kabi non-load
# markerga (yoki sanaga) bog'langan, ya'ni matnda load raqami UMUMAN yo'q. Bu yagona
# yo'lning xavfsizlik dalili STRUKTURAVIY ("bu matnda load raqami yo'q"), shablon-taxmin
# emas. Satr-shakliga BOG'LIQ EMAS, shuning uchun ovozli transkript / bir-qatorli /
# tinish belgisiz variantlarda ham ishlaydi (voice handler ham shu funksiyaga keladi —
# handlers.py:1182).
#
# ATAYLAB YO'Q (v2 soddalashtirish): "shape" yo'li (trailer sarlavhasi + 2+ forma maydoni)
# va LLM'dan KEYINGI backstop. Ikkalasi ham shablon-taxmin edi va HAQIQIY check-in'larni
# JIMGINA yo'qotardi (javob yo'q, backend yo'q, statistika yo'q) — bu o'zi tuzatgan
# ko'rinadigan "nag"dan qat'iy yomonroq. ZARAR ASIMMETRIYASI qoidasi: shubhali holatda
# SKIP QILMAYMIZ.
#
# Filtr "check in/check out" yoki soat ko'rsatilgan matnni HECH QACHON ushlamaydi —
# haqiqiy check-in doim vaqt bilan keladi (hard constraint).

# Gorizontal bo'shliq: NBSP (\xa0, iOS klaviaturasi) ham kirsin — [ \t] uni o'tkazmaydi.
_H = r'[^\S\n]'

# Trailer sarlavhasi so'zlari (kiril variantlari ham — haydovchilar aralash yozadi).
# "tri" — haqiqiy prod xatosi ("trl" da l -> i, klaviaturada yonma-yon va ko'rinishi bir xil).
# "trail(er|or|r)?" — bitta o'zak butun oilani qamrab oladi: trail/trailr/trailer/trailor.
# ATAYLAB YO'Q: "tr" va "trk" — juda qisqa/noaniq, ular load raqamini "trailerniki" deb
# yuvib, haqiqiy check-in'ni sokin yo'qotishi mumkin (zarar asimmetriyasi).
_TRAILER_MARKER_WORDS = r'trl|trlr|tri|trail(?:er|or|r)?|unit|прицеп|трейлер'
# Load BO'LMAGAN, lekin trailer hisobotida uchraydigan raqam egalari.
_NON_LOAD_MARKER_WORDS = (
    _TRAILER_MARKER_WORDS
    + r'|seal|plomba|пломба|truck|tractor|chassis|container'
    + r'|vin|plate|tag|mileage|odometer|door|dock|bay|gate'
)
# Load raqami markerlari (reply-enrichment ham aynan shu shaklda qo'shadi).
_LOAD_MARKER_WORDS = r'bol|pod|load|order|trip|shipment|manifest|ref|reference'


def _bound_id_pattern(markers: str) -> re.Pattern:
    """`<marker> [no|#|:] <id>` — marker bilan BOG'LANGAN raqamni topadi (id = group 1).

    Id marker'dan keyin BEVOSITA turishi shart. Avvalgi ixtiyoriy `[A-Za-z]{1,4}\\s+`
    prefiksi oradagi ISTALGAN qisqa so'zni yutar edi ("unit ok 448812" -> haqiqiy load
    raqami "trailer'niki" deb belgilanardi), ya'ni load raqamini "xavfsiz" deb yuvardi.
    Endi faqat id'ning o'ziga yopishgan harflar ("VT700653") qabul qilinadi: "trl VT 700653"
    kabi bo'shliqli variant bog'lanmaydi -> raqam manbasi noma'lum -> SKIP QILINMAYDI
    (zarar asimmetriyasi bo'yicha xavfsiz tomon).
    """
    return re.compile(
        r'\b(?:' + markers + r')s?\b'
        r'(?:' + _H + r'*(?:number|num|no|nomer|id)\b\.?)?'
        + _H + r'*[:#.\-–—/]{0,2}' + _H + r'*'
        r'(' + _LOAD_ID + r')\b',
        re.IGNORECASE,
    )


TRAILER_BOUND_ID_PATTERN = _bound_id_pattern(_TRAILER_MARKER_WORDS)
NON_LOAD_BOUND_ID_PATTERN = _bound_id_pattern(_NON_LOAD_MARKER_WORDS)
LOAD_BOUND_ID_PATTERN = _bound_id_pattern(_LOAD_MARKER_WORDS)

# Matndagi HAR QANDAY load-id nomzodi (LLM ham aynan shu shakldagi tokenni tanlaydi).
# `_LOAD_ID` naqshini BUTUN matn bo'ylab finditer qilish KVADRATIK edi: `[A-Za-z0-9\-]*`
# uzun harf-oqimida ("xxxx...") har bir boshlanish nuqtasidan oqimni to'liq yutib, keyin
# orqaga qaytardi — 4 KB xabar 80 ms, 16 KB esa 1.3 s (bu funksiya HAR BIR xabarda ishlaydi).
# Endi avval `\d{3,}` o'zagi topiladi, so'ng chetlarga chiziqli kengaytiriladi: natija
# AYNAN o'sha maksimal span'lar, murakkablik O(n).
_ID_DIGIT_CORE_PATTERN = re.compile(r'\d{3,}')
_ID_BODY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")


def _id_candidate_spans(text: str) -> list[tuple[int, int]]:
    """Har bir "3+ raqamli" identifikatorning maksimal span'i (finditer(_LOAD_ID) ekvivalenti)."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _ID_DIGIT_CORE_PATTERN.finditer(text):
        if m.start() < pos:
            continue  # oldingi span bu o'zakni allaqachon yutgan (finditer kabi kesishmaydi)
        start, end = m.start(), m.end()
        while start > 0 and text[start - 1] in _ID_BODY_CHARS:
            start -= 1
        while end < len(text) and text[end] in _ID_BODY_CHARS:
            end += 1
        spans.append((start, end))
        pos = end
    return spans

# Sana bo'lagi — "Date: 07/25/2026" ichidagi "2026" ni "noma'lum raqam" deb sanamaslik uchun.
# Siqiq 8-raqamli variant (YYYYMMDD) oy/kun oralig'i bilan TEKSHIRILADI: avvalgi
# `(?:19|20)\d{6}` haqiqiy 8-xonali load raqamini ("20123456" -> oy 34) "sana" deb
# yuvib yuborardi va check-in jimgina yo'qolardi.
_DATE_SPAN_PATTERN = re.compile(
    r'\b\d{1,4}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?\b'
    r'|\b(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\b',
)

# Vaqt YORLIG'I: "in/out/at/arrived/departed/time...". "Date"/"Sana" ATAYLAB yo'q — aynan
# yorliq sana bilan soatni ajratadigan yagona ishonchli signal ("Date: 7.25" = sana,
# "out 15.45" = soat).
_TIME_LABEL = (
    r'in|out|at|time|arrive[ds]?|arriving|arrival|depart(?:ed|s|ure)?|left|'
    r'start(?:ed)?|finish(?:ed)?|eta|etd'
)
# Yorliqdan keyingi "yalang'och" soat: military (0900/1700, 0000-2359 oralig'ida) yoki
# nuqta/tire/vergul/"h" bilan yozilgani (15.45, 8.30, 8h30, 15,45). Yorliqsiz "7.25" —
# sana bo'lib qolaveradi (aynan yorliq ikkisini ajratadi).
_LABELLED_TIME = (
    r'\b(?:' + _TIME_LABEL + r')\b' + _H + r'*[:=\-–—]?' + _H + r'*'
    r'(?:(?:[01]?\d|2[0-3])[0-5]\d|\d{1,2}[.,h\-][0-5]\d)\b'
)

# VAQT SIGNALI — haqiqiy check-in'da DOIM bor (soat, AM/PM, military+TZ/hrs, yorliqli
# yalang'och soat yoki "check in/out" so'zi). Sof sana ("Date: 7/25", "7.25") vaqt
# hisoblanmaydi: yorliqsiz nuqtali ajratgich ataylab chiqarib tashlangan, aks holda
# trailer hisobotining sanasi filtrni qurolsizlantirardi.
CHECKIN_TIME_SIGNAL_PATTERN = re.compile(
    r'check(?:ed|ing)?[\s\-_]*(?:in|out)\b'
    r'|\b\d{1,2}:\d{2}\b'
    r'|\b\d{1,2}' + _H + r'*[AaPp]\.?' + _H + r'*[Mm]\b'
    r'|\b\d{3,4}' + _H + r'*(?:hrs?\b|hours\b|o\'?clock\b|(?:' + _TZ_TOKEN_PATTERN + r')\b)'
    r'|' + _LABELLED_TIME
    + r'|\bnoon\b|\bmidnight\b',
    re.IGNORECASE,
)

# --- KORROBORATSIYA (AND-shart) ---
# Yalang'och "<marker> <raqam> <fe'l>" ("Unit 448812 delivered", "Trl 448812 delivered")
# hisobot EMAS: u haqiqiy LOAD raqami bo'lishi mumkin va uni jimgina yo'qotib bo'lmaydi
# (o'lchandi: fix'gacha bunday xabar javob olardi, provenance esa uni sokin yutardi).
# Shuning uchun skip uchun trailer hisobotining kamida BITTA qo'shimcha belgisi talab
# qilinadi: "<fe'l> ... by <ISM>" / "Driver: <ISM>" aktori yoki forma maydoni yorlig'i.
# DIQQAT: bu shart faqat SKIP'ni KAMAYTIRADI — yangi "sokin yo'qotish" yo'lini ocholmaydi.
# Yalang'och "driver"/"by" IKKI NUQTA talab qiladi, aks holda oddiy nasr ("By the time I
# got to the dock they were closed") aktor deb hisoblanardi.
_TRAILER_ACTOR_PATTERN = re.compile(
    r'(?:picked|pick|dropped|drop|hooked|hook|swapped|swap|left|took|taken|returned|'
    r'delivered|empty|loaded|unloaded|bobtail)[^\n:]{0,24}?\bby\b'
    + _H + r'*:?' + _H + r'*[^\W\d_]'
    r'|\b(?:driver|by)\b' + _H + r'*:' + _H + r'*[^\W\d_]',
    re.IGNORECASE,
)
_TRAILER_FIELD_PATTERN = re.compile(
    r'(?:^|[|;•·/,])' + _H + r'*[>*\-–—#)\].]*' + _H + r'*'
    r'(?:location|loc|city|yard|place|address|addr|manzil|joy|адрес|место'
    r'|date|sana|дата|condi?ti?on|condtion|cond|holat(?:i)?|состояние'
    r'|seal|plomba|пломба|notes?|comments?|izoh|примечание)\b',
    re.IGNORECASE | re.MULTILINE,
)

# load_id da bo'lishi mumkin bo'lmagan "noise" tokenlar
_INVALID_LOAD_ID_TOKENS = {
    "none", "null", "n/a", "na", "for", "the", "this", "that",
    "load", "and", "or", "with", "from", "to", "is", "has", "been",
    "bol", "pod",
}


# ====== Pure utilities ======

def extract_tz_token(time_str) -> str | None:
    """Vaqt stringidan TZ abbreviaturasini (est/edt/et/ct/mt/pt...) olish.

    \\b chegaralari shart: aks holda "WESTERN" ichidagi "EST" ham match bo'lardi.
    """
    if not time_str or not isinstance(time_str, str):
        return None
    match = re.search(r'\b(' + _TZ_TOKEN_PATTERN + r')\b', time_str, re.IGNORECASE)
    return match.group(1).lower() if match else None


def parse_time_to_iso(time_str, default_region: str = DEFAULT_REGION) -> str | None:
    """Vaqt stringini driver-lokal vaqtdan HAQIQIY UTC ga o'girish (ISO 8601).

    Qo'llab-quvvatlaydi: "10:00 AM PST", "10.00 AM", "1455", "1000 AM", "14:30",
    "15:10 PM EST" (24h+meridiem), "12:00n" (noon), "04:50PM DPT" (noma'lum
    suffix strip). TZ abbreviatura regionga (IANA zona) map qilinadi — DST'ni
    zona o'zi hal qiladi. Stringda TZ bo'lmasa `default_region` ishlatiladi.

    Parse bo'lmasa None qaytaradi — yarim tun (00:00) fallback YO'Q: u prod'da
    jimgina buzuq check-in yozuvlarini yaratardi (Delay/DwellTime ham buzilardi).
    """
    if not time_str or not isinstance(time_str, str) or not time_str.strip():
        return None

    original = str(time_str)
    s = original.strip()

    # 1) TZ tokenni aniqlash (region sifatida) va strip
    token = extract_tz_token(s)
    region = _TZ_REGION[token] if token else default_region
    s = re.sub(r'\s*\b(' + _TZ_TOKEN_PATTERN + r')\b\s*', ' ', s, flags=re.IGNORECASE)

    # 2) "a.m." / "p.m." → "AM"/"PM"
    s = re.sub(r'([AaPp])\s*\.\s*([Mm])\s*\.?', r'\1\2', s)

    # 2b) Noon shorthand: "12:00n" / "12 noon" / "noon" → 12:00 PM
    s = re.sub(r'\b(12(?:[:.\-]\d{2})?)\s*n(?:oon)?\b\.?', r'\1 PM', s, flags=re.IGNORECASE)
    if s.strip().lower() == "noon":
        s = "12:00 PM"

    # 3) Nuqta/tire separator → colon
    s = re.sub(r'(\d{1,2})[.\-](\d{2})', r'\1:\2', s)

    # 4) AM/PM oldiga bo'sh joy
    s = re.sub(r'(\d)\s*([AaPp][Mm])\b', r'\1 \2', s)

    # 5) Military (4 raqam) + AM/PM
    s = re.sub(r'\b(\d{2})(\d{2})(\s+[AaPp][Mm])\b', r'\1:\2\3', s)

    # 6) Sof military (AM/PM yo'q)
    s = re.sub(r'\b(\d{2})(\d{2})\b(?!\s*[:AaPp])', r'\1:\2', s)

    # 7) Noma'lum 2-4 harfli trailing suffix strip
    s = re.sub(r'\s+(?!(?:AM|PM)\b)[A-Za-z]{2,4}\s*$', '', s, flags=re.IGNORECASE)

    # 8) Extra bo'sh joy va trailing punktuatsiyani tozalash
    s = re.sub(r'\s+', ' ', s).strip(" .,;:!?").upper()

    # 9) 24-soat + meridiem aralash ("15:10 PM") — %I formatlar hour>12 da yiqilib
    # yarim tun fallback'ka olib kelardi. 24-soat qiymatiga ishonib meridiem tashlanadi.
    m24 = re.match(r'^(\d{1,2}):(\d{2})\s*(?:AM|PM)$', s)
    if m24 and (int(m24.group(1)) > 12 or int(m24.group(1)) == 0):
        s = f"{m24.group(1)}:{m24.group(2)}"

    has_ampm = bool(re.search(r'[AP]M', s))
    formats = ("%I:%M %p", "%I:%M%p") if has_ampm else ("%H:%M", "%I:%M")

    parsed = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue

    if parsed is None:
        logger.warning("⚠️ parse_time_to_iso: parse bo'lmadi | original=%r | normalized=%r", original, s)
        return None

    # Sana ham REGION bo'yicha "bugun" bo'lishi kerak: UTC yarim tundan keyin
    # (masalan 19:00 PDT) UTC-sana ertaga o'tib ketadi, driver esa hali bugunda.
    zone = ZoneInfo(region)
    now_local = datetime.now(zone)
    local_dt = parsed.replace(year=now_local.year, month=now_local.month, day=now_local.day, tzinfo=zone)
    return local_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def extract_timezone(time_str) -> int:
    """Vaqt stringidan backend ETimeZone enum (0=Est 1=Cst 2=Mst 3=Pst). Default: 3."""
    token = extract_tz_token(time_str)
    return _REGION_ENUM[_TZ_REGION[token]] if token else 3


def resolve_shared_region(checkin, checkout) -> str | None:
    """Ikkala vaqt uchun UMUMIY region: checkin'dagi token, bo'lmasa checkout'dagi.

    Driverlar TZ'ni ko'pincha faqat bitta vaqtga yozadi — ikkinchisi ham o'sha
    zonada deb qabul qilinadi (alohida-alohida default'lash +2/3h siljish berardi).
    """
    token = extract_tz_token(checkin) or extract_tz_token(checkout)
    return _TZ_REGION[token] if token else None


def build_time_fields(checkin, checkout) -> tuple[str, str, int] | None:
    """(checkin_iso, checkout_iso, tz_enum) yoki None (birortasi parse bo'lmasa).

    Checkout checkin'dan oldin chiqsa — tun oralab o'tgan deb ertasi kunga suriladi
    (aks holda backend DwellTime manfiy bo'lardi).
    """
    region = resolve_shared_region(checkin, checkout) or DEFAULT_REGION
    checkin_iso = parse_time_to_iso(checkin, default_region=region)
    checkout_iso = parse_time_to_iso(checkout, default_region=region)
    if not checkin_iso or not checkout_iso:
        return None
    if checkout_iso < checkin_iso:  # ISO format bir xil — leksikografik = xronologik
        rolled = datetime.strptime(checkout_iso, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=UTC) + timedelta(days=1)
        checkout_iso = rolled.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return checkin_iso, checkout_iso, _REGION_ENUM[region]


def is_valid_load_id(s) -> bool:
    """Load ID qabul qilinishi uchun: bo'sh emas, noise token emas, kamida 3 raqam."""
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if not s:
        return False
    if s.lower() in _INVALID_LOAD_ID_TOKENS:
        return False
    if len(re.findall(r'\d', s)) < 3:
        return False
    return True


# ====== Parsers ======

def parse_checkin_checkout(text: str) -> dict | None:
    """BOL/POD raqami, checkin va checkout vaqtlarini regex orqali parse qiladi."""
    match = CHECKIN_CHECKOUT_PATTERN.search(text)
    if match:
        load_id = match.group(2)
        if is_valid_load_id(load_id):
            return {
                "doc_type": match.group(1).upper(),
                "load_id": load_id,
                "checkin": match.group(3).strip(),
                "checkout": match.group(4).strip(),
            }
    match = CHECKIN_CHECKOUT_PATTERN_REV.search(text)
    if match:
        load_id = match.group(1)
        if is_valid_load_id(load_id):
            return {
                "doc_type": match.group(2).upper(),
                "load_id": load_id,
                "checkin": match.group(3).strip(),
                "checkout": match.group(4).strip(),
            }
    return None


def extract_doc_numbers_from_text(text: str) -> str:
    """Matndan BOL/POD/Load raqamlarini prefix bilan chiqaradi.

    Forward ("BOL 12345") va reverse ("L260504-01027 BOL") formatlarni qabul qiladi.
    Prefix bo'lmasa, standalone alfanumerik IDlar "Load <n>" sifatida qaytariladi.
    """
    found = []
    seen = set()

    def add(doc_type: str, load_id: str):
        load_id = (load_id or "").strip()
        if not load_id:
            return
        if not is_valid_load_id(load_id):
            return
        key = (doc_type.upper(), load_id.lower())
        if key in seen:
            return
        seen.add(key)
        found.append(f"{doc_type} {load_id}")

    for m in re.finditer(
        r'\b(BOL|POD|Load\s*Id|Load)\s*[:#]?\s*([A-Za-z0-9\-]*\d{3,}[A-Za-z0-9\-]*)',
        text, re.IGNORECASE,
    ):
        prefix = re.sub(r'\s+', '', m.group(1)).upper()
        doc_type = "BOL" if prefix == "BOL" else "POD" if prefix == "POD" else "Load"
        add(doc_type, m.group(2))

    for m in re.finditer(
        r'\b([A-Za-z0-9\-]*\d{3,}[A-Za-z0-9\-]*)\W{0,5}?(BOL|POD)\b',
        text, re.IGNORECASE,
    ):
        add(m.group(2).upper(), m.group(1))

    if found:
        return " ".join(found)

    standalone = re.findall(r'\b([A-Za-z0-9\-]*\d{5,}[A-Za-z0-9\-]*)\b', text)
    standalone = [s for s in standalone if is_valid_load_id(s)]
    if standalone:
        return " ".join(f"Load {n}" for n in standalone[:3])
    return ""


def build_checkin_checkout_text(msg: types.Message, primary_text: str) -> str:
    """Current text + reply xabaridan BOL/POD/Load raqamlarini birlashtiradi.

    Reply'dan load number, asosiy matndan checkin/checkout matni — bu tartibda
    qo'shiladi, regex pattern (load→check in→check out) ishlashi uchun.
    """
    base_text = (primary_text or "").strip()
    parts = []

    if base_text and CHECKIN_CHECKOUT_KEYWORDS.search(base_text):
        reply = getattr(msg, "reply_to_message", None)
        if reply:
            reply_text = (getattr(reply, "text", None) or getattr(reply, "caption", None) or "").strip()
            if reply_text and reply_text != base_text:
                doc_numbers = extract_doc_numbers_from_text(reply_text)
                if doc_numbers:
                    parts.append(doc_numbers)

    if base_text:
        parts.append(base_text)

    return "\n".join(parts).strip()


async def parse_checkin_checkout_llm(text: str) -> dict | None:
    """Regex topalmasa Groq LLM orqali checkin/checkout parse qiladi."""
    try:
        prompt = f"""Extract check-in/check-out info from this message. Return ONLY valid JSON, nothing else.

Message: "{text}"

Return format:
{{"doc_type": "BOL" or "POD", "load_id": "load number", "checkin": "time with timezone", "checkout": "time with timezone"}}

Rules:
- doc_type must be BOL or POD (uppercase)
  * BOL = pickup stop (e.g. "picked up", "loaded", "pick up", "pickup completed", "successfully picked up")
  * POD = delivery stop (e.g. "delivered", "delivery completed", "successfully delivered", "unloaded", "dropped off", "load finished", "the load finished", "load is finished")
  * If the message explicitly says BOL or POD, use that directly
  * If neither keyword, infer from context: pickup-related = BOL, delivery-related = POD
- load_id is the load/BOL/POD/reference number
- checkin and checkout must include time and timezone if present (e.g. "10:00 AM PST")
- If timezone is missing, keep time as-is
- If any field is missing, return null

If this is NOT a check-in/check-out or pickup/delivery confirmation message, return: null"""

        # Provider fallback: Groq (asosiy) -> Cerebras (Groq ishlamay qolsa).
        # Cerebras'da gpt-oss reasoning modeli — ko'proq token + reasoning_effort=low kerak.
        groq_extra: dict[str, Any] = {"max_tokens": 150}
        providers = [("Groq", groq_client, "llama-3.3-70b-versatile", groq_extra)]
        if cerebras_client is not None:
            cb_extra: dict[str, Any] = {"max_tokens": 400, "extra_body": {"reasoning_effort": "low"}}
            providers.append(("Cerebras", cerebras_client, "gpt-oss-120b", cb_extra))

        content = None
        used = None
        last_err = None
        for name, llm, model, extra in providers:
            # 429 (rate-limit) bo'lsa o'sha provayderni qisqa kutib qayta urinamiz.
            for attempt in range(3):
                try:
                    res = await asyncio.to_thread(
                        _llm_create,
                        llm,
                        model=model,
                        messages=[
                            {"role": "system", "content": "You are a logistics data extractor. Return only valid JSON or null."},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.1,
                        **extra,
                    )
                    content = (res.choices[0].message.content or "").strip()
                    used = name
                    break
                except Exception as e:
                    last_err = e
                    if getattr(e, "status_code", None) == 429 and attempt < 2:
                        resp = getattr(e, "response", None)
                        ra = resp.headers.get("retry-after") if resp is not None else None
                        try:
                            delay = min(float(ra), 12.0) if ra else 2.0 * (attempt + 1)
                        except (TypeError, ValueError):
                            delay = 2.0 * (attempt + 1)
                        logger.warning("⚠️ %s rate-limit (429) — %.1fs kutib qayta urinilyapti", name, delay)
                        await asyncio.sleep(delay)
                        continue
                    logger.warning("⚠️ %s checkin parse xato, keyingi provayderga o'tilyapti: %s", name, e)
                    break
            if content is not None:
                break

        if content is None:
            await send_error_to_group(f"❌ Checkin/checkout LLM parse xato (barcha provayder): {last_err}")
            return None
        if content.lower() == "null" or not content:
            return None
        content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content, flags=re.MULTILINE).strip()
        data = json.loads(content)
        if not data or not isinstance(data, dict):
            return None
        if not all(k in data for k in ("doc_type", "load_id", "checkin", "checkout")):
            return None
        if data["doc_type"] not in ("BOL", "POD"):
            return None
        logger.info("🤖 %s parsed checkin/checkout: %s", used, data)
        return data
    except Exception as e:
        await send_error_to_group(f"❌ Checkin/checkout parse error: {e}")
        return None


# ====== Filters (advisory + dispatch) ======

def is_advisory_text(text: str) -> bool:
    """Driver harakati emas, balki ko'rsatma/qoida bo'lgan xabarlarni aniqlaydi."""
    return bool(ADVISORY_CHECKIN_PATTERN.search(text or ""))


def is_strong_dispatch(text: str) -> bool:
    """2+ ta dispatch markeri bo'lsa True — CHECK IN keyword'ni override qiladi."""
    if not text:
        return False
    signals = 0
    if re.search(r'\brate\s*[:=]?\s*\$\s*\d', text, re.IGNORECASE):
        signals += 1
    if re.search(r'\bmiles?\s*[:=]?\s*[\d,]{3,}\b', text, re.IGNORECASE):
        signals += 1
    if re.search(r'\bstop\s*\d+\s*[\.\:]?\s*(?:pickup|delivery|live\s*(?:load|unload))', text, re.IGNORECASE):
        signals += 1
    if re.search(r'\bcustomer\s*(?:ref|pickup|reference)\s*#', text, re.IGNORECASE):
        signals += 1
    if re.search(r'\$\s*\d+\s*charge\b|\d+%\s+(?:from|of)\s+load\s+rate\b', text, re.IGNORECASE):
        signals += 1
    if len(re.findall(r'\bstop\s*\d+\b', text, re.IGNORECASE)) >= 2:
        signals += 1
    return signals >= 2


def _norm_id(value) -> str:
    """Identifikatorni solishtirish uchun normallashtirish ("VT-700 653" -> "vt700653")."""
    return re.sub(r'[^a-z0-9]', '', str(value or "").lower())


def _trailer_bound_ids(text: str) -> set[str]:
    """Trailer/seal/truck kabi NON-LOAD markerga bog'langan raqamlar (normallashgan)."""
    return {_norm_id(m.group(1)) for m in NON_LOAD_BOUND_ID_PATTERN.finditer(text)}


def _has_foreign_load_ref(text: str) -> bool:
    """Trailer raqamidan BOSHQA, BOL/POD/Load markeriga bog'langan haqiqiy raqam bormi.

    Bo'lsa — matnda load raqami BOR, ya'ni provenance dalili yiqiladi va filtr tegmaydi.
    """
    trailer_ids = _trailer_bound_ids(text)
    for m in LOAD_BOUND_ID_PATTERN.finditer(text):
        ref = m.group(1)
        if is_valid_load_id(ref) and _norm_id(ref) not in trailer_ids:
            return True
    return False


def is_trailer_only_reference(text: str) -> bool:
    """Trailer hisobotimi: matndagi HAR BIR raqam trailer/seal/truck markeriga (yoki sanaga) bog'langan.

    Ya'ni matnda load raqami umuman yo'q — LLM baribir bittasini "topib" beradi.
    Satr shakliga bog'liq emas: ovozli transkript ("trailer 700653 picked up by doniyor")
    ham ushlanadi. Noma'lum manbali raqam (bare military time "0900", zip, telefon)
    uchrasa — filtr tegmaydi (konservativ).

    Bu — YAGONA skip mexanizmi. Uning dalili strukturaviy: load raqami yo'q ekan,
    check-in ham yo'q. Shubhali holatda False (skip qilmaymiz) tomonga og'adi.

    Provenance'ning O'ZI yetarli emas: "Unit 448812 delivered" da ham hamma raqam
    markerga bog'langan, lekin 448812 haqiqiy load raqami bo'lishi mumkin. Shuning uchun
    korroboratsiya (aktor yoki forma maydoni) MAJBURIY — pastdagi AND-shart.
    """
    if not text:
        return False
    if not TRAILER_BOUND_ID_PATTERN.search(text):
        return False
    if CHECKIN_TIME_SIGNAL_PATTERN.search(text):
        return False
    if _has_foreign_load_ref(text):
        return False
    if not (_TRAILER_ACTOR_PATTERN.search(text) or _TRAILER_FIELD_PATTERN.search(text)):
        return False
    safe_spans = [m.span(1) for m in NON_LOAD_BOUND_ID_PATTERN.finditer(text)]
    safe_spans += [m.span() for m in _DATE_SPAN_PATTERN.finditer(text)]
    for start, end in _id_candidate_spans(text):
        if not any(s <= start and end <= e for s, e in safe_spans):
            return False
    return True


# ====== Backend communication ======

async def resolve_load_id(group_id, load_number):
    """Load number ni load ID ga aylantirish. Avval cache, keyin API."""
    load_data = await get_load_from_cache(group_id, load_number)
    if load_data:
        # CHK-5: kesh load_id bo'yicha kalitlangan — bu yerda load_number keshda mavjud,
        # ya'ni u backend qabul qiladigan haqiqiy load_id. (API yo'li loadNumber→id hal qiladi.)
        return str(load_number)

    try:
        loads = await get_loads_from_api(str(group_id))
        for load in loads:
            load_num = str(load.get("loadNumber", ""))
            load_id = str(load.get("id") or load.get("loadId") or "")
            if load_num == str(load_number) or load_id == str(load_number):
                logger.debug("🔍 Load resolved: number=%s -> id=%s", load_number, load_id)
                return load_id
    except Exception as e:
        logger.warning("⚠️ Could not resolve load ID: %s", e)

    return str(load_number)


async def send_checkin_checkout(group_id, load_id, checkin, checkout, is_pickup):
    """Checkin/checkout vaqtlarini API ga yuborish. Returns {success, error, ...}."""
    logger.info("📋 CheckinCheckout | Group: %s | Load: %s | In: %s | Out: %s | isPickup: %s",
                group_id, load_id, checkin, checkout, is_pickup)

    time_fields = build_time_fields(checkin, checkout)
    if time_fields is None:
        # Yarim tun fallback bilan buzuq yozuv yaratmaymiz — driver'dan qayta so'raladi.
        logger.warning("⚠️ CheckinCheckout: vaqt parse bo'lmadi | in=%r | out=%r", checkin, checkout)
        return {"success": False, "invalid_time": True,
                "error": f"unparseable time (in={checkin!r}, out={checkout!r})"}
    checkin_iso, checkout_iso, tz_enum = time_fields

    resolved_id = await resolve_load_id(group_id, load_id)
    logger.debug("🔍 Resolved load ID: %s -> %s", load_id, resolved_id)

    token = await get_api_token()
    if not token:
        logger.error("❌ CheckinCheckout: API token olishda xato")
        return {"success": False, "error": "API token olishda xato"}

    payload = {
        "groupId": str(group_id),
        "loadId": resolved_id,
        "checkIn": checkin_iso,
        "checkOut": checkout_iso,
        "timeZone": tz_enum,
        "isPickup": is_pickup,
    }
    logger.debug("📤 CheckinCheckout payload: %s", json.dumps(payload))

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            headers = {"Authorization": f"Bearer {token}", "Accept-Language": "EN", "X-Group-Id": str(group_id)}
            async with session.post(CHECKIN_CHECKOUT_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status in (200, 201, 204):
                    logger.info("✅ CheckinCheckout sent for load %s", load_id)
                    return {"success": True}
                elif resp.status == 401:
                    logger.info("🔄 Token expired, refreshing...")
                    invalidate_token()
                    new_token = await get_api_token()
                    if new_token:
                        headers = {"Authorization": f"Bearer {new_token}", "Accept-Language": "EN", "X-Group-Id": str(group_id)}
                        async with session.post(CHECKIN_CHECKOUT_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as retry_resp:
                            if retry_resp.status in (200, 201, 204):
                                logger.info("✅ CheckinCheckout sent for load %s (after token refresh)", load_id)
                                return {"success": True}
                            text = await retry_resp.text()
                            await send_error_to_group(f"❌ CheckinCheckout failed after refresh: {retry_resp.status} - {text}", group_id=group_id)
                            parsed_error = None
                            try:
                                err_json = json.loads(text)
                                if isinstance(err_json.get("errors"), dict):
                                    parsed_error = (
                                        err_json["errors"].get("description")
                                        or err_json["errors"].get("message")
                                    )
                                parsed_error = parsed_error or err_json.get("description") or err_json.get("detail")
                            except (json.JSONDecodeError, AttributeError, TypeError):
                                logger.debug("CheckinCheckout error response not JSON", exc_info=True)
                            return {
                                "success": False,
                                "status": retry_resp.status,
                                "error": parsed_error or f"{retry_resp.status}: {text[:200]}",
                            }
                    return {"success": False, "error": "Token refresh failed"}
                else:
                    text = await resp.text()
                    await send_error_to_group(f"❌ CheckinCheckout failed: {resp.status} - {text}", group_id=group_id)
                    parsed_error = None
                    try:
                        err_json = json.loads(text)
                        if isinstance(err_json.get("errors"), dict):
                            parsed_error = (
                                err_json["errors"].get("description")
                                or err_json["errors"].get("message")
                            )
                        parsed_error = parsed_error or err_json.get("description") or err_json.get("detail")
                    except (json.JSONDecodeError, AttributeError, TypeError):
                        logger.debug("CheckinCheckout error response not JSON", exc_info=True)
                    return {
                        "success": False,
                        "status": resp.status,
                        "error": parsed_error or f"{resp.status}: {text[:200]}",
                    }
    except (TimeoutError, aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as e:
        # CHK-6: faqat haqiqiy transport xatosi server_down (transient). Boshqa exception'lar
        # (kod bug'lari) server_down deb yashirilmasin — real xato yo'lidan chiqsin.
        await send_error_to_group(f"❌ CheckinCheckout transport error: {e}", group_id=group_id)
        return {"success": False, "error": str(e)[:200], "server_down": True}
    except Exception as e:
        await send_error_to_group(f"❌ CheckinCheckout error: {e}", group_id=group_id)
        return {"success": False, "error": str(e)[:200]}


# ====== Top-level orchestrator ======

async def process_checkin_checkout_text(text: str, chat_id: int, msg) -> bool:
    """Text dan checkin/checkout parse qilib API ga yuborish. True = topildi."""
    logger.debug("🔍 process_checkin_checkout_text | chat=%s | text=%r", chat_id, text[:200])
    if is_advisory_text(text):
        logger.debug("🔍 Advisory text detected, skipping checkin/checkout")
        return False
    if is_strong_dispatch(text):
        logger.debug("🔍 Strong dispatch signals detected, skipping checkin/checkout")
        return False
    if TRAILER_REPORT_SKIP and is_trailer_only_reference(text):
        # Trailer almashinuvi hisoboti — javob ham, backend ham, statistika ham yo'q.
        # YAGONA skip nuqtasi: LLM'dan keyin boshqa "backstop" YO'Q (pastga qarang).
        logger.debug("🔍 Trailer report detected, skipping checkin/checkout")
        return False
    if DISPATCH_MESSAGE_PATTERN.search(text) and not CHECKIN_CHECKOUT_KEYWORDS.search(text):
        logger.debug("🔍 Dispatch message detected, skipping checkin/checkout")
        return False
    parsed = parse_checkin_checkout(text)
    regex_matched = parsed is not None
    logger.debug("🔍 regex parse result: %s", parsed)
    if not parsed and CHECKIN_CHECKOUT_KEYWORDS.search(text):
        parsed = await parse_checkin_checkout_llm(text)
        logger.debug("🔍 llm parse result: %s", parsed)
        # CHK-3: LLM raqamli (int) load_id qaytarsa is_valid_load_id uni rad etib check-in'ni
        # jimgina tashlab yubarardi — validatsiyadan oldin str'ga aylantiramiz.
        if parsed and parsed.get("load_id") is not None:
            parsed["load_id"] = str(parsed["load_id"]).strip()
        if parsed and not is_valid_load_id(parsed.get("load_id", "")):
            # CHK-A2: load_id yaroqsiz bo'lsa butun natijani tashlab yubormaymiz — faqat
            # load_id'ni tozalaymiz, shunda quyidagi 558-satr guard'i (check-in+check-out
            # bo'lsa) "load number kiriting" deb so'raydi, jimgina yo'qotmaydi.
            logger.debug("🔍 LLM returned invalid load_id=%r, clearing load_id", parsed.get('load_id'))
            parsed["load_id"] = ""
    if not parsed:
        logger.debug("🔍 No checkin/checkout found in text")
        return False

    # LLM'dan KEYINGI backstop ATAYLAB YO'Q. Avvalgi varianti LLM qaytargan load_id'ni
    # "trailer raqami" deb hisoblab xabarni jimgina yo'qotardi — va "<fe'l> ... by <ISM>"
    # yoki "Driver:" satri bo'lgan HAR QANDAY xabarda (matnda trailer so'zi umuman
    # bo'lmasa ham) ishlab ketardi: "Delivered 129201881, signed by John Smith" jimgina
    # yo'qolardi. Bu yerda skip qilmaslikning narxi — ko'rinadigan "vaqtlarni kiriting"
    # javobi; skip qilishning narxi — yo'qolgan check-in. Ikkinchisi qimmatroq.

    company_id = await get_or_fetch_company_id(chat_id)
    if company_id:
        perms = await get_company_permissions(str(company_id))
        if perms and not perms.get("checkInCheckOut", True):
            await record_checkin_event(
                group_id=chat_id, result="no_permission",
                load_id=parsed.get("load_id"), doc_type=parsed.get("doc_type"),
            )
            return True

    if not parsed.get("load_id"):
        if regex_matched or (parsed.get("checkin") and parsed.get("checkout")):
            await msg.answer("Please include the load number in your message.")
            return True
        logger.debug("🔍 LLM matched but checkin/checkout incomplete — treating as non-checkin message")
        return False

    event = {
        "group_id": chat_id, "load_id": parsed["load_id"], "doc_type": parsed["doc_type"],
        "checkin_raw": parsed.get("checkin"), "checkout_raw": parsed.get("checkout"),
    }

    if not parsed.get("checkin") or not parsed.get("checkout"):
        # Ikkala vaqtsiz yubormaymiz: backend yo'q vaqtni ham yozib stop'ni
        # IsCompleted qiladi, Delay/DwellTime buziladi (avval 00:00 ketardi).
        await msg.answer(
            f"Please include both check-in and check-out times for Load #{parsed['load_id']}, e.g:\n"
            "Check in: 2:30 PM CST\n"
            "Check out: 4:15 PM CST"
        )
        await record_checkin_event(result="missing_time", **event)
        return True

    is_pickup = parsed["doc_type"] == "BOL"
    result = await send_checkin_checkout(chat_id, parsed["load_id"], parsed["checkin"], parsed["checkout"], is_pickup)
    tz_enum = extract_timezone(parsed.get("checkin")) if extract_tz_token(parsed.get("checkin")) else extract_timezone(parsed.get("checkout"))
    if result.get("success"):
        doc_type = parsed["doc_type"]
        stop_label = "Picked up" if doc_type == "BOL" else "Finished/delivered"
        await msg.answer(f"✅ {doc_type} Check-in/out received for Load #{parsed['load_id']} ({stop_label})")
        await record_checkin_event(result="sent", tz_enum=tz_enum, **event)
    elif result.get("invalid_time"):
        await msg.answer(
            f"❌ Couldn't read the check-in/check-out time for Load #{parsed['load_id']}. "
            "Please resend, e.g:\n"
            "Check in: 2:30 PM CST\n"
            "Check out: 4:15 PM CST"
        )
        await record_checkin_event(result="invalid_time", **event)
    elif result.get("server_down") or result.get("status") in (502, 503, 504):
        logger.warning("⚠️ Server down — silently skipping checkin/checkout reply for Load #%s: %s",
                       parsed['load_id'], result.get('error'))
        await record_checkin_event(result="server_down", error=result.get("error"), **event)
    elif result.get("status") == 400:
        await msg.answer(f"❌ Invalid data for Load #{parsed['load_id']}. Please check the format and try again.")
        await record_checkin_event(result="backend_error", error=f"400: {result.get('error')}", **event)
    elif result.get("status") == 404:
        await msg.answer(f"❌ Load #{parsed['load_id']} not found. Please check the load number.")
        await record_checkin_event(result="backend_error", error=f"404: {result.get('error')}", **event)
    elif result.get("status") == 409:
        await msg.answer(result.get("error", "Conflict"))
        await record_checkin_event(result="backend_error", error=f"409: {result.get('error')}", **event)
    elif result.get("status") == 500:
        await msg.answer("❌ Server error. Please try again in a few minutes.")
        await record_checkin_event(result="backend_error", error=f"500: {result.get('error')}", **event)
    else:
        error = result.get("error", "Unknown error")
        await msg.answer(f"❌ Check-in/out failed for Load #{parsed['load_id']}\n\n⚠️ {error}")
        await record_checkin_event(result="backend_error", error=str(error), **event)
    return True
