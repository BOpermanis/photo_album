#!/usr/bin/env python3
"""Run kraken OCR on an image and print / save the recognized text.

Uses kraken's high-level task API (kraken >= 7, see
https://kraken.re/main/api/index.html):

  * ``SegmentationTaskModel`` — baseline + line detection (default BLLA model).
  * ``RecognitionTaskModel``  — text recognition with a downloaded ATR model.

The recognition model is not bundled with kraken; if ``--model`` is not given the
script downloads a general model from the kraken model zoo on first use and
caches it under the user's kraken directory.

Example:
    python scripts/kraken_ocr.py test_data/PXL_20260929_123152874.jpg
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

from kraken.tasks import RecognitionTaskModel, SegmentationTaskModel
from kraken.configs import RecognitionInferenceConfig, SegmentationInferenceConfig

try:  # optional HEIC support, matching the rest of the project
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # noqa: BLE001 - pillow_heif is an optional dependency
    pass

# CATMuS-Print (Large): general diachronic Latin-script recognition model from
# the kraken model zoo (model_type=recognition, script=Latn).
DEFAULT_RECOGNITION_MODEL = "10.5281/zenodo.10592716"


def resolve_recognition_model(model: str | None) -> str:
    """Return a local path to a recognition model.

    A local file path is used as-is; otherwise the given (or default) model
    identifier is fetched from the kraken model zoo and cached locally. The zoo
    stores each record as a directory, so the actual weights file inside it is
    located afterwards.
    """
    if model and Path(model).exists():
        return model

    from htrmopo import get_model
    from kraken.lib.progress import KrakenDownloadProgressBar

    model_id = model or DEFAULT_RECOGNITION_MODEL
    print(f"Fetching recognition model {model_id} from the kraken model zoo...")
    with KrakenDownloadProgressBar() as progress:
        task = progress.add_task("Downloading", total=0, visible=True)
        model_dir = get_model(
            model_id,
            callback=lambda total, advance: progress.update(task, total=total, advance=advance),
        )
    candidates = [p for p in Path(model_dir).iterdir() if p.suffix in (".mlmodel", ".safetensors")]
    if not candidates:
        raise SystemExit(f"no recognition model file found in {model_dir}")
    return str(candidates[0])


def ocr(image_path: Path, recognition_model: str | None) -> list[str]:
    """Segment and recognize text in ``image_path``; return one string per line."""
    im = Image.open(image_path)

    seg_model = SegmentationTaskModel.load_model()
    segmentation = seg_model.predict(im, SegmentationInferenceConfig())

    rec_model = RecognitionTaskModel.load_model(resolve_recognition_model(recognition_model))
    rec_config = RecognitionInferenceConfig()

    return [record.prediction for record in rec_model.predict(im, segmentation, rec_config)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run kraken OCR on an image.")
    parser.add_argument(
            "image", nargs="?", default="test_data/PXL_20260929_123152874.jpg", type=Path
        )
    parser.add_argument(
        "-m",
        "--model",
        help="Recognition model: a local path or a kraken model-zoo identifier. "
        "Defaults to a general Latin-script model that is downloaded on first use.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Optional path to write the recognized text to (one line per text line).",
    )
    args = parser.parse_args(argv)

    if not args.image.exists():
        parser.error(f"image not found: {args.image}")

    lines = ocr(args.image, args.model)
    text = "\n".join(lines)

    print("\n--- Recognized text ---")
    print(text if text else "(no text detected)")

    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"\nWrote {len(lines)} line(s) to {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
