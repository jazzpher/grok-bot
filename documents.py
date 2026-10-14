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


W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
MAX_DOCX_PARAGRAPHS = 500


def docx_paragraphs(text):
    """Paragraph strings a .docx built from text will contain (blank lines kept)."""
    text = ''.join(ch for ch in text if ch in '\n\r\t' or ord(ch) >= 32)
    return [line[2:] if line.startswith('# ') else line for line in text.replace('\r\n', '\n').split('\n')]


def build_docx(text):
    """Deterministic minimal Word document from plain text. Stdlib only, no macros.
    One paragraph per line; a line starting with '# ' becomes a bold, larger heading."""
    from xml.sax.saxutils import escape
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Document text must not be empty.')
    text = ''.join(ch for ch in text if ch in '\n\r\t' or ord(ch) >= 32)
    lines = text.replace('\r\n', '\n').split('\n')
    if len(lines) > MAX_DOCX_PARAGRAPHS:
        raise ValueError('Documents are limited to %d lines.' % MAX_DOCX_PARAGRAPHS)
    body = []
    for line in lines:
        props = ''
        if line.startswith('# '):
            line, props = line[2:], '<w:rPr><w:b/><w:sz w:val="32"/></w:rPr>'
        body.append('<w:p><w:r>%s<w:t xml:space="preserve">%s</w:t></w:r></w:p>' % (props, escape(line)))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="%s"><w:body>%s</w:body></w:document>' % (W_NS, ''.join(body)))
    parts = [
        ('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="xml" ContentType="application/xml"/>'
         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
         '</Types>'),
        ('_rels/.rels', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
         '</Relationships>'),
        ('word/document.xml', document),
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, xml in parts:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))  # fixed time: same input, same bytes
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, xml.encode('utf-8'))
    return buf.getvalue()
