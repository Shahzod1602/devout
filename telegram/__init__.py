"""Aiogram handlers — registered on a Router and included into the Dispatcher in bot.py."""
from .handlers import router as telegram_router

__all__ = ["telegram_router"]
