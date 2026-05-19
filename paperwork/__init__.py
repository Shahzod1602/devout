"""Paperwork analysis: PDF processing, Gemini calls, BOL ↔ load matching, US Mail support."""
from .api import router as paperwork_api_router
from .gemini import gemini_extract_once, genai_client, parse_gemini_json
from .pdf import fix_image_orientation, pdf_to_images, process_file
from .us_mail import analyze_us_mail_federal_gemini, is_us_mail_load
from .validator import (
    count_stops_by_type,
    determine_file_type,
    format_ratecon_address,
    validate_bol_with_loads_gemini,
)

__all__ = [
    "paperwork_api_router",
    # PDF
    "fix_image_orientation",
    "pdf_to_images",
    "process_file",
    # Gemini
    "genai_client",
    "gemini_extract_once",
    "parse_gemini_json",
    # US Mail
    "is_us_mail_load",
    "analyze_us_mail_federal_gemini",
    # Validator
    "format_ratecon_address",
    "count_stops_by_type",
    "determine_file_type",
    "validate_bol_with_loads_gemini",
]
