"""The root OFF dump selector is streaming, country exact, and accepts plain/gzip TSV."""
from __future__ import annotations

import csv
import gzip
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "select_off_barcodes.py"
HEADER = "code\tcountries_tags\tcountries_en\tproduct_name\n"
ROWS = (
    "000000000001\ten:france\tFrance\tFrench product\n"
    "000000000002\ten:india\tIndia\tIndian product\n"
    "000000000002\ten:india\tIndia\tDuplicate barcode\n"
    "000000000003\ten:indonesia\tIndonesia\tNot India\n"
    "000000000004\ten:in\tIndia\tIndia by name\n"
)


def _run(source: Path, output: Path, limit: str = "10") -> list[str]:
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), str(source), "--country", "India", "--limit", limit,
                             "--output", str(output)], check=True, capture_output=True, text=True)
    assert "Wrote" in result.stdout
    with output.open(newline="", encoding="utf-8") as fh:
        return [row[0] for row in list(csv.reader(fh))[1:]]


def test_selector_filters_and_deduplicates_plain_tsv(tmp_path):
    source, output = tmp_path / "dump.tsv", tmp_path / "stocked.csv"
    source.write_text(HEADER + ROWS, encoding="utf-8")
    assert _run(source, output) == ["000000000002", "000000000004"]


def test_selector_reads_gzip_tsv_and_stops_at_limit(tmp_path):
    source, output = tmp_path / "dump.tsv.gz", tmp_path / "stocked.csv"
    with gzip.open(source, "wt", encoding="utf-8", newline="") as fh:
        fh.write(HEADER + ROWS)
    assert _run(source, output, "1") == ["000000000002"]

