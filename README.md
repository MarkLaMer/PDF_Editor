# PDF_Editor
A simple PDF editor that allows users to add text and add e-signatures. An alternative to the "free" paywall alternatives on the internet. It consists of a Python server built with Flask and a web‑based front end powered by PDF.js and SignaturePad. Users can upload a PDF, click anywhere on the page to add text, type or draw an e‑signature, and export a flattened PDF with all annotations.

## Requirements

The server depends on a handful of Python libraries. You can install them with `pip`:

```bash
pip install Flask PyPDF2 reportlab Pillow
```

You will also need a modern web browser to interact with the editor. The front end loads PDF.js, SignaturePad and a cursive font from public CDNs, so an internet connection is required for those assets.

## Running the application

1. Clone or download this repository.

2. Make sure you are in the `pdf_editor_split` directory.

3. Start the Flask server:
```bash
python app.py
```

4. Open your browser at http://localhost:5000

4. Click Upload PDF and choose a file from your computer. The PDF will render on the page.

5. Click anywhere on a page to enter text. Double‑click the text input to open the signature dialog, where you can type a name (rendered with a cursive font) or draw a signature with your mouse or stylus.

6. When you are finished, click Export PDF to download the annotated PDF. All text and signatures are flattened into the document using the server‑side libraries.

## Custom signature fonts

If you would like your typed signatures to use a specific TrueType font, copy the `.ttf` file into the same directory as `app.py` and rename it to `GreatVibes-Regular.ttf` (or modify `CUSTOM_SIG_FONT` in `app.py` to match your file name). If the font file is not present, the server will fall back to Helvetica when embedding typed signatures.
