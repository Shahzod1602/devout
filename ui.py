"""Telegram UI helpers: quick-action buttons keyboard.

Buttons backend'dan olinadi (per-group). Hech qanday fallback default tugma
ko'rsatilmaydi:
  • backend'da tugma sozlanmagan (bo'sh ro'yxat) → eski keyboard ReplyKeyboardRemove
    bilan tozalanadi (chat pastidagi tugmalar yo'qoladi);
  • transient API xatosi (timeout/5xx) → eski keyboard tegilmaydi (ishteyapti);
  • fallback default tugmalar (Vehicle Issue, ...) — butunlay olib tashlandi.
"""
import logging

import aiohttp
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from config import BASE_URL, ssl_context
from external import get_api_token
from messaging import send_error_to_group

logger = logging.getLogger(__name__)


async def get_quickbuttons(group_id):
    """Backend'dan guruh uchun quick-action buttons ro'yxatini olish.

    Return qiymati farqlaydi:
      • ro'yxat (bo'sh bo'lishi mumkin) — backend javob berdi; [] = tugma sozlanmagan.
      • None — transient API xatosi (token yo'q/timeout/5xx); chaqiruvchi eski
        keyboard'ga tegmasligi kerak.
    """
    token = await get_api_token()
    if not token:
        return None

    group_id_str = str(group_id)
    # Negative group ID'lar uchun keying'i blok no-op edi, lekin xulqni saqlaymiz.
    if group_id_str.startswith('-'):
        if group_id_str.startswith('-100'):
            group_id_str = group_id_str[0:]
        else:
            group_id_str = group_id_str[0:]

    url = f"{BASE_URL}/quick-action-buttons/by-group/{group_id_str}"
    params = {"PageIndex": 1, "PageSize": 50}
    headers = {"Authorization": f"Bearer {token}", "Accept-Language": "EN", "X-Group-Id": str(group_id)}

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await send_error_to_group(f"❌ Quickbuttons API error: {resp.status}", group_id=group_id_str)
                    return None

                # content_type=None — prod backend Content-Type yubormaydi (#botprod-migration)
                data = await resp.json(content_type=None)
                buttons = [x["title"] for x in data.get("items", [])]
                if buttons:
                    logger.info("✅ Loaded %d quick buttons from API for group %s", len(buttons), group_id_str)
                return buttons  # [] — tugma sozlanmagan (None emas: transient emas)

    except TimeoutError:
        await send_error_to_group("❌ Quickbuttons API timeout, keyboard unchanged", group_id=group_id_str)
        return None
    except Exception as e:
        await send_error_to_group(f"❌ Error fetching quickbuttons: {e}", group_id=group_id_str)
        return None


def build_quickbuttons_keyboard(buttons: list):
    """Quickbuttons matnlaridan ReplyKeyboardMarkup yaratish (oxiriga Refresh tugmasi qo'shadi).

    Bo'sh/None bersa None qaytaradi (faqat himoya — chaqiruvchi send_quickbuttons
    orqali to'g'ridan-to'g'ri shu holatni boshqaradi).
    """
    if not buttons:
        return None
    kb = [[KeyboardButton(text=btn)] for btn in buttons]
    kb.append([KeyboardButton(text="🔄 Refresh")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


async def send_quickbuttons(target, chat_id, *, loaded_text="Quick buttons loaded:"):
    """Backend'dan quick-buttonlarni yuklab, `target` (types.Message) ga yuboradi.

    Xulq:
      • tugma kelsa        → loaded_text + ReplyKeyboardMarkup (tugmalar + Refresh);
      • bo'sh config ([])  → ReplyKeyboardRemove — ESKI keyboard tozalanadi;
      • transient xato None → hech narsa (eski keyboard saqlanadi).
    True = keyboard yuborildi yoki tozalandi; False = transient skip.
    """
    buttons = await get_quickbuttons(chat_id)
    if buttons is None:
        return False  # transient — eski keyboard'ga tegmaymiz
    if not buttons:
        # Backend'da tugma sozlanmagan — chat pastidagi eski tugmalarni tozalaymiz.
        await target.answer("ℹ️ No quick buttons configured for this group.",
                            reply_markup=ReplyKeyboardRemove())
        return True
    keyboard = build_quickbuttons_keyboard(buttons)
    await target.answer(loaded_text, reply_markup=keyboard)
    return True
