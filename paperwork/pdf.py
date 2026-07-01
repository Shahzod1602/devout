"""PDF / image processing for paperwork analysis."""
import asyncio
import logging
from io import BytesIO

import fitz  # PyMuPDF
from PIL import Image

logger = logging.getLogger(__name__)

# Hujjat tahlili sifati uchun markaziy konstantalar. AskAI (test/prod/askai.py)
# bilan bir xil tutilishi shart — aks holda bir xil keshlangan PDF ikkala tomonda
# har xil sahifa soni / o'qish sifatida ko'rinib, pageCount sog'lig'ini buzadi.
MAX_PAGES = 8          # PDF dan tahlil qilinadigan sahifalar soni (avval 5 edi — askai bilan tenglashtirildi)
RENDER_DPI = 200       # PDF sahifalarini render qilish (avval Matrix(2,2) ≈ 144 dpi edi)
MAX_LONG_EDGE = 2600   # juda katta renderni cheklab Gemini payload/latency'ni ushlab turish


def fix_image_orientation(img_bytes: bytes) -> bytes:
    """Rasmni to'g'ri holatga keltirish."""
    try:
        img: Image.Image = Image.open(BytesIO(img_bytes))
        if hasattr(img, '_getexif') and img._getexif():
            exif = img._getexif()
            if exif and 274 in exif:
                orientation = exif[274]
                if orientation == 3:
                    img = img.rotate(180, expand=True)
                elif orientation == 6:
                    img = img.rotate(270, expand=True)
                elif orientation == 8:
                    img = img.rotate(90, expand=True)
        width, height = img.size
        if width > height * 1.3:
            img = img.rotate(90, expand=True)
        output = BytesIO()
        img.save(output, format='PNG')
        return output.getvalue()
    except Exception:
        # Rasm formati buzilgan yoki PIL'da noma'lum xato — original byte'larni qaytaramiz.
        logger.debug("fix_image_orientation failed, returning original bytes", exc_info=True)
        return img_bytes


def pdf_to_images(pdf_bytes: bytes, max_pages: int = MAX_PAGES) -> list:
    """PDF ning birinchi `max_pages` sahifasini rasmlarga aylantirish (RENDER_DPI da)."""
    images = []
    pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page_num in range(min(len(pdf_doc), max_pages)):
            page = pdf_doc.load_page(page_num)
            pix = page.get_pixmap(dpi=RENDER_DPI, alpha=False)
            pil_img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            # Yuqori DPI render payload'ni shishirmasligi uchun uzun tomonni cheklaymiz.
            long_edge = max(pil_img.size)
            if long_edge > MAX_LONG_EDGE:
                scale = MAX_LONG_EDGE / long_edge
                pil_img = pil_img.resize((round(pil_img.width * scale), round(pil_img.height * scale)))
            buf = BytesIO()
            pil_img.save(buf, format="PNG")
            img_bytes = buf.getvalue()
            img_bytes = fix_image_orientation(img_bytes)
            images.append(img_bytes)
    finally:
        # close() finally'da — render xatosida ham fitz hujjati oqib ketmasin.
        pdf_doc.close()
    return images


async def process_file(file_bytes: bytes, filename: str, max_pages: int = MAX_PAGES) -> list:
    """Faylni rasmga aylantirish (PDF → ko'p sahifa, image → 1 ta).

    Render (PyMuPDF/PIL) — CPU-og'ir, bloklaydigan ish. Event loop'ni (aiogram +
    FastAPI) muzlatmaslik uchun ishchi thread'ga (run_in_executor) o'tkazamiz.
    """
    loop = asyncio.get_running_loop()
    if filename.lower().endswith('.pdf'):
        return await loop.run_in_executor(None, pdf_to_images, file_bytes, max_pages)
    img = await loop.run_in_executor(None, fix_image_orientation, file_bytes)
    return [img]
