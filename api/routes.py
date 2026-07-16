"""HTTP API endpoints — message queue, tickets, drivers, permissions, loads, accepted.

State, funksiyalar va singleton client'lar o'z modullaridan (state, groups,
tickets) module-top'da import qilinadi — hech qanday late-import bandage'lar yo'q.
"""
import asyncio
import logging
from datetime import datetime

import aiosqlite
from config import DB_PATH
from db import (
    all_bols_accepted,
    clear_load_from_cache,
    get_bols_count,
    get_company_permissions,
    get_delivery_count,
    get_pickup_count,
    get_pods_count,
    has_bol_for_load,
    has_pods_for_load,
    remove_last_bol_for_load,
    remove_last_pod_for_load,
    set_bol_accepted,
)
from db.connect import db_connect
from fastapi import APIRouter, Depends, HTTPException
from groups import (
    get_group_company_id,
    get_group_driver,
    load_all_group_tokens,
    remove_group_token,
    save_group_company_id,
)
from state import (
    ACCEPTED_STATUS,
    AWAITING_TOKEN,
    GROUP_DRIVER_IDS,
    GROUP_TEAM_DRIVERS,
    GROUP_TICKET_MESSAGES,
    GROUP_TICKET_POLLING_TASKS,
    GROUP_TICKET_STATUS,
    GROUP_TICKET_TIMERS,
    REGISTERED_GROUPS,
    message_queue,
)
from state import (
    bot as tg_bot,
)
from tickets import ticket_status_api

from .auth import require_api_key
from .models import (
    AcceptedRequest,
    AcceptedResponse,
    GroupCompanyChangedRequest,
    GroupCompanyChangedResponse,
    InlineButton,
    MessageRequest,
    MessageResponse,
    PaperworkIssueNotifyRequest,
    PaperworkIssueNotifyResponse,
    PermissionsRequest,
    PermissionsResponse,
    PermissionsUpdateRequest,
    TicketStatusRequest,
    TicketStatusResponse,
)

logger = logging.getLogger(__name__)
# Butun control-plane router shared-secret guard ortida (audit v3 H2).
# DARK-LAUNCH: kalit o'rnatilmagunicha no-op; MONITOR→ENFORCE config orqali.
router = APIRouter(dependencies=[Depends(require_api_key)])


# ====== Messages ======

@router.post("/send-message", response_model=MessageResponse)
async def send_message(data: MessageRequest):
    """Enqueue a Telegram message for the worker to deliver."""
    # HND-6: xabar uzunligini tekshiramiz (Telegram limiti ~4096) + navbat to'lsa 503 (backpressure).
    if not data.message or len(data.message) > 4096:
        raise HTTPException(status_code=422, detail="message bo'sh yoki 4096 belgidan uzun")
    try:
        message_queue.put_nowait(data)
    except asyncio.QueueFull:
        raise HTTPException(status_code=503, detail="Message queue full — try again later")
    return MessageResponse(
        success=True,
        message="Xabar navbatga qo'yildi",
        pinned=False,
    )


# ====== Paperwork issue notifications ======

