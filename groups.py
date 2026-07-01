"""Guruh / driver / token boshqaruvi.

Bu modul:
- groups_token_cache.json fayli bilan ishlash (yuklash, saqlash, token, companyId, ticket status)
- started_groups.json bilan ishlash (/start bosilgan guruh ro'yxati)
- DB orqali driver / team driver CRUD + in-memory cache (GROUP_DRIVER_IDS, GROUP_TEAM_DRIVERS)
- Backend bilan guruh ro'yxatdan o'tganligini tekshirish (by-group API)
- User-yuborgan tokenni backend orqali validatsiya qilish

Function'lar bot.py'dan ko'chirilgan (FAZA 3a). Backward compat uchun bot.py
ulardan re-export qiladi.
"""
import asyncio
import json
import logging
import os
from datetime import datetime

import aiohttp
import aiosqlite
from config import (
    BASE_URL,
    DB_PATH,
    INTERNAL_VALIDATE_TOKEN_URL,
    STARTED_GROUPS_FILE,
    TOKEN_CACHE_FILE,
    VALIDATE_TOKEN_URL,
    ssl_context,
)
from db import remove_team_driver_db, save_driver_id_db, save_team_driver_id_db
from external import get_api_token, invalidate_token
from messaging import send_error_to_group
from state import AWAITING_TOKEN, GROUP_DRIVER_IDS, GROUP_TEAM_DRIVERS, REGISTERED_GROUPS, STARTED_GROUPS

logger = logging.getLogger(__name__)


# ====== Token cache file CRUD ======

# RAM cache — is_internal_group() har text xabarda chaqiriladi; avval har safar
# diskdan o'qib parse qilinardi (event loop'ni bloklaydi). Endi bir marta yuklanadi.
_TOKEN_CACHE: dict | None = None


