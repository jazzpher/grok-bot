"""Bounded text extraction. No macros, scripts, archive unpacking or OCR."""
import io
import pathlib
import zipfile
from pypdf import PdfReader
from defusedxml import ElementTree

MAX_UPLOAD = 1024 * 1024
MAX_TEXT = 32000
ALLOWED = {'.txt', '.md', '.csv', '.json', '.py', '.js', '.html', '.css', '.pdf', '.docx'}

def extract_text(name, raw):
    ext = pathlib.Path(name).suffix.lower()
    if ext not in ALLOWED:
        raise ValueError('Supported: TXT, MD, CSV, JSON, code/text, PDF and DOCX. Images/OCR are not supported yet.')
    if not raw or len(raw) > MAX_UPLOAD:
        raise ValueError('Upload must be 1 byte to 1 MB.')
    if ext == '.pdf':
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise ValueError('Encrypted PDFs are not supported.')
        if len(reader.pages) > 20:
            raise ValueError('PDF limit: 20 pages.')
        text = '\n\n'.join(page.extract_text() or '' for page in reader.pages)
        if not text.strip():
            raise ValueError('No readable text. Scanned PDFs need OCR, which is not supported.')
    elif ext == '.docx':
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info = archive.getinfo('word/document.xml')
            if info.file_size > 2 * MAX_UPLOAD:
                raise ValueError('DOCX text archive exceeds extraction limit.')
            # defusedxml rejects entity expansion and external entities.
            root = ElementTree.fromstring(archive.read(info))
            paragraphs = root.findall('.//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p')
            text = '\n'.join(''.join(p.itertext()) for p in paragraphs)
    else:
        text = raw.decode('utf-8-sig')
        if '\x00' in text:
            raise ValueError('Binary files are not supported.')
    if len(text.encode('utf-8')) > MAX_TEXT:
        raise ValueError('Extracted text exceeds 32 KB. Upload a smaller document.')
    return text
