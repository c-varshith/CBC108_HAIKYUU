"""Upload handling: validate, normalise EXIF orientation, strip metadata (GPS), make a ~1600 px display copy.

Bounding boxes are always expressed in the pixel space of the *normalised original*: after EXIF rotation,
before the display downscale. The client scales them by image.width / displayed width."""
from __future__ import annotations

import asyncio
import hashlib
import io
from dataclasses import dataclass

import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import Settings

pillow_heif.register_heif_opener()
_ALLOWED = {"JPEG", "MPO", "PNG", "WEBP", "HEIF"}  # MPO: multi-picture JPEGs some phones write
_sem = asyncio.Semaphore(2)  # a 50 MP decode is ~150 MB of RAM: cap concurrency


class ImageRejected(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class ProcessedImage:
    original: bytes
    display: bytes
    width: int
    height: int


def _open(data: bytes, max_pixels: float) -> Image.Image:
    try:
        im = Image.open(io.BytesIO(data))
        fmt, (w, h) = im.format, im.size
    except Image.DecompressionBombError:
        raise ImageRejected(413, "Photo is too large for the server.")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise ImageRejected(422, "Could not read this image. Use a JPG, PNG, WebP or HEIC photo.")
    if fmt not in _ALLOWED:
        raise ImageRejected(415, "Use a JPG, PNG, WebP or HEIC photo.")
    if w < 16 or h < 16:
        raise ImageRejected(422, "This image is too small to use.")
    if w * h > max_pixels:  # checked before decoding any pixels
        raise ImageRejected(413, f"Photo is over {max_pixels / 1e6:g} megapixels.")
    return im


def _jpeg(im: Image.Image, quality: int, icc: bytes | None, progressive: bool = False) -> bytes:
    buf = io.BytesIO()
    kw = {"icc_profile": icc} if icc else {}
    im.save(buf, "JPEG", quality=quality, progressive=progressive, **kw)  # no exif= argument: metadata is dropped
    return buf.getvalue()


def _normalise(im: Image.Image) -> tuple[Image.Image, bytes | None]:
    try:
        im.load()
    except (OSError, SyntaxError, ValueError):
        raise ImageRejected(422, "This image looks damaged or incomplete.")
    icc = im.info.get("icc_profile")
    im = ImageOps.exif_transpose(im)
    return im.convert("RGB"), icc


def process_scan_image(data: bytes, s: Settings) -> ProcessedImage:
    im, icc = _normalise(_open(data, s.max_megapixels * 1e6))
    w, h = im.size
    disp = im
    if max(w, h) > s.display_max_side:
        disp = im.copy()
        disp.thumbnail((s.display_max_side, s.display_max_side), Image.Resampling.LANCZOS)
    return ProcessedImage(_jpeg(im, 92, icc), _jpeg(disp, 85, icc, progressive=True), w, h)


def process_reference(data: bytes) -> tuple[bytes, bytes]:
    """Reference photo for a product: <=512 px JPEG for embedding, 96 px square thumbnail for the catalog UI."""
    im, _ = _normalise(_open(data, 40e6))
    ref = im.copy()
    ref.thumbnail((512, 512), Image.Resampling.LANCZOS)
    thumb = ImageOps.fit(im, (96, 96), Image.Resampling.LANCZOS)
    return _jpeg(ref, 90, None), _jpeg(thumb, 80, None)


async def aprocess_scan_image(data: bytes, s: Settings) -> ProcessedImage:
    async with _sem:
        return await asyncio.to_thread(process_scan_image, data, s)


async def aprocess_reference(data: bytes) -> tuple[bytes, bytes]:
    async with _sem:
        return await asyncio.to_thread(process_reference, data)


# ---------------------------------------------------------------- catalog: hashes, quality gate, shelf crops
@dataclass
class ImageInfo:
    width: int  # after EXIF orientation
    height: int
    sha256: str  # of the bytes as received
    phash: str  # 64-bit difference hash, 16 hex chars
    flat: bool  # (almost) one colour: blank image or "no image" placeholder


def perceptual_hash(im: Image.Image) -> str:
    """64-bit difference hash: survives re-encoding and resizing, so the same photo from two sources collides.
    A blank image hashes to all zeros, which is why blank placeholders are also caught by the duplicate check."""
    g = im.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | int(px[row * 9 + col] > px[row * 9 + col + 1])
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _is_flat(im: Image.Image) -> bool:
    g = im.convert("L").resize((64, 64), Image.Resampling.BILINEAR)
    hist = g.histogram()
    bins = [sum(hist[i:i + 16]) for i in range(0, 256, 16)]  # 16 grey levels per bin
    return max(bins) / (64 * 64) > 0.97


def inspect_image(data: bytes) -> ImageInfo:
    """Decode once and fingerprint. Raises ImageRejected for unreadable / unsupported / oversized files."""
    im, _ = _normalise(_open(data, 40e6))
    return ImageInfo(im.width, im.height, hashlib.sha256(data).hexdigest(), perceptual_hash(im), _is_flat(im))


async def ainspect_image(data: bytes) -> ImageInfo:
    async with _sem:
        return await asyncio.to_thread(inspect_image, data)


def crop_region(data: bytes, bbox: tuple[float, float, float, float], pad: float = 0.04, min_side: int = 32) -> bytes:
    """JPEG crop of a detection box (pixels of the normalised original) with a small margin, the same one the worker uses."""
    im, _ = _normalise(_open(data, 60e6))
    x1, y1, x2, y2 = bbox
    w, h = x2 - x1, y2 - y1
    box = (max(0, int(x1 - w * pad)), max(0, int(y1 - h * pad)), min(im.width, int(x2 + w * pad)), min(im.height, int(y2 + h * pad)))
    if box[2] - box[0] < min_side or box[3] - box[1] < min_side:
        raise ImageRejected(422, f"This box is too small to use as a reference photo (needs at least {min_side} px on each side).")
    return _jpeg(im.crop(box), 95, None)


async def acrop_region(data: bytes, bbox: tuple[float, float, float, float], **kw) -> bytes:
    async with _sem:
        return await asyncio.to_thread(crop_region, data, bbox, **kw)
