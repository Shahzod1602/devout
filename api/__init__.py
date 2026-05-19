"""HTTP API endpoints for the bot service."""
from .models import (
    AcceptedRequest,
    AcceptedResponse,
    MessageRequest,
    MessageResponse,
    PermissionsRequest,
    PermissionsResponse,
    PermissionsUpdateRequest,
    TicketStatusRequest,
    TicketStatusResponse,
)
from .routes import router as api_router

__all__ = [
    "api_router",
    "MessageRequest",
    "MessageResponse",
    "TicketStatusRequest",
    "TicketStatusResponse",
    "PermissionsRequest",
    "PermissionsUpdateRequest",
    "PermissionsResponse",
    "AcceptedRequest",
    "AcceptedResponse",
]
