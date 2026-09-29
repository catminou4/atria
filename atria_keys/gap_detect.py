"""CV-based slider gap detection.

The real Alibaba NC slider encodes the cutout position in the puzzle
image, not the DOM/track. We edge-detect the background and (when
available) the puzzle piece, then locate the cutout x-offset by template
matching or, failing that, vertical-edge density on the cutout outline —
thresholding on the cutout's border/shadow, not its color.

Every detection returns a confidence; the caller retries with a fresh
widget on low confidence and never falls back to a guessed distance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger("atria_keys.gap_detect")

# Below this, the caller reloads the widget and tries again.
CONFIDENCE_ACCEPT = 0.55
CONFIDENCE_TEMPLATE_ACCEPT = 0.45


@dataclass
class GapDetection:
    gap_x: float
    confidence: float
    method: str


def image_is_blank(img_bytes: bytes) -> bool:
    """Unpainted element screenshots and failed fetches yield near-uniform
    images — detection on them fabricates confident garbage, so callers
    must refuse instead of trusting a gap found in noise."""
    arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    return img is None or float(img.std()) < 20.0


def image_width(img_bytes: bytes) -> int | None:
    arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return int(img.shape[1]) if img is not None else None


def _decode(img_bytes: bytes) -> np.ndarray:
    arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("could not decode image bytes")
    return img


def _edges(img: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    return cv2.Canny(gray, 60, 160)


def detect_with_piece(bg_bytes: bytes, piece_bytes: bytes) -> GapDetection:
    """Template-match the puzzle piece against the background cutout."""
    bg = _decode(bg_bytes)
    piece = _decode(piece_bytes)
    bg_e = _edges(bg)
    piece_e = _edges(piece)
    if piece_e.shape[0] > bg_e.shape[0] or piece_e.shape[1] > bg_e.shape[1]:
        return GapDetection(0.0, 0.0, "template")
    res = cv2.matchTemplate(bg_e, piece_e, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    return GapDetection(float(max_loc[0]), float(max_val), "template")


def _content_mask(piece: np.ndarray) -> np.ndarray | None:
    """Mask of the piece's real content. Pieces with an alpha channel are
    exact (alpha>0); opaque pieces fall back to 'different from the most
    common corner colour'."""
    if piece.shape[2] == 4:
        return piece[:, :, 3] > 10
    gray = cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY)
    border = np.concatenate(
        [gray[0], gray[-1], gray[:, 0], gray[:, -1]]
    )
    bg_tone = float(np.median(border))
    return np.abs(gray.astype(np.float32) - bg_tone) > 24


