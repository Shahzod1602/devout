"""Rate limiting: driver ticket cooldown va non-driver conversation timeout.

- Driver cooldown: bir driver oxirgi ticket'idan keyin COOLDOWN_DURATION soniya
  ichida yangi ticket yuborolmaydi.
- Conversation timeout: non-driver user oxirgi xabaridan CONVERSATION_TIMEOUT
  soniya ichida yana yozsa, ticket yaratilmaydi.
"""
import logging
from datetime import datetime

from config import CONVERSATION_TIMEOUT, COOLDOWN_DURATION
from groups import is_any_driver
from state import CONVERSATION_LAST_TIME, DRIVER_COOLDOWN

logger = logging.getLogger(__name__)


async def check_driver_cooldown(driver_id):
    """Driverning oxirgi ticket yuborgan vaqti bilan solishtiramiz.

    Returns (allowed: bool, remaining_seconds: float | None).
    """
    now = datetime.now()
    last_time = DRIVER_COOLDOWN.get(driver_id)

    if last_time is None:
        return True, None

    time_diff = (now - last_time).total_seconds()
    if time_diff < COOLDOWN_DURATION:
        remaining = COOLDOWN_DURATION - time_diff
        return False, remaining

    return True, None


async def update_driver_cooldown(driver_id):
    """Driverning oxirgi ticket vaqtini yangilaymiz"""
    DRIVER_COOLDOWN[driver_id] = datetime.now()


async def check_conversation_timeout(group_id, user_id):
    """Non-driver user conversation timeout'i ichida ekanligini tekshirish.

    Driver uchun har doim True (cooldown yo'q).
    """
    if is_any_driver(group_id, user_id):
        return True

    now = datetime.now()
    last_conv_time = CONVERSATION_LAST_TIME.get(group_id)

    if last_conv_time is None:
        return True

    time_diff = (now - last_conv_time).total_seconds()
    if time_diff < CONVERSATION_TIMEOUT:
        remaining = CONVERSATION_TIMEOUT - time_diff
        logger.debug("⏳ Conversation davom etmoqda (non-driver). %d soniya kutish kerak.", int(remaining))
        return False

    return True


async def update_conversation_time(group_id, user_id):
    """Non-driver xabar yozganda conversation vaqtini yangilaymiz."""
    if not is_any_driver(group_id, user_id):
        CONVERSATION_LAST_TIME[group_id] = datetime.now()
        logger.debug("💬 Conversation time updated for non-driver %s", user_id)
