"""Deterministic pixel-diff verification (Pillow only; no network, no model calls).

The verifier half of a verifiable agent loop: a number, not an opinion. Compares
a ground-truth image against an output image and returns the mismatch ratio,
optionally writing a highlighted diff image for inspection. Used by the
``verify_visual`` tool and by durable-task ``pixel_diff`` checks.

Comparison rules (deterministic):
* Images are placed on a union canvas anchored at the top-left; area present on
  only one side counts as differing, area absent on both sides is not compared.
* A pixel differs when any RGB channel differs by more than ``tolerance``
  (0-255, default 0 = exact).
* ``ignore`` rectangles ([x, y, width, height], ground-truth coordinates) are
  excluded from both the differing and the compared counts - the documented
  escape hatch for known-irrelevant noise such as font rendering.
"""
import pathlib

MAX_IMAGE_BYTES = 10 * 1024 * 1024     # read cap per image (matches GROK_MAX_FILE_BYTES default)
MAX_DIMENSION = 4000                   # px cap per side, keeps diff memory bounded
MAX_IGNORE_REGIONS = 8
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}
HTML_SUFFIXES = {'.html', '.htm'}


class VisualVerifyError(ValueError):
    """Inputs could not be compared; the message is safe to show to model and user."""


def is_html_name(name):
    return pathlib.PurePath(str(name)).suffix.lower() in HTML_SUFFIXES


def _pil():
    try:
        from PIL import Image, ImageChops, ImageDraw
        return Image, ImageChops, ImageDraw
    except ImportError as exc:
        raise VisualVerifyError('Pixel comparison needs Pillow installed: pip install pillow.') from exc


def _load(source):
    """source is a filesystem path or bytes; returns an RGB image."""
    Image, _, _ = _pil()
    import io
    if isinstance(source, (bytes, bytearray)):
        if len(source) > MAX_IMAGE_BYTES:
            raise VisualVerifyError('Image exceeds the %d MB limit.' % (MAX_IMAGE_BYTES // (1024 * 1024)))
        raw = io.BytesIO(bytes(source))
    else:
        p = pathlib.Path(source)
        if not p.is_file() or p.stat().st_size > MAX_IMAGE_BYTES:
            raise VisualVerifyError('Image missing or exceeds the %d MB limit: %s'
                                    % (MAX_IMAGE_BYTES // (1024 * 1024), p.name))
        raw = p
    try:
        img = Image.open(raw)
        img.load()
    except Exception as exc:
        raise VisualVerifyError('Not a readable image (%s).' % type(exc).__name__) from exc
    if img.width > MAX_DIMENSION or img.height > MAX_DIMENSION:
        raise VisualVerifyError('Image exceeds the %d px dimension limit.' % MAX_DIMENSION)
    return img.convert('RGB')


def validate_ignore(ignore):
    """Normalize ignore rectangles to (x, y, w, h) int tuples; raises on bad shape."""
    if ignore is None:
        return []
    if not isinstance(ignore, (list, tuple)) or len(ignore) > MAX_IGNORE_REGIONS:
        raise VisualVerifyError('ignore must be a list of up to %d [x, y, width, height] rectangles.'
                                % MAX_IGNORE_REGIONS)
    rects = []
    for rect in ignore:
        if (not isinstance(rect, (list, tuple)) or len(rect) != 4
                or any(type(v) is not int for v in rect)):
            raise VisualVerifyError('Each ignore rectangle must be [x, y, width, height] of integers.')
        x, y, w, h = rect
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            raise VisualVerifyError('Ignore rectangles need x >= 0, y >= 0, width > 0, height > 0.')
        rects.append((x, y, w, h))
    return rects


def validate_threshold(value, name='threshold'):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
        raise VisualVerifyError(name + ' must be a number from 0 to 1 (a mismatch ratio, e.g. 0.01 = 1%).')
    return float(value)


def validate_tolerance(value):
    if isinstance(value, bool) or type(value) is not int or not 0 <= value <= 255:
        raise VisualVerifyError('tolerance must be an integer from 0 to 255 (per-channel difference).')
    return value


def _channel_mask(diff, tolerance):
    """255 where any RGB channel exceeds tolerance."""
    Image, ImageChops, _ = _pil()
    r, g, b = diff.split()
    cutoff = lambda v: 255 if v > tolerance else 0
    return ImageChops.lighter(ImageChops.lighter(r.point(cutoff), g.point(cutoff)), b.point(cutoff))


def _count(mask):
    return mask.histogram()[255]


def diff_images(ground_truth, output, *, tolerance=0, ignore=(), diff_path=None):
    """Compare two images; returns a result dict (see module docstring for rules).

    ground_truth/output: filesystem paths or PNG/JPEG bytes.
    diff_path: optional filesystem path for the highlighted diff PNG.
    """
    Image, ImageChops, ImageDraw = _pil()
    tolerance = validate_tolerance(tolerance)
    rects = validate_ignore(ignore)
    gt, out = _load(ground_truth), _load(output)
    w, h = max(gt.width, out.width), max(gt.height, out.height)
    size_match = gt.size == out.size

    gt_canvas = Image.new('RGB', (w, h))
    gt_canvas.paste(gt, (0, 0))
    out_canvas = Image.new('RGB', (w, h))
    out_canvas.paste(out, (0, 0))
    present_gt = Image.new('L', (w, h), 0)
    present_gt.paste(255, (0, 0, gt.width, gt.height))
    present_out = Image.new('L', (w, h), 0)
    present_out.paste(255, (0, 0, out.width, out.height))

    # Content differences where both sides have pixels.
    both = ImageChops.multiply(present_gt, present_out)
    content_diff = ImageChops.darker(_channel_mask(ImageChops.difference(gt_canvas, out_canvas), tolerance), both)
    # Region present on exactly one side is always a difference.
    missing = ImageChops.difference(present_gt, present_out)
    differing = ImageChops.lighter(content_diff, missing)
    compared = ImageChops.lighter(present_gt, present_out)   # area with content on at least one side

    ignored_pixels = 0
    if rects:
        ignore_mask = Image.new('L', (w, h), 0)
        draw = ImageDraw.Draw(ignore_mask)
        for x, y, rw, rh in rects:
            draw.rectangle([x, y, x + rw - 1, y + rh - 1], fill=255)
        keep = ImageChops.invert(ignore_mask)
        ignored_pixels = _count(ImageChops.darker(compared, ignore_mask))
        differing = ImageChops.darker(differing, keep)
        compared = ImageChops.darker(compared, keep)

    compared_pixels = _count(compared)
    different_pixels = _count(differing)
    if compared_pixels == 0:
        raise VisualVerifyError('Nothing left to compare: ignore regions cover the entire compared area.')
    ratio = different_pixels / compared_pixels

    if diff_path is not None:
        dim = gt_canvas.point(lambda v: v // 2)
        red = Image.new('RGB', (w, h), (255, 0, 0))
        Image.composite(red, dim, differing).save(diff_path, format='PNG')

    return {'mismatch_ratio': ratio,
            'mismatch_percent': round(ratio * 100, 4),
            'different_pixels': different_pixels,
            'compared_pixels': compared_pixels,
            'ignored_pixels': ignored_pixels,
            'ground_truth_size': [gt.width, gt.height],
            'output_size': [out.width, out.height],
            'size_match': size_match,
            'diff_path': str(diff_path) if diff_path is not None else None}
