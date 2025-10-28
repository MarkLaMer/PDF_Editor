"""
Flask server for the PDF editor.

This application exposes three endpoints:

- renders the front end contained in ``index.html``.
- upload - accepts an uploaded PDF and stores it temporarily.  Returns a unique
  filename that the client can use to fetch the file via ``/uploads/<filename>``.
- export - accepts a set of annotations from the client, applies them to
  the uploaded PDF using PyPDF2 and ReportLab, and streams back the final PDF.

Before running this script, install the required dependencies:

```
pip install Flask PyPDF2 reportlab Pillow
```
"""

import os
import uuid
import base64
from io import BytesIO
from pathlib import Path
from typing import List, Dict, Any

from flask import Flask, request, send_file, jsonify, render_template
from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PIL import Image


# Determine the directory of this file so we can locate other resources relative to it
BASE_DIR = Path(__file__).parent

app = Flask(__name__, template_folder=str(BASE_DIR))

# Directory to store uploaded and temporary PDFs
UPLOAD_FOLDER = BASE_DIR / "uploads"
EXPORT_FOLDER = BASE_DIR / "exports"
for folder in (UPLOAD_FOLDER, EXPORT_FOLDER):
    folder.mkdir(parents=True, exist_ok=True)

# Attempt to register a cursive font for signature rendering.  If the
# specified TrueType file exists in the same directory as this script
# then it will be registered under the name "GreatVibes".  If it does
# not exist, the application will gracefully fall back to standard
# Helvetica when embedding typed signatures into exported PDFs.
CUSTOM_SIG_FONT = "GreatVibes-Regular.ttf"
font_file = BASE_DIR / CUSTOM_SIG_FONT
if font_file.exists():
    try:
        pdfmetrics.registerFont(TTFont("GreatVibes", str(font_file)))
    except Exception:
        pass


@app.route('/')
def index() -> str:
    """Render the main editor page."""
    # Flask will look for a file named 'index.html' in the configured template folder
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload() -> Any:
    """Handle PDF uploads and store them in a temporary folder."""
    uploaded = request.files.get('pdf')
    if not uploaded or uploaded.filename == '':
        return jsonify({'error': 'No file uploaded'}), 400
    # generate a unique filename to avoid clashes
    ext = Path(uploaded.filename).suffix.lower()
    if ext != '.pdf':
        return jsonify({'error': 'File must be a PDF'}), 400
    unique_name = f"{uuid.uuid4().hex}{ext}"
    saved_path = UPLOAD_FOLDER / unique_name
    uploaded.save(saved_path)
    return jsonify({'filename': unique_name})


@app.route('/export', methods=['POST'])
def export_pdf() -> Any:
    """Apply annotations to the PDF and send back the new file."""
    data = request.get_json(force=True)
    filename = data.get('filename')
    annots = data.get('annotations', [])
    pdf_path = UPLOAD_FOLDER / filename if filename else None
    if not pdf_path or not pdf_path.exists():
        return jsonify({'error': 'Original PDF not found'}), 400
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as e:
        return jsonify({'error': f'Failed to read PDF: {e}'}), 500
    writer = PdfWriter()
    # group annotations by page
    annots_by_page: Dict[int, List[Dict[str, Any]]] = {}
    for ann in annots:
        pidx = int(ann.get('pageIndex', 0))
        annots_by_page.setdefault(pidx, []).append(ann)
    for page_index, page in enumerate(reader.pages):
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        # create overlay
        packet = BytesIO()
        c = rl_canvas.Canvas(packet, pagesize=(width, height))
        # set default font
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 12)
        page_annots = annots_by_page.get(page_index, [])
        for ann in page_annots:
            x = float(ann.get('x', 0))
            y = float(ann.get('y', 0))
            # convert y from top‑based DOM to bottom‑based PDF coordinates
            y_pdf = height - y
            if ann['type'] == 'text':
                text = str(ann['value'])
                c.setFont("Helvetica", 12)
                c.drawString(x, y_pdf, text)
            elif ann['type'] == 'signature':
                sig_data = ann['value']
                if sig_data['type'] == 'typed':
                    text = sig_data['text']
                    # draw typed signature using cursive font if available
                    try:
                        c.setFont("GreatVibes", 32)
                    except Exception:
                        c.setFont("Helvetica", 32)
                    c.drawString(x, y_pdf, text)
                else:
                    # drawn signature: decode dataURL
                    data_url = sig_data['dataURL']
                    header, encoded = data_url.split(',', 1)
                    image_data = base64.b64decode(encoded)
                    sig_img = Image.open(BytesIO(image_data))
                    # convert to RGB if needed
                    if sig_img.mode != 'RGB':
                        sig_img = sig_img.convert('RGB')
                    img_path = BytesIO()
                    sig_img.save(img_path, format='PNG')
                    img_path.seek(0)
                    # scale signature down if larger than page area
                    max_width = width * 0.4
                    max_height = height * 0.2
                    img_width, img_height = sig_img.size
                    scale_factor = min(max_width / img_width, max_height / img_height, 1.0)
                    draw_width = img_width * scale_factor
                    draw_height = img_height * scale_factor
                    # draw image; y coordinate refers to bottom of image
                    c.drawImage(img_path, x, y_pdf - draw_height, width=draw_width, height=draw_height, mask='auto')
        c.save()
        packet.seek(0)
        overlay_reader = PdfReader(packet)
        overlay_page = overlay_reader.pages[0]
        # merge overlay into page
        try:
            page.merge_page(overlay_page)
        except Exception:
            # fallback: add page unmodified
            pass
        writer.add_page(page)
    # write out new PDF
    output_buffer = BytesIO()
    writer.write(output_buffer)
    output_buffer.seek(0)
    return send_file(
        output_buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name='edited.pdf'
    )


@app.route('/uploads/<path:filename>')
def serve_uploaded(filename: str) -> Any:
    """Serve uploaded PDF files so that pdf.js can render them."""
    return send_file(UPLOAD_FOLDER / filename)


def main() -> None:
    """Entry point for running the Flask app."""
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=False)


if __name__ == '__main__':
    main()