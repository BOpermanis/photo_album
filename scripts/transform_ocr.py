# Run this locally before you waste time deploying a full database
import argparse
import warnings
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from skimage.filters import threshold_sauvola
from skimage.morphology import remove_small_objects
from transformers import TrOCRProcessor, VisionEncoderDecoderModel
from transformers.utils import logging as hf_logging

warnings.filterwarnings("ignore", message="Parameter `min_size` is deprecated")


def segment_lines(path: Path, margin: float = 0.06, min_gap: int = 12,
                  min_height: int = 20, pad: int = 12):
	"""TrOCR reads one line at a time, so split the photo into per-line crops."""
	full = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
	w, h = full.size
	# Drop the torn photo border so it isn't mistaken for text.
	inner = full.crop((int(w * margin), int(h * margin),
	                    int(w * (1 - margin)), int(h * (1 - margin))))

	gray = np.asarray(ImageOps.autocontrast(inner.convert("L")), dtype=np.float32)
	window = max(3, (min(gray.shape) // 20) | 1)
	# r=128 is required for float input; the default over-segments to a full mask.
	text = gray < threshold_sauvola(gray, window_size=window, k=0.2, r=128)
	text = remove_small_objects(text, min_size=64)

	rows = np.where(text.sum(axis=1) > text.shape[1] * 0.01)[0]
	if rows.size == 0:
		return [inner]

	bands = np.split(rows, np.where(np.diff(rows) > min_gap)[0] + 1)
	iw, ih = inner.size
	crops = []
	for band in bands:
		y0, y1 = band[0], band[-1]
		if y1 - y0 < min_height:
			continue
		cols = np.where(text[y0:y1 + 1].sum(axis=0) > 0)[0]
		if cols.size == 0:
			continue
		box = (max(0, cols[0] - pad), max(0, y0 - pad),
		       min(iw, cols[-1] + pad), min(ih, y1 + pad))
		crops.append(inner.crop(box))
	return crops or [inner]


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument(
		"image", nargs="?", default="test_data/PXL_20260929_123152874.jpg", type=Path
	)
	args = parser.parse_args()

	if not args.image.is_file():
		parser.error(f"image not found: {args.image}")

	hf_logging.set_verbosity_error()
	processor = TrOCRProcessor.from_pretrained(
		"microsoft/trocr-base-handwritten", use_fast=False
	)
	model = VisionEncoderDecoderModel.from_pretrained("microsoft/trocr-base-handwritten")

	for crop in segment_lines(args.image):
		pixel_values = processor(crop, return_tensors="pt").pixel_values
		generated_ids = model.generate(pixel_values)
		print(processor.batch_decode(generated_ids, skip_special_tokens=True)[0])


if __name__ == "__main__":
	main()