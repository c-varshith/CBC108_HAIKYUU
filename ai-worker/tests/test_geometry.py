import numpy as np

from app.geometry import clip_boxes, drop_border_cut, merge_passes, nms, reading_order, tile_grid


def test_tile_grid_covers_image_with_full_tiles():
    tiles = tile_grid(4032, 3024, 1280, 0.2)
    cover = np.zeros((3024, 4032), bool)
    for x1, y1, x2, y2 in tiles:
        assert (x2 - x1, y2 - y1) == (1280, 1280)
        cover[y1:y2, x1:x2] = True
    assert cover.all()


def test_small_image_single_tile():
    assert tile_grid(800, 600, 1280, 0.2) == [(0, 0, 800, 600)]


def test_nms_removes_duplicates_keeps_best():
    b = np.array([[0, 0, 10, 20], [1, 0, 11, 20], [30, 0, 40, 20]], np.float32)
    keep = nms(b, np.array([0.7, 0.9, 0.8], np.float32), 0.6)
    assert sorted(keep.tolist()) == [1, 2]


def test_nms_drops_half_box_inside_full_box():
    b = np.array([[0, 0, 100, 100], [0, 0, 50, 100]], np.float32)  # IoU 0.5 only, but contained
    assert nms(b, np.array([0.9, 0.8], np.float32), 0.6).tolist() == [0]


def test_adjacent_items_not_merged():
    b = np.array([[0, 0, 50, 100], [52, 0, 102, 100]], np.float32)
    assert len(nms(b, np.array([0.9, 0.9], np.float32), 0.6)) == 2


def test_drop_border_cut_only_internal_borders():
    tile, wh = (1000, 0, 2000, 1000), (3000, 1000)
    b = np.array([[1000, 100, 1040, 200], [1500, 100, 1540, 200], [1960, 100, 2000, 200]], np.float32)
    kept, _ = drop_border_cut(b, np.ones(3, np.float32), tile, wh)
    assert kept.tolist() == [[1500, 100, 1540, 200]]
    # an image-edge border is not an internal cut
    kept2, _ = drop_border_cut(np.array([[0, 0, 40, 40]], np.float32), np.ones(1, np.float32), (0, 0, 1000, 1000), (3000, 1000))
    assert len(kept2) == 1


def test_merge_passes_dedups_overlap_between_tiles():
    a = (np.array([[100, 100, 150, 200]], np.float32), np.array([0.8], np.float32))
    b = (np.array([[101, 100, 151, 201]], np.float32), np.array([0.9], np.float32))
    boxes, scores = merge_passes([a, b], 0.6, 100)
    assert len(boxes) == 1 and abs(scores[0] - 0.9) < 1e-6


def test_clip_and_reading_order():
    b = clip_boxes(np.array([[-5, -5, 50, 5000]], np.float32), 100, 200)
    assert b.tolist() == [[0, 0, 50, 200]]
    boxes = np.array([[200, 0, 250, 90], [0, 100, 50, 190], [0, 0, 50, 90]], np.float32)
    assert reading_order(boxes).tolist() == [2, 0, 1]