@router.post("/paperwork-issue/notify", response_model=PaperworkIssueNotifyResponse)
async def paperwork_issue_notify(data: PaperworkIssueNotifyRequest):
    """Backend webhook: paperwork-issue yaratildi — toggle'larga qarab guruh(lar)ga inline tugmali xabar yuborish.

    Backend tayyor rendered message yuboradi. Bot company permissions'dan
    `paperworkDriverGroup` / `paperworkInternalTeam` toggle'larini o'qib, shu
    company'ning standard va/yoki internal guruhlariga (issueId tugmalar bilan)
    xabar enqueue qiladi.
    """
    perms = await get_company_permissions(str(data.companyId))
    if not perms:
        logger.info("ℹ️ /paperwork-issue/notify: no permissions row for company %s — skip", data.companyId)
        return PaperworkIssueNotifyResponse(success=True, deliveredCount=0, groupIds=[])

    send_to_driver = perms.get("paperworkDriverGroup", False)
    send_to_internal = perms.get("paperworkInternalTeam", False)
    if not (send_to_driver or send_to_internal):
        logger.info("ℹ️ /paperwork-issue/notify: both toggles off for company %s — skip", data.companyId)
        return PaperworkIssueNotifyResponse(success=True, deliveredCount=0, groupIds=[])

    # callback_data formati pw_accept_<issueId> / pw_resend_<issueId> — handlers.py'da parse qilinadi.
    buttons = [[
        InlineButton(text="✅ Accept", callback_data=f"pw_accept_{data.issueId}"),
        InlineButton(text="🔄 Resend", callback_data=f"pw_resend_{data.issueId}"),
    ]]

    tokens = load_all_group_tokens()
    delivered: list[str] = []
    for group_id, info in tokens.items():
        if str(info.get("companyId")) != str(data.companyId):
            continue
        gtype = info.get("type", "standard")
        if gtype == "standard" and not send_to_driver:
            continue
        if gtype == "internal" and not send_to_internal:
            continue
        await message_queue.put(MessageRequest(
            group_id=group_id,
            message=data.message,
            inline_buttons=buttons,
        ))
        delivered.append(group_id)

    logger.info(
        "📨 /paperwork-issue/notify: company=%s issue=%s delivered to %d group(s): %s",
        data.companyId, data.issueId, len(delivered), delivered,
    )
    return PaperworkIssueNotifyResponse(success=True, deliveredCount=len(delivered), groupIds=delivered)


# ====== Ticket status ======

@router.post("/update-ticket-status", response_model=TicketStatusResponse)
async def update_ticket_status(data: TicketStatusRequest):
    """Backend webhook: update ticket status for a group."""
    return await ticket_status_api(data)


@router.get("/ticket-status/{group_id}")
async def get_ticket_status(group_id: str):
    """Current ticket status snapshot for a group."""
    try:
        group_id_str = str(group_id)
        if group_id_str in GROUP_TICKET_STATUS:
            ticket_data = GROUP_TICKET_STATUS[group_id_str]
            messages_count = len(GROUP_TICKET_MESSAGES.get(group_id_str, []))
            return {
                "success": True,
                "group_id": group_id_str,
                "status": ticket_data.get("status", ""),
                "created_at": ticket_data.get("created_at"),
                "done_at": ticket_data.get("done_at"),
                "collected_messages": messages_count,
            }
        return {
            "success": True,
            "group_id": group_id_str,
            "status": "",
            "message": "No active ticket for this group",
        }
    except Exception:
        # HND-4: 200 + xom xato o'rniga log + umumiy xabar (bug'ni yashirmaymiz, str(e) sizmaydi).
        logger.exception("get_ticket_status failed")
        return {"success": False, "error": "internal error"}


# ====== Group ⇄ company ======

@router.post("/group-company-changed", response_model=GroupCompanyChangedResponse)
async def group_company_changed(data: GroupCompanyChangedRequest):
    """Backend webhook: guruhning company'si o'zgartirildi.

    Faqat `groups_token_cache.json`'dagi `companyId`'ni yangilaydi. Driver,
    BOL/POD, loads, ticket history va boshqa hech narsa tegmaydi —
    yangi company'ning permissions'lari avtomatik amalda bo'ladi
    (har xabar oldidan `get_or_fetch_company_id` cache'dan o'qiydi).
    """
    if data.group_id == 0 or data.company_id == 0:
        raise HTTPException(status_code=400, detail="group_id and company_id must be non-zero")

    group_id_str = str(data.group_id)
    new_company_id = str(data.company_id)
    previous = get_group_company_id(group_id_str)

    logger.info(
        "📥 /group-company-changed: group=%s company=%s (prev=%s)",
        group_id_str, new_company_id, previous,
    )

    save_group_company_id(group_id_str, new_company_id)

    return GroupCompanyChangedResponse(
        success=True,
        group_id=data.group_id,
        company_id=data.company_id,
        previous_company_id=int(previous) if previous and str(previous).isdigit() else None,
        changed=str(previous) != new_company_id,
    )


# ====== Drivers ======

@router.get("/driver/{group_id}")
async def get_driver_by_group(group_id: str):
    """Get driver info for a Telegram group."""
    group_id_str = str(group_id)
    driver_id = get_group_driver(group_id_str)
    data = load_all_group_tokens()
    group_data = data.get(group_id_str, {})
    driver_name = group_data.get("driver_name")
    company_id = group_data.get("companyId")

    if driver_id:
        return {
            "success": True,
            "group_id": group_id_str,
            "driver_id": driver_id,
            "driver_name": driver_name,
            "company_id": company_id,
        }
    return {
        "success": False,
        "group_id": group_id_str,
        "message": "No driver set for this group",
    }


