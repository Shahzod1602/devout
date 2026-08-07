"""Telegram UI helpers: quick-action buttons keyboard.

Buttons backend'dan olinadi (per-group). API xato bersa yoki bo'sh qaytsa,
hech qanday fallback ko'rsatilmaydi — None qaytadi (keyboard chiqmaydi).
"""
import logging

import aiohttp
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from config import BASE_URL, ssl_context
from external import get_api_token
from messaging import send_error_to_group

logger = logging.getLogger(__name__)


async def get_quickbuttons(group_id):
    """Backend'dan guruh uchun quick-action buttons ro'yxatini olish."""
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
                if not buttons:
                    return None

                logger.info("✅ Loaded %d quick buttons from API for group %s", len(buttons), group_id_str)
                return buttons

    except TimeoutError:
        await send_error_to_group("❌ Quickbuttons API timeout, no buttons loaded", group_id=group_id_str)
        return None
    except Exception as e:
        await send_error_to_group(f"❌ Error fetching quickbuttons: {e}", group_id=group_id_str)
        return None


def build_quickbuttons_keyboard(buttons: list):
    """Quickbuttons matnlaridan ReplyKeyboardMarkup yaratish (oxiriga Refresh tugmasi qo'shadi).

    Buttons yo'q (None/bo'sh) bo'lsa — None qaytaradi: hech qanday fallback
    default tugma ko'rsatilmaydi, chat pastida keyboard chiqmaydi.
    """
    if not buttons:
        return None
    kb = [[KeyboardButton(text=btn)] for btn in buttons]
    kb.append([KeyboardButton(text="🔄 Refresh")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
