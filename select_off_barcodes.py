#!/usr/bin/env python3
"""Stream an Open Food Facts TSV dump and write barcodes matching a country."""
from __future__ import annotations

import argparse
import csv
import gzip
import re
import sys
from pathlib import Path

BARCODE = re.compile(r"^\d{8,14}$")


def open_dump(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def country_matches(row: dict[str, str], country: str) -> bool:
    wanted = country.strip().casefold().replace("_", "-")
    tags = (row.get("countries_tags") or "").split(",")
    names = (row.get("countries_en") or "").split(",")
    return any(tag.strip().casefold() in {wanted, f"en:{wanted}"} for tag in tags) or any(
        name.strip().casefold() == wanted.replace("-", " ") for name in names
    )


def select(path: Path, country: str, limit: int | None) -> tuple[list[str], int]:
    csv.field_size_limit(min(sys.maxsize, 16 * 1024 * 1024))
    selected: list[str] = []
    seen: set[str] = set()
    scanned = 0
    with open_dump(path) as fh:
        rows = csv.DictReader(fh, delimiter="\t")
        if not rows.fieldnames or "code" not in rows.fieldnames:
            raise ValueError("Expected a tab-separated OFF dump with a 'code' column.")
        if not {"countries_tags", "countries_en"}.intersection(rows.fieldnames):
            raise ValueError("Dump needs a countries_tags or countries_en column.")
        for row in rows:
            scanned += 1
            code = (row.get("code") or "").strip()
            if not BARCODE.fullmatch(code) or code in seen or not country_matches(row, country):
                continue
            selected.append(code)
            seen.add(code)
            if limit is not None and len(selected) >= limit:
                break
    return selected, scanned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path, help="plain TSV or .gz Open Food Facts dump")
    parser.add_argument("--country", required=True, help="country name, for example India")
    parser.add_argument("--limit", type=int, help="maximum unique barcodes to write")
    parser.add_argument("-o", "--output", type=Path, default=Path("stocked.csv"))
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    selected, scanned = select(args.dump, args.country, args.limit)
    with args.output.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["barcode"])
        writer.writerows((code,) for code in selected)
    print(f"Wrote {len(selected)} unique {args.country} barcodes to {args.output} (read {scanned} dump rows).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