def load_all_group_tokens():
    """All guruhlar tokenlarini o'qish (RAM cache orqali — har chaqiruvда diskka bormaydi)."""
    global _TOKEN_CACHE
    if _TOKEN_CACHE is None:
        if TOKEN_CACHE_FILE.exists():
            try:
                _TOKEN_CACHE = json.loads(TOKEN_CACHE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning("Token cache corrupt or unreadable: %s", TOKEN_CACHE_FILE, exc_info=True)
                _TOKEN_CACHE = {}
        else:
            _TOKEN_CACHE = {}
    return _TOKEN_CACHE


def save_all_group_tokens(data):
    """All guruhlar tokenlarini ATOMIK saqlash (temp+rename) + RAM cache'ni yangilash.

    Avval write_text() to'g'ridan-to'g'ri yozardi — yozish o'rtasida crash/ENOSPC bo'lsa
    fayl buzilib, keyingi load {} qaytarib BARCHA guruh ro'yxatini yo'qotardi. Endi
    temp faylga yozib os.replace bilan atomik almashtiramiz; xato bo'lsa RAM saqlanadi.
    """
    global _TOKEN_CACHE
    _TOKEN_CACHE = data
    try:
        tmp = TOKEN_CACHE_FILE.with_name(TOKEN_CACHE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, TOKEN_CACHE_FILE)
    except OSError as e:
        logger.error("⚠️ Failed to persist group tokens (RAM cache saqlandi): %s", e)


# ====== Started groups (/start bosilganlar) ======

def load_started_groups() -> set:
    """/start bosilgan guruhlarni fayldan yuklash"""
    if STARTED_GROUPS_FILE.exists():
        try:
            return set(json.loads(STARTED_GROUPS_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_started_groups():
    """STARTED_GROUPS to'plamini faylga saqlash"""
    try:
        STARTED_GROUPS_FILE.write_text(json.dumps(sorted(STARTED_GROUPS)))
    except Exception as e:
        logger.warning("⚠️ Failed to persist started_groups: %s", e)


def mark_group_started(chat_id):
    """Guruhni /start bosilgan deb belgilash va diskka saqlash"""
    STARTED_GROUPS.add(chat_id)
    save_started_groups()


# ====== Group tokens ======

def save_group_token(group_id, token, group_name, group_type="standard"):
    """Bitta guruh tokenini saqlash.

    `group_type`: "standard" (oddiy /start orqali registratsiya) yoki "internal"
    (/internal_team orqali — driver flow'lari o'chiriladi).
    """
    data = load_all_group_tokens()
    existing = data.get(str(group_id), {})
    data[str(group_id)] = {
        **existing,
        "token": token,
        "group_name": group_name,
        "type": group_type,
        "saved_at": datetime.now().isoformat(),
    }
    save_all_group_tokens(data)
    logger.info("💾 Token saved for group %s (type=%s)", group_id, group_type)


def get_group_type(group_id) -> str:
    """Guruh turini olish: 'standard' yoki 'internal'. Topilmasa 'standard'."""
    data = load_all_group_tokens()
    return data.get(str(group_id), {}).get("type", "standard")


def is_internal_group(group_id) -> bool:
    """True bo'lsa — bu guruh /internal_team orqali Internal Team sifatida ro'yxatdan o'tgan."""
    return get_group_type(group_id) == "internal"


def get_group_token(group_id):
    """Guruh tokenini fayldan olish"""
    data = load_all_group_tokens()
    group_data = data.get(str(group_id))
    if group_data:
        return group_data.get("token")
    return None


def remove_group_token(group_id):
    """Guruh tokenini o'chirish (muddati o'tganda)"""
    data = load_all_group_tokens()
    if str(group_id) in data:
        del data[str(group_id)]
        save_all_group_tokens(data)
        logger.info("🗑️ Token removed for group %s", group_id)


# ====== Drivers (DB + RAM cache) ======

async def save_driver_id(group_id, driver_id, driver_name):
    """Driver ID ni DB ga saqlash + in-memory cache yangilash."""
    group_id_str = str(group_id)
    await save_driver_id_db(group_id_str, driver_id, driver_name)
    GROUP_DRIVER_IDS[group_id_str] = driver_id
    logger.info("💾 Driver %s (ID: %s) saved in DB for group %s", driver_name, driver_id, group_id)


async def save_team_driver_id(group_id, driver_id, driver_name):
    """Team driver ID ni DB ga saqlash + in-memory cache yangilash."""
    group_id_str = str(group_id)
    await save_team_driver_id_db(group_id_str, driver_id, driver_name)
    GROUP_TEAM_DRIVERS[group_id_str] = driver_id
    logger.info("💾 Team driver %s (ID: %s) saved in DB for group %s", driver_name, driver_id, group_id)


async def remove_team_driver(group_id):
    """Team driverni DB va RAM dan o'chirish."""
    group_id_str = str(group_id)
    await remove_team_driver_db(group_id_str)
    GROUP_TEAM_DRIVERS.pop(group_id_str, None)
    logger.info("🗑️ Team driver removed for group %s", group_id)


def get_group_driver(group_id) -> int | None:
    """RAM dan guruh asosiy driver ID ni olish"""
    return GROUP_DRIVER_IDS.get(str(group_id))


def get_team_driver(group_id) -> int | None:
    """RAM dan guruh team driver ID ni olish"""
    return GROUP_TEAM_DRIVERS.get(str(group_id))


def is_any_driver(group_id, user_id) -> bool:
    """User asosiy yoki team driver ekanligini tekshirish"""
    gid = str(group_id)
    return user_id == GROUP_DRIVER_IDS.get(gid) or user_id == GROUP_TEAM_DRIVERS.get(gid)


# ====== Per-group ticket status (file-backed) ======

def save_group_ticket_status(group_id, status):
    """Ticket statusni groups_token_cache.json ga saqlash"""
    data = load_all_group_tokens()
    group_id_str = str(group_id)
    if group_id_str in data:
        data[group_id_str]["ticket_status"] = status
        data[group_id_str]["ticket_status_updated_at"] = datetime.now().isoformat()
        save_all_group_tokens(data)
        logger.info("💾 Ticket status '%s' saved for group %s", status, group_id)


def get_group_ticket_status(group_id):
    """Guruh uchun ticket statusni fayldan olish"""
    data = load_all_group_tokens()
    group_data = data.get(str(group_id))
    if group_data:
        return group_data.get("ticket_status", "")
    return ""


# ====== Per-group companyId ======

def save_group_company_id(group_id, company_id):
    """Group uchun companyId ni groups_token_cache.json ga saqlash"""
    data = load_all_group_tokens()
    group_data = data.get(str(group_id), {})
    group_data["companyId"] = str(company_id)
    data[str(group_id)] = group_data
    save_all_group_tokens(data)
    logger.info("💾 CompanyId %s saved for group %s", company_id, group_id)


def get_group_company_id(group_id):
    """Group uchun companyId ni olish"""
    data = load_all_group_tokens()
    return data.get(str(group_id), {}).get("companyId")


async def get_or_fetch_company_id(group_id) -> str | None:
    """companyId ni olish; yo'q bo'lsa by-group API dan olib saqlaydi."""
    company_id = get_group_company_id(group_id)
    if company_id:
        return str(company_id)
    token = await get_api_token()
    if not token:
        return None
    url = f"{BASE_URL}/group-links/{group_id}/by-group"
    headers = {"Authorization": f"Bearer {token}", "Accept-Language": "EN", "X-Group-Id": str(group_id)}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    # content_type=None — prod backend Content-Type yubormaydi (#botprod-migration)
                    data = await resp.json(content_type=None)
                    cid = data.get("companyId") or data.get("company_id")
                    if cid:
                        save_group_company_id(group_id, cid)
                        logger.info("💾 companyId %s fetched and saved for group %s", cid, group_id)
                        return str(cid)
    except Exception as e:
        await send_error_to_group(f"❌ get_or_fetch_company_id error: {e}", group_id=group_id)
    return None


# ====== Group registration check (backend by-group API) ======

async def check_group_registered(group_id, group_name):
    """Guruh ro'yxatdan o'tganligini tekshirish - avval by-group API, keyin fayldan token"""
    group_id_str = str(group_id)

    if REGISTERED_GROUPS.get(group_id) or REGISTERED_GROUPS.get(group_id_str):
        logger.debug("ℹ️ Group %s found in RAM cache (registered)", group_id)
        return True

    token = await get_api_token()
    if not token:
        return None

    group_link_url = f"{BASE_URL}/group-links/{group_id}/by-group"
    headers = {"Authorization": f"Bearer {token}", "Accept-Language": "EN", "X-Group-Id": str(group_id)}

    logger.debug("🔍 Checking group via by-group API: %s", group_id)

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.get(group_link_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                response_text = await resp.text()
                logger.debug("🔍 by-group API response: status=%s", resp.status)

                if resp.status == 200:
                    logger.info("✅ Group %s is registered (by-group API)", group_id)
                    REGISTERED_GROUPS[group_id] = True
                    REGISTERED_GROUPS[group_id_str] = True
                    AWAITING_TOKEN.pop(group_id, None)
                    AWAITING_TOKEN.pop(group_id_str, None)
                    try:
                        resp_data = json.loads(response_text)
                        company_id = resp_data.get("companyId") or resp_data.get("company_id")
                        if company_id:
                            save_group_company_id(group_id, company_id)
                        driver_id_backend = resp_data.get("driverId")
                        # `.get("driverName", "")` agar key bor lekin value=null bo'lsa,
                        # default "" ishlamaydi va None qaytadi → None.strip() crash.
                        # Backend driver olib tashlanganda aynan shu holat sodir bo'ladi.
                        driver_name_backend = (resp_data.get("driverName") or "").strip()
                        if driver_id_backend is None:
                            local_driver = GROUP_DRIVER_IDS.pop(group_id_str, None)
                            if local_driver:
                                logger.info("🗑️ Driver removed from local (driverId=null in backend) for group %s", group_id_str)
                                async with aiosqlite.connect(DB_PATH) as db:
                                    await db.execute(
                                        "UPDATE groups SET driver_id=NULL, driver_name=NULL, updated_at=datetime('now') WHERE group_id=?",
                                        (group_id_str,)
                                    )
                                    await db.commit()
                        elif driver_name_backend:
                            local_driver = GROUP_DRIVER_IDS.get(group_id_str)
                            if local_driver:
                                async with aiosqlite.connect(DB_PATH) as db:
                                    await db.execute(
                                        "UPDATE groups SET driver_name=?, updated_at=datetime('now') WHERE group_id=?",
                                        (driver_name_backend, group_id_str)
                                    )
                                    await db.commit()
                                logger.info("🔄 Driver name updated from backend: '%s' for group %s", driver_name_backend, group_id_str)
                    except Exception as e:
                        logger.warning("⚠️ by-group parse error: %s", e)
                    return True

                elif resp.status == 404:
                    await send_error_to_group("❌ Group not found in by-group API", group_id=group_id)
                    REGISTERED_GROUPS.pop(group_id, None)
                    REGISTERED_GROUPS.pop(group_id_str, None)
                    AWAITING_TOKEN[group_id] = True
                    return False

                elif resp.status in (401, 403):
                    # Token muddati o'tgan — yangilab, chaqiruvchi QAYTA URINSIN.
                    # Avval False qaytarardi ("ro'yxatda yo'q") + AWAITING_TOKEN=True yozardi —
                    # bu to'g'ri ro'yxatdagi guruhga "❌ ro'yxatda emas" deb noto'g'ri javob berardi.
                    logger.warning("⚠️ API token expired [%s], refreshing and retrying...", resp.status)
                    invalidate_token()
                    await get_api_token()
                    return None

                else:
                    logger.warning("⚠️ Unexpected response [%s]: %s", resp.status, response_text)
                    return None  # Server error - retry kerak

    except Exception as e:
        await send_error_to_group(f"❌ Error checking group registration: {e}", group_id=group_id)
        return None  # Connection error - retry kerak


async def check_group_registered_force(group_id, group_name, force_check=False):
    """Group registratsiyasini majburiy tekshirish (cache'dan o'tmay)"""
    group_id_str = str(group_id)

    if not force_check:
        if REGISTERED_GROUPS.get(group_id) or REGISTERED_GROUPS.get(group_id_str):
            logger.debug("ℹ️ Group %s found in RAM cache (skipping API call)", group_id)
            return True

    if force_check:
        REGISTERED_GROUPS.pop(group_id, None)
        REGISTERED_GROUPS.pop(group_id_str, None)
        logger.info("🧹 Cleared cache for group %s", group_id)

    return await check_group_registered(group_id, group_name)


async def wait_for_server_and_check(group_id, group_name, msg, force_check=False) -> bool:
    """500/502 yoki connection error bo'lsa 'Connecting...' xabar chiqarib retry qiladi."""
    result = await check_group_registered_force(group_id, group_name, force_check=force_check)
    if result is not None:
        return result

    try:
        while True:
            await asyncio.sleep(5)
            result = await check_group_registered_force(group_id, group_name, force_check=force_check)
            if result is not None:
                return result
    except Exception as e:
        await send_error_to_group(f"❌ wait_for_server_and_check error: {e}", group_id=group_id)
        return False


async def _post_validate_token(url, label, access_token, user_token, group_id, group_name):
    """Shared validation POST — `url` ga payload yuborib, backend javobini bir xil shaklga keltiradi.

    NOTE: prod backend `validate-bot-token` Content-Type header'ini yubormaydi
    (test'da yuboriladi). aiohttp'ning `resp.json()` strict mimetype check'idan
    o'tib bo'lmaydi — text'dan manually parse qilamiz.
    """
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            payload = {
                "token": user_token,
                "groupId": str(group_id),
                "groupName": group_name
            }
            headers = {
                "Accept-Language": "EN",
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "X-Group-Id": str(group_id)
            }

            logger.info("🔐 Validating %s token for group %s...", label, group_id)
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                response_text = await resp.text()
                logger.info("📨 %s validation response status: %s", label, resp.status)
                try:
                    data = json.loads(response_text) if response_text else None
                except (json.JSONDecodeError, ValueError, TypeError):
                    data = None
                if isinstance(data, dict):
                    return {
                        "success": resp.status == 200,
                        "status": resp.status,
                        "data": data,
                        "message": data.get("message", "Validation completed"),
                    }
                return {
                    "success": resp.status == 200,
                    "status": resp.status,
                    "data": None,
                    "message": response_text,
                    "raw_response": response_text,
                }
    except Exception as e:
        logger.exception("❌ %s Validate Token Error", label)
        return {
            "success": False,
            "error": str(e),
            "message": f"Validation error: {str(e)}"
        }


async def validate_bot_token(access_token, user_token, group_id, group_name):
    """User-yuborgan tokenni backend orqali validatsiya qilish (oddiy guruh)."""
    return await _post_validate_token(
        VALIDATE_TOKEN_URL, "standard", access_token, user_token, group_id, group_name
    )


async def validate_bot_token_internal(access_token, user_token, group_id, group_name):
    """Internal team guruhini ro'yxatdan o'tkazish.

    POST /general-settings/validate-bot-token/internal — body bir xil
    (token/groupId/groupName); backend guruhni `Internal` deb belgilaydi (AgentBot uchun).
    """
    return await _post_validate_token(
        INTERNAL_VALIDATE_TOKEN_URL, "internal", access_token, user_token, group_id, group_name
    )