def detect_sparse_piece(bg_bytes: bytes, piece_bytes: bytes) -> GapDetection:
    """Sparse-content piece (e.g. a swirl or speckles on transparency):
    the cut strip's content is the ORIGINAL pixels, removed from the
    background which was then inpainted. The hole is the column window
    whose texture is missing relative to its neighbours — computed only
    over the mask's own rows and columns, not the whole strip."""
    bg = _decode(bg_bytes)
    pc = cv2.imdecode(np.frombuffer(piece_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if pc is None:
        return GapDetection(0.0, 0.0, "sparse")
    bg = bg[:, :, :3]
    mask = _content_mask(pc)
    h, w = bg.shape[:2]
    pw = pc.shape[1]
    if pc.shape[0] < h:
        mask = np.vstack([mask, np.zeros((h - pc.shape[0], pw), bool)])
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if not rows.any() or not cols.any():
        return GapDetection(0.0, 0.0, "sparse")
    y0, y1 = int(np.argmax(rows)), int(len(rows) - np.argmax(rows[::-1]))
    c0, c1 = int(np.argmax(cols)), int(len(cols) - np.argmax(cols[::-1]))
    gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    tex = np.sqrt(gx * gx + gy * gy)
    band = tex[y0:y1]
    cw = max(4, c1 - c0)
    margin = max(12, cw // 2)
    scores = []
    xs = []
    for x in range(0, w - pw):
        hx0, hx1 = x + c0, x + c1
        inside = band[:, hx0:hx1].mean()
        lseg = band[:, max(0, hx0 - margin):hx0]
        rseg = band[:, hx1:hx1 + margin]
        left = lseg.mean() if lseg.size else inside
        right = rseg.mean() if rseg.size else inside
        xs.append(x)
        scores.append((left + right) / 2.0 - inside)
    if not xs:
        return GapDetection(0.0, 0.0, "sparse")
    scores = np.asarray(scores)
    # Missing-texture dip is the whole signal: the hole was inpainted,
    # so the piece's original pixels match nothing — template scores
    # only add red-on-red false positives.
    best_i = int(np.argmax(scores))
    median = float(np.median(scores)) + 1e-9
    conf = min(1.0, max(0.0, (float(scores[best_i]) - median) / (abs(median) + 4.0)))
    return GapDetection(float(xs[best_i]), conf, "sparse")


def _is_strip(bg: np.ndarray, piece: np.ndarray) -> bool:
    """Any full-height piece narrower than the background — strip widths
    vary between widget variants (23px on 'qst', ~100px on chunkier
    cutouts) and seam-continuity degrades gracefully on wide pieces."""
    return (
        piece.shape[0] >= bg.shape[0] * 0.8
        and piece.shape[1] < bg.shape[1] - 8
    )


def detect_strip_seam(bg_bytes: bytes, piece_bytes: bytes) -> GapDetection:
    """Full-height strip variant: the strip's left/right edge columns must
    visually continue the background columns on each side of the seam —
    template matching is useless because the hole was inpainted."""
    bg = _decode(bg_bytes).astype(np.float32)
    piece = _decode(piece_bytes).astype(np.float32)
    h, w = bg.shape[:2]
    pw = piece.shape[1]
    if piece.shape[0] != h:
        piece = cv2.resize(piece, (pw, h), interpolation=cv2.INTER_LINEAR)
    p_left = piece[:, 0, :]
    p_right = piece[:, -1, :]
    lo = max(1, int(w * 0.1))
    hi = w - pw - 1
    if hi <= lo:
        return GapDetection(0.0, 0.0, "strip")
    scores = np.empty(hi - lo, dtype=np.float64)
    for i, x in enumerate(range(lo, hi)):
        scores[i] = (
            np.abs(bg[:, x - 1, :] - p_left).mean()
            + np.abs(bg[:, x + pw, :] - p_right).mean()
        )
    best_i = int(np.argmin(scores))
    best = float(scores[best_i])
    median = float(np.median(scores)) + 1e-6
    # Lower is better; confidence from the gap below the typical score.
    confidence = min(1.0, max(0.0, (median - best) / median * 3.0))
    return GapDetection(float(best_i + lo), confidence, "strip")


def detect_cutout_only(bg_bytes: bytes, min_x_frac: float = 0.3,
                       piece_w: int = 46) -> GapDetection:
    """Locate the cutout by its distinctive vertical-edge pair (border +
    drop shadow), ignoring colour entirely. The pair is ~piece_w apart;
    the gap is the LEFT edge."""
    bg = _decode(bg_bytes)
    e = _edges(bg)
    h, w = e.shape
    colsum = e.astype(np.float64).sum(axis=0)
    kernel = np.ones(7) / 7
    smooth = np.convolve(colsum, kernel, mode="same")
    lo = int(w * min_x_frac)
    region = smooth[lo:]
    if region.max() <= 0:
        return GapDetection(0.0, 0.0, "cutout")
    # A cutout's two vertical borders sit ~piece_w apart: score x by the
    # weaker of the two edges — argmax is the LEFT border of the cutout.
    span = w - piece_w
    if span <= lo:
        return GapDetection(0.0, 0.0, "cutout")
    pair = np.minimum(smooth[:span], smooth[piece_w:w])
    seg = pair[lo:]
    peak = int(np.argmax(seg)) + lo
    median = float(np.median(seg)) + 1e-6
    peakiness = float(seg.max() / median)
    confidence = min(1.0, max(0.0, (peakiness - 2.0) / 6.0))
    return GapDetection(float(peak), confidence, "cutout")


def detect_gap_x(
    bg_bytes: bytes, piece_bytes: bytes | None = None
) -> GapDetection:
    """Full fallback chain inside the image domain: strip-seam analysis
    for the full-height-strip variant, template match for small cutout
    pieces, else cutout-outline analysis."""
    if image_is_blank(bg_bytes):
        return GapDetection(0.0, 0.0, "unresolved")
    if piece_bytes:
        try:
            bg = _decode(bg_bytes)
            pc4 = cv2.imdecode(
                np.frombuffer(piece_bytes, np.uint8), cv2.IMREAD_UNCHANGED
            )
            if pc4 is None:
                return GapDetection(0.0, 0.0, "unresolved")
            piece = pc4[:, :, :3] if pc4.shape[2] >= 3 else _decode(piece_bytes)
        except ValueError:
            return GapDetection(0.0, 0.0, "unresolved")
        if float(_content_mask(pc4).mean()) < 0.5:
            # Sparse-content piece: its pixels are the ORIGINAL cut content
            # on a mostly-transparent strip — seam/template routines key on
            # the empty edges and fabricate confident garbage.
            det = detect_sparse_piece(bg_bytes, piece_bytes)
            if det.confidence >= CONFIDENCE_TEMPLATE_ACCEPT:
                return det
            log.info(
                "sparse detection weak (%.2f) — trying classic chain",
                det.confidence,
            )
        if _is_strip(bg, piece):
            det = detect_strip_seam(bg_bytes, piece_bytes)
            if det.confidence >= CONFIDENCE_TEMPLATE_ACCEPT:
                return det
            log.info(
                "strip seam weak (%.2f) — trying cutout analysis",
                det.confidence,
            )
        else:
            det = detect_with_piece(bg_bytes, piece_bytes)
            if det.confidence >= CONFIDENCE_TEMPLATE_ACCEPT:
                return det
            log.info(
                "template match weak (%.2f) — trying cutout analysis",
                det.confidence,
            )
    return detect_cutout_only(bg_bytes)
