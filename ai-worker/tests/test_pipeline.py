"""End-to-end logic with fakes, including the headline claim: a NEW product becomes detectable by adding reference
photos only (no retraining)."""
from collections import Counter

from app.config import Settings
from app.pipeline import Pipeline
from app.store import InMemoryStore
from tests.fakes import DIM, FakeDetector, FakeEmbedder, shelf, swatch

RED, GREEN, BLUE, YELLOW = (200, 30, 30), (30, 190, 60), (30, 60, 200), (230, 210, 40)


def make(boxes, s=None):
    s = s or Settings()
    return Pipeline(s, FakeDetector(boxes), FakeEmbedder(), InMemoryStore(DIM))


def counts(dets):
    return Counter(d["matches"][0]["sku"] for d in dets if d["matches"])


def test_counts_per_sku_and_contract_shape():
    img, boxes = shelf([RED, RED, GREEN, RED, BLUE])
    p = make(boxes)
    p.add_references("SKU-R", "Red", [("r1", swatch(RED))])
    p.add_references("SKU-G", "Green", [("g1", swatch(GREEN))])
    p.add_references("SKU-B", "Blue", [("b1", swatch(BLUE))])
    dets, _ = p.detect(img, top_k=3)
    assert counts(dets) == {"SKU-R": 3, "SKU-G": 1, "SKU-B": 1}
    d = dets[0]
    assert set(d) == {"bbox", "det_confidence", "matches", "match_probability", "flags"} and len(d["bbox"]) == 4
    assert d["match_probability"] is None  # no calibration loaded in this test: similarity only
    scores = [m["score"] for m in d["matches"]]
    assert scores == sorted(scores, reverse=True) and len({m["sku"] for m in d["matches"]}) == len(scores)
    assert d["matches"][0]["score"] > 0.99


def test_new_product_detectable_after_adding_references_only():
    img, boxes = shelf([RED, YELLOW, YELLOW, RED])
    p = make(boxes)
    p.add_references("SKU-R", "Red", [("r1", swatch(RED))])
    p.add_references("SKU-B", "Blue", [("b1", swatch(BLUE))])
    before, _ = p.detect(img)
    yellow_before = [d for d in before if d["bbox"][0] in (boxes[1][0], boxes[2][0])]
    assert all(d["matches"][0]["score"] < 0.995 or d["matches"][0]["sku"] != "SKU-Y" for d in yellow_before)
    assert "SKU-Y" not in counts(before)
    p.add_references("SKU-Y", "Yellow", [("y1", swatch(YELLOW)), ("y2", swatch((225, 205, 50)))])  # <- the whole 'registration'
    after, _ = p.detect(img)
    assert counts(after) == {"SKU-R": 2, "SKU-Y": 2}


def test_remove_product_forgets_it():
    img, boxes = shelf([RED, GREEN])
    p = make(boxes)
    p.add_references("SKU-R", "Red", [("r1", swatch(RED))])
    p.add_references("SKU-G", "Green", [("g1", swatch(GREEN))])
    p.remove_product("SKU-G")
    assert "SKU-G" not in {m["sku"] for d in p.detect(img)[0] for m in d["matches"]}


def test_flags_surface_and_empty_cases():
    img, boxes = shelf([RED])
    p = make(boxes + [[0, 0, 5, 5]])  # a 5x5 box is 'tiny'
    p.add_references("SKU-R", "Red", [("r1", swatch(RED))])
    dets, _ = p.detect(img)
    assert any("tiny" in d["flags"] for d in dets)
    assert make([]).detect(img)[0] == []
    empty_catalog, _ = make(boxes).detect(img)
    assert empty_catalog[0]["matches"] == []  # backend turns this into UNKNOWN


def test_lookalike_keeps_competitor_close():
    """Two near-identical packs: the runner-up must be a different SKU with a score close to the winner, so the backend's
    margin rule sends the box to CHECK."""
    img, boxes = shelf([(200, 30, 30)])
    p = make(boxes)
    p.add_references("SKU-R1", "Red 500g", [("a", swatch((200, 30, 30)))])
    p.add_references("SKU-R2", "Red 1kg", [("a", swatch((205, 34, 30)))])
    m = p.detect(img)[0][0]["matches"]
    assert m[0]["sku"] == "SKU-R1" and m[1]["sku"] == "SKU-R2" and m[0]["score"] - m[1]["score"] < 0.08
