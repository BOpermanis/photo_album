# Family Photo Face-Tagging

Take photos with your Android phone's native camera, upload them to your laptop over
Wi-Fi from the phone's browser, and tag detected faces with names in a local web app.

- **Backend:** Python + FastAPI, SQLite for tags, originals kept full-res on disk.
- **Face detection:** InsightFace / RetinaFace (ONNX, CPU) — draws boxes; you type names.
- **Frontend:** vanilla HTML/JS served by FastAPI (no build step).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The first run downloads the InsightFace detection model automatically.

## Run

```bash
python run.py
```

On startup it prints a QR code and a URL like `http://192.168.x.x:8000`.

## Use it from your phone

1. Make sure the phone and laptop are on the **same Wi-Fi**.
2. Scan the QR code (or open the printed URL) in the phone's browser.
3. Go to **Upload**, pick photos from your gallery, and upload (full resolution).
4. On the laptop (or phone), open **Photos** → a photo → click a face box and type a
   name. Names autocomplete across photos. Use **Not a face** to remove a false detection.
5. **People** lists everyone you've tagged and their photos.

Data lives in `data/` (`library/` = originals, `app.db` = tags).