# ====== Permissions ======

@router.post("/permissions", response_model=PermissionsResponse)
async def create_permissions(data: PermissionsRequest):
    """Create company permissions row."""
    now = datetime.now().isoformat()
    try:
        async with db_connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO company_permissions
                   (company_id, ticket_create, task_paraphrase, bol_pod_paperwork, check_in_check_out, sleep_time,
                    photo_pdf, paperwork_driver_group, paperwork_internal_team, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(data.companyId), int(data.ticketCreate), int(data.taskParaphrase),
                 int(data.bolPodPaperworkAnalysis), int(data.checkInCheckOut), int(data.sleepTime),
                 int(data.photoPdf), int(data.paperworkDriverGroup), int(data.paperworkInternalTeam),
                 now, now),
            )
            await db.commit()
    except aiosqlite.IntegrityError:
        raise HTTPException(status_code=409, detail=f"Permissions already exist for company {data.companyId}")
    logger.info("✅ Permissions created for company %s", data.companyId)
    return PermissionsResponse(
        companyId=data.companyId,
        ticketCreate=data.ticketCreate,
        taskParaphrase=data.taskParaphrase,
        bolPodPaperworkAnalysis=data.bolPodPaperworkAnalysis,
        checkInCheckOut=data.checkInCheckOut,
        sleepTime=data.sleepTime,
        photoPdf=data.photoPdf,
        paperworkDriverGroup=data.paperworkDriverGroup,
        paperworkInternalTeam=data.paperworkInternalTeam,
        createdAt=now,
        updatedAt=now,
    )


