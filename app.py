"""
Flask server for the PDF editor.

This application exposes three endpoints:

* ``/`` – renders the front end contained in ``index.html``.
* ``/upload`` – accepts an uploaded PDF and stores it temporarily.  Returns a unique
  filename that the client can use to fetch the file via ``/uploads/<filename>``.
* ``/export`` – accepts a set of annotations from the client, applies them to
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
from reportlab.lib.utils import ImageReader
from PyPDF2.generic import NameObject  # for preserving form fields
from PyPDF2.generic import BooleanObject
from werkzeug.utils import secure_filename


# Determine the directory of this file so we can locate other resources relative to it
BASE_DIR = Path(__file__).parent

# Configure Flask to serve static assets (like styles.css) directly from
# the project root.  By setting ``static_folder`` to ``BASE_DIR`` and
# ``static_url_path`` to an empty string, any file placed alongside
# this script (e.g. styles.css) will be available at the URL
# ``/styles.css``.  The template folder remains set to BASE_DIR so
# that index.html is discovered correctly.
app = Flask(__name__,
            template_folder=str(BASE_DIR),
            static_folder=str(BASE_DIR),
            static_url_path='')

# Directory to store uploaded and temporary PDFs
UPLOAD_FOLDER = BASE_DIR / "uploads"
EXPORT_FOLDER = BASE_DIR / "exports"
for folder in (UPLOAD_FOLDER, EXPORT_FOLDER):
    folder.mkdir(parents=True, exist_ok=True)

# Directory to store saved signatures
SIGNATURE_FOLDER = BASE_DIR / "signatures"
SIGNATURE_FOLDER.mkdir(parents=True, exist_ok=True)

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
    original_name = secure_filename(uploaded.filename)
    ext = Path(original_name).suffix.lower()
    if ext != '.pdf':
        return jsonify({'error': 'File must be a PDF'}), 400
    unique_name = f"{uuid.uuid4().hex}{ext}"
    saved_path = UPLOAD_FOLDER / unique_name
    uploaded.save(saved_path)
    # Return both the unique filename and the original human-friendly name
    return jsonify({'filename': unique_name, 'original_name': original_name})


@app.route('/save_signature', methods=['POST'])
def save_signature() -> Any:
    """
    Accepts a base64 data URL representing a signature image, saves it
    into the signatures folder and returns a filename.  The client
    should send a JSON body with a ``dataURL`` field (data:image/png;base64,...).
    """
    data = request.get_json(force=True)
    data_url = data.get('dataURL')
    if not data_url or not isinstance(data_url, str):
        return jsonify({'error': 'No dataURL provided'}), 400
    # Extract the base64 encoded portion of the data URL
    try:
        header, encoded = data_url.split(',', 1)
    except ValueError:
        return jsonify({'error': 'Invalid dataURL'}), 400
    # Decode the image data
    try:
        image_data = base64.b64decode(encoded)
    except Exception:
        return jsonify({'error': 'Failed to decode image data'}), 400
    # Generate a unique filename
    filename = f"{uuid.uuid4().hex}.png"
    file_path = SIGNATURE_FOLDER / filename
    try:
        # Load the image into Pillow and remove any transparency by compositing
        # over a white background.  Some PDF viewers render PNG transparency as
        # black rectangles; flattening to white avoids black boxes in exported PDFs.
        with BytesIO(image_data) as bio:
            with Image.open(bio) as sig_img:
                # Always convert to RGBA to extract an alpha channel if present
                if sig_img.mode != 'RGBA':
                    sig_img = sig_img.convert('RGBA')
                # Composite the signature over a white background.  This removes
                # transparency entirely, preventing black boxes in certain PDF
                # viewers.
                background = Image.new('RGB', sig_img.size, (255, 255, 255))
                background.paste(sig_img, mask=sig_img.split()[-1])
                sig_img = background
                # Save the processed image to disk as a PNG (no alpha channel)
                sig_img.save(file_path, format='PNG')
    except Exception as e:
        return jsonify({'error': f'Failed to save signature: {e}'}), 500
    return jsonify({'filename': filename})


@app.route('/signatures/<path:filename>')
def serve_signature(filename: str) -> Any:
    """Serve a saved signature image from the signatures folder."""
    return send_file(SIGNATURE_FOLDER / filename, mimetype='image/png')


@app.route('/list_signatures')
def list_signatures() -> Any:
    """Return a list of saved signature filenames."""
    files = []
    try:
        files = [f.name for f in SIGNATURE_FOLDER.iterdir() if f.is_file()]
    except Exception:
        pass
    return jsonify({'files': files})


@app.route('/export', methods=['POST'])
def export_pdf() -> Any:
    """
    Apply user annotations to the uploaded PDF and stream back the
    resulting document.  This implementation copies the original pages
    first, preserves form fields (AcroForms), overlays the provided
    text and signature annotations, and then writes the combined
    document to a buffer.  The download name is derived from the
    original uploaded filename (if provided) with ``-edited`` appended
    before the extension.
    """
    # Parse JSON body
    data = request.get_json(force=True) or {}
    src_filename = data.get('filename')
    annotations = data.get('annotations', [])
    original_name = data.get('original_name')
    # Validate source file
    if not src_filename:
        return jsonify({'error': 'filename is required'}), 400
    src_path = UPLOAD_FOLDER / src_filename
    if not src_path.exists():
        return jsonify({'error': 'file not found'}), 404
    # Open the original PDF
    try:
        reader = PdfReader(str(src_path))
    except Exception as e:
        return jsonify({'error': f'Failed to read PDF: {e}'}), 500
    # Instead of cloning the entire document at once, copy and
    # overlay each page individually.  This approach avoids
    # inconsistencies where cloned documents lose their visible
    # content on export, as observed when only form widgets
    # remained.  We iterate through the source pages, apply any
    # annotations, and then add the modified page to the writer.
    ann_by_page: Dict[int, List[Dict[str, Any]]] = {}
    for ann in annotations:
        if ann.get('removed'):
            continue
        try:
            pidx = int(ann.get('pageIndex', 0))
        except Exception:
            pidx = 0
        ann_by_page.setdefault(pidx, []).append(ann)

    writer = PdfWriter()
    num_pages = len(reader.pages)
    for page_index in range(num_pages):
        # Get the page from the reader.  Modifications to this page
        # object do not affect the original file on disk.
        page = reader.pages[page_index]
        # If there are annotations for this page, overlay them.
        page_ann = ann_by_page.get(page_index, [])
        if page_ann:
            # Normalize rotation into content so coordinates align
            try:
                if hasattr(page, 'transfer_rotation_to_content'):
                    page.transfer_rotation_to_content()
            except Exception:
                pass
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            overlay_buf = BytesIO()
            canv = rl_canvas.Canvas(overlay_buf, pagesize=(width, height))
            canv.setFillColorRGB(0, 0, 0)
            canv.setFont('Helvetica', 12)
            for ann in page_ann:
                try:
                    x = float(ann.get('x', 0))
                    y = float(ann.get('y', 0))
                except Exception:
                    x, y = 0.0, 0.0
                y_pdf = height - y
                if ann.get('type') == 'text':
                    text = str(ann.get('value', '') or '')
                    if text:
                        canv.setFont('Helvetica', 12)
                        canv.drawString(x, y_pdf, text)
                elif ann.get('type') == 'signature':
                    sig_data = ann.get('value', {}) or {}
                    sig_type = sig_data.get('type')
                    ann_w = ann.get('width')
                    ann_h = ann.get('height')
                    if sig_type == 'saved':
                        fname = sig_data.get('filename')
                        sig_path = SIGNATURE_FOLDER / fname if fname else None
                        if sig_path and sig_path.exists():
                            try:
                                sig_img = Image.open(sig_path)
                            except Exception:
                                sig_img = None
                            if sig_img is not None:
                                if sig_img.mode != 'RGB':
                                    sig_img = sig_img.convert('RGB')
                                if ann_w and ann_h:
                                    draw_w = float(ann_w)
                                    draw_h = float(ann_h)
                                else:
                                    max_w = width * 0.4
                                    max_h = height * 0.2
                                    img_w, img_h = sig_img.size
                                    scale = min(max_w / img_w, max_h / img_h, 1.0)
                                    draw_w = img_w * scale
                                    draw_h = img_h * scale
                                ir = ImageReader(sig_img)
                                canv.drawImage(ir, x, y_pdf - draw_h, width=draw_w, height=draw_h, mask='auto')
                    elif sig_type == 'typed':
                        text = sig_data.get('text', '')
                        if text:
                            try:
                                canv.setFont('GreatVibes', 32)
                            except Exception:
                                canv.setFont('Helvetica-Oblique', 32)
                            canv.drawString(x, y_pdf, text)
                    else:
                        data_url = sig_data.get('dataURL')
                        if data_url:
                            try:
                                header, encoded = data_url.split(',', 1)
                                image_bytes = base64.b64decode(encoded)
                                sig_img = Image.open(BytesIO(image_bytes))
                                if sig_img.mode != 'RGB':
                                    sig_img = sig_img.convert('RGB')
                                if ann_w and ann_h:
                                    draw_w = float(ann_w)
                                    draw_h = float(ann_h)
                                else:
                                    max_w = width * 0.4
                                    max_h = height * 0.2
                                    img_w, img_h = sig_img.size
                                    scale = min(max_w / img_w, max_h / img_h, 1.0)
                                    draw_w = img_w * scale
                                    draw_h = img_h * scale
                                ir = ImageReader(sig_img)
                                canv.drawImage(ir, x, y_pdf - draw_h, width=draw_w, height=draw_h, mask='auto')
                            except Exception:
                                pass
            canv.save()
            overlay_buf.seek(0)
            try:
                o_reader = PdfReader(overlay_buf)
                o_page = o_reader.pages[0]
                # Align boxes to the base page
                try:
                    o_page.mediabox = page.mediabox
                except Exception:
                    pass
                try:
                    if hasattr(page, 'cropbox'):
                        o_page.cropbox = page.cropbox
                except Exception:
                    pass
                try:
                    if hasattr(page, 'bleedbox') and hasattr(o_page, 'bleedbox'):
                        o_page.bleedbox = page.bleedbox
                except Exception:
                    pass
                try:
                    if hasattr(page, 'trimbox') and hasattr(o_page, 'trimbox'):
                        o_page.trimbox = page.trimbox
                except Exception:
                    pass
                try:
                    if hasattr(page, 'artbox') and hasattr(o_page, 'artbox'):
                        o_page.artbox = page.artbox
                except Exception:
                    pass
                try:
                    page.merge_page(o_page)
                except TypeError:
                    page.merge_page(o_page, over=True)
            except Exception:
                pass
        # Add the (possibly modified) page to the writer
        writer.add_page(page)

    # After adding all pages, copy the original form fields (AcroForm) and
    # set NeedAppearances flag on the writer.  This ensures that
    # interactive form fields remain present and properly rendered in the
    # output.
    try:
        root = reader.trailer.get('/Root')
        if root and '/AcroForm' in root:
            acroform = root['/AcroForm']
            writer._root_object.update({NameObject('/AcroForm'): acroform})
            try:
                writer._root_object['/AcroForm'].update({NameObject('/NeedAppearances'): BooleanObject(True)})
            except Exception:
                pass
    except Exception:
        pass
    # Determine the download filename.  Prefer the original human-friendly name
    # provided by the client; otherwise derive from the internal filename.
    if original_name:
        stem, ext = os.path.splitext(original_name)
        download_name = f"{stem}-edited.pdf"
    else:
        stem, ext = os.path.splitext(src_filename)
        download_name = f"{stem}-edited.pdf"
    # Write PDF to buffer
    out_buf = BytesIO()
    writer.write(out_buf)
    out_buf.seek(0)
    return send_file(
        out_buf,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=download_name
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