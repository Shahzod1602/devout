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
    since: str | None = None,
    until: str | None = None,
    max_messages: int | None = None,
    model: str | None = None,
) -> dict:
    """Bir yoki bir nechta kompaniyaning BARCHA guruhlarini bitta umumiy javobga tahlil qiladi.

    Guruh yoki xabar topilmasa `answer=None` + `messages=0` qaytaradi (model
    chaqirilmaydi). Model xatosi `llm_gateway.LlmError` bo'lib yuqoriga chiqadi
    (endpoint 502 ga o'giradi). `max_messages` — GURUH BOSHIGA limit (umumiy emas);
    yakuniy transkript baribir `build_transcript`ning umumiy belgi byudjetiga
    (eng eski xabarlardan kesib) sig'diriladi.
    """
    now = datetime.now(UTC)
    since_dt = chat_data.as_dt(since)
    until_dt = chat_data.as_dt(until)
    if hours and not since_dt:
        since_dt = now - timedelta(hours=int(hours))

    # Guruhlarni kompaniyalar bo'yicha yig'amiz (dublikatsiz — bir guruh ikkita
    # company_id ostida ikki marta hisoblanmasin).
    groups_by_id: dict[str, str | None] = {}
    for cid in company_ids:
        for g in await chat_data.fetch_groups(str(cid)):
            groups_by_id[str(g["group_id"])] = g.get("title")

    meta: dict = {
        "company_ids": [str(c) for c in company_ids],
        "groups": [{"group_id": gid, "title": t} for gid, t in groups_by_id.items()],
        "window": {
            "since": since_dt.isoformat() if since_dt else None,
            "until": until_dt.isoformat() if until_dt else None,
        },
    }
    if not groups_by_id:
        return {"answer": None, "meta": {**meta, "messages": 0, "truncated": False, "chars": 0}}

    limit = max_messages or GROUP_ANALYZE_MAX_MESSAGES

    async def _fetch(group_id: str, title: str | None) -> list[dict]:
        msgs = await chat_data.fetch_messages_window(
            group_id, since=since_dt, until=until_dt, max_messages=limit
        )
        for m in msgs:
            m["_group_title"] = title or group_id
        return msgs

    fetched = await asyncio.gather(*(_fetch(gid, t) for gid, t in groups_by_id.items()))
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

    names = [t or gid for gid, t in groups_by_id.items()]
    group_list = ", ".join(names) if len(names) <= _MAX_LISTED_GROUPS else f"{len(names)} groups"

    user_prompt = (
        f"COMPANIES: {', '.join(meta['company_ids'])}\n"
        f"GROUPS ({len(groups_by_id)}): {group_list}\n"
        f"MESSAGES: {len(all_messages)}"
        f"{' (oldest ones truncated)' if truncated else ''}\n"
        f"PERIOD (UTC): {first_ts.isoformat() if first_ts else '?'}"
        f" → {last_ts.isoformat() if last_ts else '?'}\n\n"
        f"=== TRANSCRIPT (oldest → newest, [group name] prefix per line) ===\n"
        f"{transcript}\n=== END TRANSCRIPT ===\n\n"
        f"QUESTION / TASK:\n{prompt.strip()}"
    )

    started = datetime.now(UTC)
    answer, usage = await llm_chat(
        _SYSTEM, user_prompt, model=model, endpoint_label="group-analyze"
    )
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

    Xato bo'lsa error-guruhga hisobot ketadi (jim yiqilish yo'q).
    """
    label = ",".join(str(c) for c in company_ids)
    try:
        result = await analyze_companies(company_ids, prompt, **kwargs)
    except Exception as e:
        logger.exception("analyze_and_deliver: tahlil yiqildi (companies=%s)", label)
        await _report_error(f"kompaniya tahlili yiqildi (companies={label}): {e}", label)
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
