import numpy as np

from app.store import Hit, InMemoryStore, aggregate_by_sku


def _vec(*xs):
    return np.array([xs], np.float32)


def test_search_orders_by_cosine_and_filters_by_delete():
    st = InMemoryStore(3)
    st.upsert("A", "Alpha", "r1", _vec(1, 0, 0))
    st.upsert("B", "Beta", "r1", _vec(0, 1, 0))
    top = st.search(_vec(0.9, 0.1, 0), 2)[0]
    assert [h.sku for h in top] == ["A", "B"] and top[0].score > 0.99
    st.delete_sku("A")
    assert [h.sku for h in st.search(_vec(1, 0, 0), 5)[0]] == ["B"]
    assert st.skus() == ["B"]


def test_upsert_same_ref_overwrites():
    st = InMemoryStore(3)
    st.upsert("A", "Alpha", "r1", _vec(1, 0, 0))
    st.upsert("A", "Alpha", "r1", _vec(0, 1, 0))
    assert st.count() == 1


def test_delete_reference_removes_only_that_reference_and_is_safe_to_repeat():
    st = InMemoryStore(3)
    st.upsert("A", "Alpha", "r1", _vec(1, 0, 0))
    st.upsert("A", "Alpha", "r2", _vec(0, 1, 0))
    st.delete_reference("A", "r1")
    st.delete_reference("A", "r1")
    assert st.count() == 1
    assert st.search(_vec(1, 0, 0), 2)[0][0].sku == "A"


def test_empty_store_returns_no_hits():
    assert InMemoryStore(3).search(_vec(1, 0, 0), 5) == [[]]


def test_aggregate_distinct_skus_best_first_with_support_bonus():
    hits = [Hit("A", "a", 0.90), Hit("A", "a", 0.89), Hit("B", "b", 0.905), Hit("C", "c", 0.5)]
    out = aggregate_by_sku(hits, 5)
    assert [h.sku for h in out] == ["A", "B", "C"]  # two agreeing refs lift A just above B's single lucky hit
    assert len({h.sku for h in out}) == len(out)
    assert out[0].score <= 1.0
