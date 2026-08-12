"""Guruh chat tahlili — foydalanuvchi prompti + guruh transkripti → LLM javobi.

Oqim: `chat_data.fetch_messages_window` (DB'dagi chat_messages) → transkript matni
→ `llm_gateway.llm_chat`. Endpoint: `api/ai_chat.py: POST /api/ai/chat/analyze`.

Dizayn qarorlari:
- **DB'ga hech nima yozilmaydi** — chat tarixi faqat o'qiladi.
- Telegram'ga xabar FAQAT `ceo_id` berilganda ketadi (`analyze_and_deliver`);
  usiz endpoint sof o'quvchi bo'lib qoladi.
- **Transkript kesish eskidan.** Limit oshsa eng eski xabarlar tushadi (yangi
  kontekst muhimroq), javob meta'sida `truncated: true` qaytadi.
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

        lines.append(f"{stamp} {who}{reply}: {text}")

    truncated = False
    # Eng eskilardan kesamiz — oxirgi kontekst saqlanadi.
    while lines and sum(len(x) + 1 for x in lines) > max_chars:
        lines.pop(0)
        truncated = True
    return "\n".join(lines), truncated


async def analyze_group(
    group_id: str,
    prompt: str,
    *,
    hours: int | None = None,
    since: str | None = None,
    until: str | None = None,
    max_messages: int | None = None,
    model: str | None = None,
) -> dict:
    """Guruh tarixini foydalanuvchi promptiga ko'ra tahlil qiladi.

    Xabar topilmasa `answer=None` + `messages=0` qaytaradi (model chaqirilmaydi).
    Model xatosi `llm_gateway.LlmError` bo'lib yuqoriga chiqadi (endpoint 502 ga o'giradi).
    """
    now = datetime.now(UTC)
    since_dt = chat_data.as_dt(since)
    until_dt = chat_data.as_dt(until)
    if hours and not since_dt:
        since_dt = now - timedelta(hours=int(hours))

    limit = max_messages or GROUP_ANALYZE_MAX_MESSAGES
    messages = await chat_data.fetch_messages_window(
        group_id, since=since_dt, until=until_dt, max_messages=limit
    )
    title = await chat_data.fetch_group_title(group_id)

    meta: dict = {
        "group_id": group_id,
        "title": title,
        "messages": len(messages),
        "window": {
            "since": since_dt.isoformat() if since_dt else None,
            "until": until_dt.isoformat() if until_dt else None,
        },
    }
    if not messages:
        return {"answer": None, "meta": {**meta, "truncated": False, "chars": 0}}

    transcript, truncated = build_transcript(messages)
    first_ts = chat_data.as_dt(messages[0].get("created_at"))
    last_ts = chat_data.as_dt(messages[-1].get("created_at"))

    user_prompt = (
        f"GROUP: {title or group_id}\n"
        f"MESSAGES: {len(messages)}"
        f"{' (oldest ones truncated)' if truncated else ''}\n"
        f"PERIOD (UTC): {first_ts.isoformat() if first_ts else '?'}"
        f" → {last_ts.isoformat() if last_ts else '?'}\n\n"
        f"=== TRANSCRIPT (oldest → newest) ===\n{transcript}\n=== END TRANSCRIPT ===\n\n"
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


async def analyze_and_deliver(chat_id: str | int, group_id: str, prompt: str, **kwargs) -> dict:
    """Tahlil + yetkazish. HECH QACHON raise qilmaydi — fon task'da hech kim ushlamaydi.

    Xato bo'lsa error-guruhga hisobot ketadi (jim yiqilish yo'q).
    """
    try:
        result = await analyze_group(group_id, prompt, **kwargs)
    except Exception as e:
        logger.exception("analyze_and_deliver: tahlil yiqildi (group=%s)", group_id)
        await _report_error(f"guruh tahlili yiqildi (group={group_id}): {e}", group_id)
        return {"ok": False, "stage": "analyze", "error": str(e)}

    meta = result["meta"]
    title = meta.get("title") or group_id
    if result["answer"] is None:
        # Bo'sh oyna ham xabar qilinadi — jim qolish "ishlamadi"dan farq qilmaydi.
        body = f"📊 {title}\n\nSo'ralgan davrda xabar yo'q."
    else:
        body = f"📊 {title}\n\n{result['answer']}"

    delivery = await deliver_text(chat_id, body)
    if not delivery["ok"]:
        logger.warning("analyze_and_deliver: yetkazilmadi (chat=%s): %s", chat_id, delivery["error"])
        await _report_error(
            f"CEO hisoboti yetkazilmadi (chat={chat_id}, group={group_id}): {delivery['error']}",
            group_id,
        )
    else:
        logger.info("analyze_and_deliver: chat=%s ga %d qismda yuborildi (group=%s, %d xabar)",
                    chat_id, delivery["parts"], group_id, meta.get("messages", 0))
    return {"ok": delivery["ok"], "stage": "deliver", "delivery": delivery, "meta": meta}


async def _report_error(message: str, group_id: str) -> None:
    """Error-guruhga xabar; hisobot yo'lini o'zi yiqitmasin."""
    try:
        from messaging import send_error_to_group  # late import: circular'ni sindirish
        await send_error_to_group(message, group_id)
    except Exception:
        logger.exception("analyze_and_deliver: error-guruhga yozib bo'lmadi")
