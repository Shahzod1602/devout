"""PDF / image processing for paperwork analysis."""
import logging
from io import BytesIO

import fitz  # PyMuPDF
from PIL import Image

logger = logging.getLogger(__name__)


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


def pdf_to_images(pdf_bytes: bytes) -> list:
    """PDF ning birinchi 5 sahifasini rasmlarga aylantirish."""
    images = []
    pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page_num in range(min(len(pdf_doc), 5)):
        page = pdf_doc.load_page(page_num)
        mat = fitz.Matrix(2, 2)
        pix = page.get_pixmap(matrix=mat)
        mode = "RGBA" if pix.alpha else "RGB"
        pil_img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        if pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")
        buf = BytesIO()
        pil_img.save(buf, format="PNG")
        img_bytes = buf.getvalue()
        img_bytes = fix_image_orientation(img_bytes)
        images.append(img_bytes)
    pdf_doc.close()
    return images


async def process_file(file_bytes: bytes, filename: str) -> list:
    """Faylni rasmga aylantirish (PDF → ko'p sahifa, image → 1 ta)."""
    if filename.lower().endswith('.pdf'):
        return pdf_to_images(file_bytes)
    return [fix_image_orientation(file_bytes)]