@router.put("/permissions/{company_id}", response_model=PermissionsResponse)
async def update_permissions(company_id: int, data: PermissionsUpdateRequest):
    """Upsert company permissions."""
    now = datetime.now().isoformat()
    async with db_connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO company_permissions
               (company_id, ticket_create, task_paraphrase, bol_pod_paperwork, check_in_check_out, sleep_time,
                photo_pdf, paperwork_driver_group, paperwork_internal_team, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(company_id) DO UPDATE SET
                 ticket_create=excluded.ticket_create,
                 task_paraphrase=excluded.task_paraphrase,
                 bol_pod_paperwork=excluded.bol_pod_paperwork,
                 check_in_check_out=excluded.check_in_check_out,
                 sleep_time=excluded.sleep_time,
                 photo_pdf=excluded.photo_pdf,
                 paperwork_driver_group=excluded.paperwork_driver_group,
                 paperwork_internal_team=excluded.paperwork_internal_team,
                 updated_at=excluded.updated_at""",
            (str(company_id), int(data.ticketCreate), int(data.taskParaphrase),
             int(data.bolPodPaperworkAnalysis), int(data.checkInCheckOut), int(data.sleepTime),
             int(data.photoPdf), int(data.paperworkDriverGroup), int(data.paperworkInternalTeam),
             now, now),
        )
        await db.commit()
    logger.info("✅ Permissions updated for company %s", company_id)
    perms = await get_company_permissions(str(company_id))
    if perms is None:
        raise HTTPException(status_code=404, detail=f"Permissions not found for company {company_id}")
    return PermissionsResponse(**perms)


@router.get("/permissions/{company_id}", response_model=PermissionsResponse)
async def get_permissions(company_id: int):
    """Read company permissions."""
    perms = await get_company_permissions(str(company_id))
    if not perms:
        raise HTTPException(status_code=404, detail=f"Permissions not found for company {company_id}")
    return PermissionsResponse(**perms)


# ====== Group lifecycle ======

@router.delete("/group-deleted/{group_id}")
async def group_deleted_webhook(group_id: str):
    """Backend webhook: group deleted — wipe local state."""
    group_id_str = str(group_id)
    logger.info("🗑️ Webhook: group %s deleted from backend, cleaning up...", group_id_str)

    remove_group_token(group_id_str)
    REGISTERED_GROUPS.pop(group_id_str, None)
    try:
        REGISTERED_GROUPS.pop(int(group_id_str), None)
    except ValueError:
        # group_id_str int emas — pop noma'lum key bilan no-op, e'tibor bermaymiz
        pass
    AWAITING_TOKEN.pop(group_id_str, None)
    GROUP_DRIVER_IDS.pop(group_id_str, None)
    # HND-5: ishlab turgan poll/timer task'larni bekor qilamiz + qolgan per-group holatni tozalaymiz
    # (aks holda o'chirilgan guruh uchun polling abadiy davom etardi va RAM oqib ketardi).
    for _tasks in (GROUP_TICKET_POLLING_TASKS, GROUP_TICKET_TIMERS):
        _t = _tasks.pop(group_id_str, None)
        if _t is not None and not _t.done():
            _t.cancel()
    for _d in (GROUP_TEAM_DRIVERS, GROUP_TICKET_STATUS, GROUP_TICKET_MESSAGES, ACCEPTED_STATUS):
        _d.pop(group_id_str, None)

    async with db_connect(DB_PATH) as db:
        # #20 (audit v3): destructive wipe — oldin NIMA o'chirilayotganini audit-log qilamiz
        # (recovery izi). Endpoint #6 auth guard ortida. To'liq soft-delete/DB-backup mahsulot
        # qarorini + disk hisobini kutadi (deferred — #20 qolgan qismi).
        _counts = {}
        for _t in ("loads", "bols", "pods"):
            async with db.execute(f"SELECT COUNT(*) FROM {_t} WHERE group_id=?", (group_id_str,)) as _c:
                _r = await _c.fetchone()
            _counts[_t] = _r[0] if _r else 0
        logger.warning("🗑️ AUDIT group-delete %s: o'chirilmoqda %s", group_id_str, _counts)
        await db.execute("DELETE FROM groups WHERE group_id=?", (group_id_str,))
        await db.execute("DELETE FROM loads WHERE group_id=?", (group_id_str,))
        await db.execute("DELETE FROM bols WHERE group_id=?", (group_id_str,))
        await db.execute("DELETE FROM pods WHERE group_id=?", (group_id_str,))
        await db.commit()

    logger.info("✅ Cleanup done for group %s", group_id_str)
    return {"success": True, "groupId": group_id_str}


@router.get("/company/{company_id}/groups")
async def get_company_groups(company_id: int):
    """List bot-registered groups for a company."""
    data = load_all_group_tokens()
    groups = [
        {
            "groupId": group_id,
            "groupName": info.get("group_name"),
            "savedAt": info.get("saved_at"),
        }
        for group_id, info in data.items()
        if str(info.get("companyId")) == str(company_id)
    ]
    return {"companyId": company_id, "count": len(groups), "groups": groups}


# ====== Loads ======

@router.get("/load-status/{group_id}/{load_id}")
async def get_load_status(group_id: str, load_id: str):
    """Snapshot of a single load (counts + per-BOL/POD timestamps)."""
    bols_count = await get_bols_count(group_id, load_id)
    pods_count = await get_pods_count(group_id, load_id)
    pickup_count = await get_pickup_count(group_id, load_id)
    delivery_count = await get_delivery_count(group_id, load_id)
    all_accepted = await all_bols_accepted(group_id, load_id) if bols_count > 0 else False

    async with db_connect(DB_PATH) as db:
        async with db.execute(
            "SELECT accepted, saved_at FROM bols WHERE group_id=? AND load_id=? ORDER BY id",
            (str(group_id), str(load_id)),
        ) as cursor:
            bols = [{"accepted": bool(row[0]), "saved_at": row[1]} async for row in cursor]
        async with db.execute(
            "SELECT saved_at FROM pods WHERE group_id=? AND load_id=? ORDER BY id",
            (str(group_id), str(load_id)),
        ) as cursor:
            pods = [{"saved_at": row[0]} async for row in cursor]

    return {
        "group_id": group_id,
        "load_id": load_id,
        "pickup_count": pickup_count,
        "delivery_count": delivery_count,
        "bols": {"count": bols_count, "required": pickup_count, "all_accepted": all_accepted, "items": bols},
        "pods": {"count": pods_count, "required": delivery_count, "items": pods},
        "in_cache": bols_count > 0 or pods_count > 0,
    }


# ====== Accepted (multi-BOL/POD state machine) ======

@router.post("/accepted", response_model=AcceptedResponse)
async def set_accepted_status(data: AcceptedRequest):
    """Set accepted/rejected/completed status for a load.

    - accepted: BOL yoki POD tasdiqlandi
    - rejected: oxirgi POD yoki BOL o'chadi
    - completed: load cache dan tozalanadi
    """
    group_id = data.group_id
    load_id = data.load_id
    status = data.status.lower()
    custom_message = data.message.strip() if data.message else ""

    async def _send(text):
        if not text:
            return
        try:
            await tg_bot.send_message(chat_id=int(group_id), text=text)
        except Exception as e:
            logger.warning("⚠️ Could not send message to group: %s", e)

    if status == "accepted":
        ACCEPTED_STATUS[f"{group_id}_{load_id}"] = True

        if await has_pods_for_load(group_id, load_id):
            current_pods = await get_pods_count(group_id, load_id)
            required_pods = await get_delivery_count(group_id, load_id)
            logger.info("✅ Group %s Load %s POD #%d/%d ACCEPTED", group_id, load_id, current_pods, required_pods)
            await _send(custom_message)
            return AcceptedResponse(
                success=True,
                message=f"Load {load_id} POD #{current_pods}/{required_pods} accepted",
                status="accepted",
            )
        if await has_bol_for_load(group_id, load_id):
            await set_bol_accepted(group_id, load_id, True)
            current_bols = await get_bols_count(group_id, load_id)
            required_bols = await get_pickup_count(group_id, load_id)
            all_accepted = await all_bols_accepted(group_id, load_id)
            logger.info("✅ Group %s Load %s BOL #%d/%d ACCEPTED (all_accepted=%s)",
                        group_id, load_id, current_bols, required_bols, all_accepted)
            await _send(custom_message)
            return AcceptedResponse(
                success=True,
                message=f"Load {load_id} BOL #{current_bols}/{required_bols} accepted",
                status="accepted",
            )
        return AcceptedResponse(
            success=False,
            message=f"No BOL or POD in cache for load {load_id}",
            status="error",
        )

    if status == "rejected":
        ACCEPTED_STATUS[f"{group_id}_{load_id}"] = False

        if await has_pods_for_load(group_id, load_id):
            current_pods = await get_pods_count(group_id, load_id)
            required_pods = await get_delivery_count(group_id, load_id)
            await remove_last_pod_for_load(group_id, load_id)
            remaining_pods = await get_pods_count(group_id, load_id)
            logger.error("❌ Group %s Load %s POD #%d REJECTED - remaining %d/%d",
                         group_id, load_id, current_pods, remaining_pods, required_pods)
            await _send(custom_message)
            return AcceptedResponse(
                success=True,
                message=f"Load {load_id} POD #{current_pods} rejected, waiting for new POD",
                status="rejected",
            )
        if await has_bol_for_load(group_id, load_id):
            current_bols = await get_bols_count(group_id, load_id)
            required_bols = await get_pickup_count(group_id, load_id)
            await remove_last_bol_for_load(group_id, load_id)
            remaining_bols = await get_bols_count(group_id, load_id)
            logger.error("❌ Group %s Load %s BOL #%d REJECTED - remaining %d/%d",
                         group_id, load_id, current_bols, remaining_bols, required_bols)
            await _send(custom_message)
            return AcceptedResponse(
                success=True,
                message=f"Load {load_id} BOL #{current_bols} rejected, waiting for new BOL",
                status="rejected",
            )
        return AcceptedResponse(
            success=False,
            message=f"No BOL or POD in cache for load {load_id}",
            status="error",
        )

    if status == "completed":
        await clear_load_from_cache(group_id, load_id)
        ACCEPTED_STATUS[f"{group_id}_{load_id}"] = True
        logger.info("✅ Group %s Load %s COMPLETED - load cleared from cache", group_id, load_id)
        await _send(custom_message)
        return AcceptedResponse(
            success=True,
            message=f"Load {load_id} completed, cleared from cache",
            status="completed",
        )

    return AcceptedResponse(
        success=False,
        message=f"Invalid status: {data.status}. Use 'accepted', 'rejected' or 'completed'",
        status="error",
    )
