"""Pydantic request/response models for HTTP endpoints."""
from pydantic import BaseModel


class InlineButton(BaseModel):
    """Telegram inline keyboard button.

    Bittasi `callback_data` (in-bot callback uchun) yoki `url` (tashqi link uchun)
    bilan to'ldiriladi. Hozir faqat `callback_data` ishlatiladi — `url` kelajak uchun.
    """
    text: str
    callback_data: str | None = None
    url: str | None = None


class MessageRequest(BaseModel):
    group_id: str
    message: str
    has_pin_required: bool = False
    # 2D list — har bir ichki list bir qator tugmalar (Telegram InlineKeyboard).
    # None yoki bo'sh bo'lsa oddiy xabar (backward-compat).
    inline_buttons: list[list[InlineButton]] | None = None


class MessageResponse(BaseModel):
    success: bool
    message: str
    message_id: int | None = None
    pinned: bool = False


class TicketStatusRequest(BaseModel):
    group_id: str
    status: str  # "done" or empty string


class TicketStatusResponse(BaseModel):
    success: bool
    message: str


class PermissionsRequest(BaseModel):
    companyId: int
    ticketCreate: bool = True
    taskParaphrase: bool = False
    bolPodPaperworkAnalysis: bool = True
    checkInCheckOut: bool = True
    sleepTime: bool = True
    photoPdf: bool = True
    paperworkDriverGroup: bool = False
    paperworkInternalTeam: bool = False


class PermissionsUpdateRequest(BaseModel):
    ticketCreate: bool = True
    taskParaphrase: bool = False
    bolPodPaperworkAnalysis: bool = True
    checkInCheckOut: bool = True
    sleepTime: bool = True
    photoPdf: bool = True
    paperworkDriverGroup: bool = False
    paperworkInternalTeam: bool = False


class PermissionsResponse(BaseModel):
    companyId: int
    ticketCreate: bool
    taskParaphrase: bool
    bolPodPaperworkAnalysis: bool
    checkInCheckOut: bool
    sleepTime: bool
    photoPdf: bool
    paperworkDriverGroup: bool
    paperworkInternalTeam: bool
    createdAt: str
    updatedAt: str


class PaperworkIssueNotifyRequest(BaseModel):
    """Backend webhook payload: paperwork-issue yaratildi.

    Backend message matnini tayyor render qilib yuboradi (template engine'i tomonida).
    Bot company permissions va group cache asosida 0..2 ta guruhga yetkazadi va
    Accept/Resend tugmalarini issueId bilan qo'shadi.
    """
    companyId: int
    issueId: str
    message: str


class PaperworkIssueNotifyResponse(BaseModel):
    success: bool
    deliveredCount: int
    groupIds: list[str]


class AcceptedRequest(BaseModel):
    group_id: str
    load_id: str  # Load ID - qaysi load uchun
    status: str  # "accepted", "rejected" yoki "completed"
    message: str = ""  # Backend dan kelgan xabar


class AcceptedResponse(BaseModel):
    success: bool
    message: str
    status: str  # "accepted", "rejected" or "completed"


class GroupCompanyChangedRequest(BaseModel):
    """Backend webhook: guruhning company'si o'zgartirildi.

    Faqat `groups_token_cache.json`'dagi companyId yangilanadi. Driver,
    BOL/POD, loads, ticket history — hammasi avvalgicha qoladi.
    """
    group_id: int
    company_id: int


class GroupCompanyChangedResponse(BaseModel):
    success: bool
    group_id: int
    company_id: int
    previous_company_id: int | None = None  # bo'lmasa None
    changed: bool  # avvalgi qiymat boshqacha bo'lganmi
