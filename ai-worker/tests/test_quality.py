import numpy as np

from app.config import Settings
from app.quality import crop_flags, glare_fraction, laplacian_variance


def test_sharp_beats_blurry():
    rng = np.random.default_rng(0)
    sharp = rng.integers(0, 255, (96, 96)).astype(np.float32)
    blur = np.full((96, 96), 128, np.float32)
    assert laplacian_variance(sharp) > 1000 > laplacian_variance(blur)


def test_flags():
    s = Settings()
    rng = np.random.default_rng(1)
    good = rng.integers(0, 255, (120, 80, 3)).astype(np.uint8)
    assert crop_flags(good, s) == []
    assert crop_flags(np.zeros((10, 10, 3), np.uint8), s) == ["tiny"]
    assert "blurry" in crop_flags(np.full((120, 80, 3), 120, np.uint8), s)
    white = np.full((120, 80, 3), 255, np.uint8)
    assert glare_fraction(white) == 1.0 and "glare" in crop_flags(white, s)
