"""Kompaniya chat tahlili — foydalanuvchi prompti + BIR NECHTA guruh transkripti →
BITTA umumiy LLM javobi.

Oqim: `chat_data.fetch_groups` (company_id → guruhlar) → har guruh uchun PARALLEL
`chat_data.fetch_messages_window` → barcha xabarlar VAQT bo'yicha birlashtirilib
saralanadi (guruhlar aralash-quralash, eng yangi kontekst ustuvor) → bitta
transkript → `llm_gateway.llm_chat`. Endpoint: `api/ai_chat.py: POST /api/ai/chat/analyze`.

Dizayn qarorlari:
- **Guruh emas, kompaniya bo'yicha.** `group_id` yo'q — chaqiruvchi `company_ids`
  beradi, shu kompaniya(lar)ning BARCHA guruhlari avtomatik yig'iladi va BITTA
  javobga birlashtiriladi (2026-08-13, foydalanuvchi so'rovi bilan; eski
  bir-guruhli `group_id` rejimi butunlay olib tashlandi — breaking change).
- Tenant izolyatsiyasi TABIIY: `chat_data.fetch_groups(company_id)` faqat o'sha
  kompaniyaga tegishli guruhlarni qaytaradi — boshqa kompaniya guruhi hech qachon
  qatnashmaydi, alohida darvoza tekshiruvi shart emas.
- **DB'ga hech nima yozilmaydi** — chat tarixi faqat o'qiladi.
- Telegram'ga xabar FAQAT `ceo_id` berilganda ketadi (`analyze_and_deliver`);
  usiz endpoint sof o'quvchi bo'lib qoladi.
- **Transkript kesish eskidan.** Limit oshsa eng eski xabarlar tushadi (yangi
  kontekst muhimroq), javob meta'sida `truncated: true` qaytadi. Kesish BARCHA
  guruhlar bo'ylab umumiy (vaqt bo'yicha saralangan ro'yxatdan), bitta guruh
  boshqasi hisobiga butunlay yutib yubormaydi.
- **Media** matn sifatida `[photo]` / `[voice]` ko'rinishida — fayl mazmuni
  tahlilga kirmaydi (blob DB'da yo'q, faqat file_id).
- **Ko'p kompaniyada javob KOMPANIYA BO'YICHA bo'linadi** (2026-08-13, foydalanuvchi
  so'rovi): promptga `COMPANIES:` ro'yxati (har biriga tegishli guruhlar bilan)
  qo'shiladi, transkript qatorlari `[Cxx·GuruhNomi]` prefiksi oladi (faqat >1
  kompaniya bo'lsa — bitta kompaniyada eski, shovqinsiz format qoladi), va
  `DEFAULT_PROMPT` modelga har kompaniya uchun alohida sarlavha (`🏢 COMPANY <id>`)
  qo'yishni buyuradi.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import chat_data
from config import GROUP_ANALYZE_MAX_CHARS, GROUP_ANALYZE_MAX_MESSAGES
from llm_gateway import llm_chat
from tg_deliver import deliver_text

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are an analyst reading Telegram group chats of a US trucking dispatch company. "
    "The groups contain dispatchers, drivers and bots discussing loads, pickups, deliveries, "
    "documents (BOL/POD/rate confirmation), check-ins, breakdowns and payments.\n"
    "Rules:\n"
    "- Answer ONLY from the transcript below. Never invent names, numbers, dates or events.\n"
    "- If the transcript does not contain the answer, say so plainly instead of guessing.\n"
    "- Quote message timestamps/authors when they support your answer.\n"
    "- Reply in the SAME language the user's question is written in.\n"
    "- Be concise and concrete; prefer bullet points for lists of findings."
)

# `prompt` ixtiyoriy — berilmasa shu ishlatiladi (2026-08-13, foydalanuvchi so'rovi:
# har safar prompt yozish shart bo'lmasin, `company_ids` + `hours` yetarli bo'lsin).
# 2026-08-13 v2: bir nechta company_ids berilganda javob KOMPANIYA BO'YICHA bo'linadi
# (foydalanuvchi so'rovi) — promptdagi COMPANIES ro'yxatiga va transkriptdagi
# [company·group] prefiksiga tayanadi (analyze_companies quradi).
DEFAULT_PROMPT = (
    "Give me today's operational summary. If the COMPANIES list above has more than one "
    "company, structure your answer BY COMPANY — one \"🏢 COMPANY <id>\" heading per company, "
    "each with its own NEEDS ATTENTION and RESOLVED TODAY underneath, so nothing from one "
    "company's chats gets attributed to another. If there is only one company, skip the "
    "heading and go straight into the sections below.\n\n"
    "🚨 NEEDS ATTENTION — anything unresolved or concerning: drivers not responding, "
    "loads stuck/delayed at pickup or delivery, missing or rejected documents "
    "(BOL/POD), breakdowns, detention, payment disputes. For each item: which "
    "group/load, what happened, how long it's been open, who last engaged with it.\n\n"
    "✅ RESOLVED TODAY — loads delivered, documents accepted, issues closed since "
    "yesterday.\n\n"
    "📈 SNAPSHOT — one combined set of rough counts at the very end (active loads, "
    "deliveries completed, open document issues, drivers not checked in), broken down "
    "per company only if the numbers are large enough that a single total would hide "
    "which company needs the attention.\n\n"
    "Rules: use ONLY what's in the transcript — don't guess or infer beyond what's "
    "written. Cite load numbers, driver names, and timestamps whenever you state a "
    "fact. If a section (or a whole company) has nothing to report, say so briefly "
    "instead of omitting it. Keep it tight — bullet points, no fluff."
)


def build_transcript(
    messages: list[dict], *, max_chars: int = GROUP_ANALYZE_MAX_CHARS
) -> tuple[str, bool]:
    """Xabarlar → `MM-DD HH:MM Ism: matn` satrlari. `(transkript, kesildimi)`."""
    lines: list[str] = []
    for m in messages:
        ts = chat_data.as_dt(m.get("created_at"))
        stamp = ts.strftime("%m-%d %H:%M") if ts else "??-?? ??:??"
        who = (m.get("user_name") or f"user{m.get('user_id') or '?'}").strip()
        text = (m.get("text") or "").strip()
        msg_type = m.get("msg_type") or "text"
        if not text:
            text = f"[{msg_type}]" if msg_type != "text" else "[bo'sh]"
        elif msg_type != "text":
            text = f"[{msg_type}] {text}"

        reply = ""
        if m.get("reply_to_message_id"):
            r_who = (m.get("reply_user_name") or "?").strip()
            r_text = (m.get("reply_text") or f"[{m.get('reply_msg_type') or 'media'}]").strip()
            if len(r_text) > 80:
                r_text = r_text[:80] + "…"
            reply = f" (↳ {r_who}: {r_text})"

        # Ko'p-guruhli tahlilda har xabar qaysi guruhdan ekani ko'rinsin (bitta
        # guruhda `_group_title` qo'yilmagan — eski format o'zgarishsiz qoladi).
        group_label = f"[{m['_group_title']}] " if m.get("_group_title") else ""
        lines.append(f"{stamp} {group_label}{who}{reply}: {text}")

    truncated = False
    # Eng eskilardan kesamiz — oxirgi kontekst saqlanadi.
    while lines and sum(len(x) + 1 for x in lines) > max_chars:
        lines.pop(0)
        truncated = True
    return "\n".join(lines), truncated


# Ko'p guruhli chaqiruvda promptga bosiladigan guruh nomlari ro'yxati shu
# uzunlikdan oshsa "N groups" bilan almashtiriladi (bitta company o'nlab guruhga
# ega bo'lsa, sarlavha o'zi transkript byudjetini yemasin).
_MAX_LISTED_GROUPS = 20


async def analyze_companies(
    company_ids: list[str],
    prompt: str,
    *,
    hours: int | None = None,
) -> dict:
    """Bir yoki bir nechta kompaniyaning BARCHA guruhlarini bitta umumiy javobga tahlil qiladi.

    Guruh yoki xabar topilmasa `answer=None` + `messages=0` qaytaradi (model
    chaqirilmaydi). Model xatosi `llm_gateway.LlmError` bo'lib yuqoriga chiqadi
    (endpoint 502 ga o'giradi). `hours` bermasangiz — butun tarix (guruh boshiga
    `GROUP_ANALYZE_MAX_MESSAGES` limitigacha); yakuniy transkript baribir
    `build_transcript`ning umumiy belgi byudjetiga (eng eski xabarlardan kesib)
    sig'diriladi.
    """
    since_dt = datetime.now(UTC) - timedelta(hours=int(hours)) if hours else None

    # Guruhlarni kompaniyalar bo'yicha yig'amiz (dublikatsiz — bir guruh ikkita
    # company_id ostida ikki marta hisoblanmasin). company_id har guruhga
    # yopishtiriladi — javobni kompaniya bo'yicha bo'lish uchun kerak.
    groups_by_id: dict[str, dict] = {}
    for cid in company_ids:
        for g in await chat_data.fetch_groups(str(cid)):
            gid = str(g["group_id"])
            groups_by_id[gid] = {
                "title": g.get("title"),
                "company_id": str(g.get("company_id") or cid),
            }

    meta: dict = {
        "company_ids": [str(c) for c in company_ids],
        "groups": [
            {"group_id": gid, "title": info["title"], "company_id": info["company_id"]}
            for gid, info in groups_by_id.items()
        ],
        "window": {"since": since_dt.isoformat() if since_dt else None},
    }
    if not groups_by_id:
        return {"answer": None, "meta": {**meta, "messages": 0, "truncated": False, "chars": 0}}

    by_company: dict[str, list[str]] = {}
    for info in groups_by_id.values():
        by_company.setdefault(info["company_id"], []).append(info["title"] or "?")
    multi_company = len(by_company) > 1

    async def _fetch(group_id: str, title: str | None, company_id: str) -> list[dict]:
        msgs = await chat_data.fetch_messages_window(
            group_id, since=since_dt, max_messages=GROUP_ANALYZE_MAX_MESSAGES
        )
        # Bir nechta kompaniya bo'lsa, har xabar qaysi kompaniyadan ekani transkriptda
        # ham ko'rinsin — model javobni kompaniya bo'yicha to'g'ri bo'la olsin.
        label = f"C{company_id}·{title}" if multi_company and title else title
        for m in msgs:
            m["_group_title"] = label or group_id
        return msgs

    fetched = await asyncio.gather(
        *(_fetch(gid, info["title"], info["company_id"]) for gid, info in groups_by_id.items())
    )
    all_messages = [m for msgs in fetched for m in msgs]
    # Guruhlar aralash-quralash — vaqt bo'yicha bitta oqimga tekislaymiz, shunda
    # kesish (build_transcript) BARCHA guruhlar bo'ylab adolatli bo'ladi (bitta
    # gapiruvchan guruh boshqalarini butunlay yutib yubormaydi).
    all_messages.sort(key=lambda m: chat_data.as_dt(m.get("created_at")) or datetime.min.replace(tzinfo=UTC))

    if not all_messages:
        return {"answer": None, "meta": {**meta, "messages": 0, "truncated": False, "chars": 0}}

    transcript, truncated = build_transcript(all_messages)
    first_ts = chat_data.as_dt(all_messages[0].get("created_at"))
    last_ts = chat_data.as_dt(all_messages[-1].get("created_at"))

    companies_lines = []
    for cid, names in by_company.items():
        listed = ", ".join(names) if len(names) <= _MAX_LISTED_GROUPS else f"{len(names)} groups"
        companies_lines.append(f"- Company {cid} — {len(names)} group(s): {listed}")
    companies_block = "\n".join(companies_lines)

    user_prompt = (
        f"COMPANIES ({len(by_company)}):\n{companies_block}\n\n"
        f"MESSAGES: {len(all_messages)}"
        f"{' (oldest ones truncated)' if truncated else ''}\n"
        f"PERIOD (UTC): {first_ts.isoformat() if first_ts else '?'}"
        f" → {last_ts.isoformat() if last_ts else '?'}\n\n"
        f"=== TRANSCRIPT (oldest → newest, [company·group] prefix per line) ===\n"
        f"{transcript}\n=== END TRANSCRIPT ===\n\n"
        f"QUESTION / TASK:\n{prompt.strip()}"
    )

    started = datetime.now(UTC)
    answer, usage = await llm_chat(_SYSTEM, user_prompt, endpoint_label="group-analyze")
    latency_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)

    return {
        "answer": answer,
        "meta": {
            **meta,
            "messages": len(all_messages),
            "truncated": truncated,
            "chars": len(transcript),
            "period": {
                "first": first_ts.isoformat() if first_ts else None,
                "last": last_ts.isoformat() if last_ts else None,
            },
            "latency_ms": latency_ms,
            "usage": usage,
        },
    }


# === Fon oqimi: tahlil qil → natijani Telegram chatga yubor ===
#
# Nega fon: backend HttpClient timeouti 100s, bitta guruh tahlili esa 12-40s
# (kattaroqlarida ko'proq) — sinxron javob timeout xavfi
# (memory: project_categorize_timeout_100s). Endpoint 202 qaytaradi, ish shu
# yerda davom etadi. Task'lar GC qilib yuborilmasligi uchun ro'yxatda ushlanadi.
_BG_TASKS: set = set()


def schedule_analyze_and_deliver(chat_id: str | int, **kwargs) -> None:
    """`analyze_and_deliver` ni fon task sifatida ishga tushiradi (hech nima kutmaydi)."""
    task = asyncio.create_task(analyze_and_deliver(chat_id, **kwargs))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def analyze_and_deliver(chat_id: str | int, company_ids: list[str], prompt: str, **kwargs) -> dict:
    """Tahlil + yetkazish. HECH QACHON raise qilmaydi — fon task'da hech kim ushlamaydi.

    Xato bo'lsa error-guruhga hisobot ketadi VA CEO chatiga ham qisqa xabar
    yuboriladi — jim qolish "hali tayyorlanyapti"dan farq qilmaydi, CEO
    hech qachon kelmaydigan javobni kutib qolmasin (2026-08-13 PROD topilmasi).
    """
    label = ",".join(str(c) for c in company_ids)
    try:
        result = await analyze_companies(company_ids, prompt, **kwargs)
    except Exception as e:
        logger.exception("analyze_and_deliver: tahlil yiqildi (companies=%s)", label)
        await _report_error(f"kompaniya tahlili yiqildi (companies={label}): {e}", label)
        try:
            await deliver_text(
                chat_id, "⚠️ Tahlil vaqtincha ishlamadi. Birozdan so'ng qayta urinib ko'ring."
            )
        except Exception:
            logger.exception("analyze_and_deliver: xato xabarini ham yuborib bo'lmadi (chat=%s)", chat_id)
        return {"ok": False, "stage": "analyze", "error": str(e)}

    meta = result["meta"]
    groups = meta.get("groups") or []
    if not groups:
        header = f"companies: {label}"
    elif len(groups) == 1:
        header = groups[0].get("title") or groups[0].get("group_id")
    else:
        header = f"{len(groups)} groups — companies: {label}"

    if result["answer"] is None:
        # Bo'sh oyna ham xabar qilinadi — jim qolish "ishlamadi"dan farq qilmaydi.
        body = f"📊 {header}\n\nSo'ralgan davrda xabar yo'q."
    else:
        body = f"📊 {header}\n\n{result['answer']}"

    delivery = await deliver_text(chat_id, body)
    if not delivery["ok"]:
        logger.warning("analyze_and_deliver: yetkazilmadi (chat=%s): %s", chat_id, delivery["error"])
        await _report_error(
            f"CEO hisoboti yetkazilmadi (chat={chat_id}, companies={label}): {delivery['error']}",
            label,
        )
    else:
        logger.info("analyze_and_deliver: chat=%s ga %d qismda yuborildi (companies=%s, %d xabar)",
                    chat_id, delivery["parts"], label, meta.get("messages", 0))
    return {"ok": delivery["ok"], "stage": "deliver", "delivery": delivery, "meta": meta}


async def _report_error(message: str, label: str) -> None:
    """Error-guruhga xabar; hisobot yo'lini o'zi yiqitmasin."""
    try:
        from messaging import send_error_to_group  # late import: circular'ni sindirish
        await send_error_to_group(message, label)
    except Exception:
        logger.exception("analyze_and_deliver: error-guruhga yozib bo'lmadi")
