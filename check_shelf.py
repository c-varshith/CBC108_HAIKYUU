"""Send a shelf photo to /analyze and compare the result with its answer key.

  python tools/check_shelf.py shelf_1.jpg
  python tools/check_shelf.py shelf_1.jpg http://localhost:8080/api/analyze     (this is the default)
"""
import collections, csv, json, sys, urllib.request, uuid
from pathlib import Path

shelf = Path(sys.argv[1])
api = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8080/api/analyze"
truth = collections.Counter(r["barcode"] for r in csv.DictReader(open(shelf.with_suffix(".truth.csv"))))

b = uuid.uuid4().hex
body = (f'--{b}\r\nContent-Disposition: form-data; name="image"; filename="{shelf.name}"\r\n'
        f'Content-Type: image/jpeg\r\n\r\n').encode() + shelf.read_bytes() + f'\r\n--{b}--\r\n'.encode()
req = urllib.request.Request(api, data=body, headers={"Content-Type": f"multipart/form-data; boundary={b}"})
groups = json.load(urllib.request.urlopen(req, timeout=600))

found = {g["label"]: g["quantity"] for g in groups if g["label"]}
unknown = sum(g["quantity"] for g in groups if g["label"] is None)
uncertain = sum(g["uncertain_units"] for g in groups)
total = sum(truth.values())
correct = sum(min(truth[k], found.get(k, 0)) for k in truth)

print(f"products on shelf: {total}")
print(f"boxes found:       {sum(found.values()) + unknown}")
print(f"matched correctly: {correct}  ({100 * correct / total:.0f}%)")
print(f"unknown boxes:     {unknown}")
print(f"flagged uncertain: {uncertain}")
print("\nMISSED or WRONG COUNT:")
for k in sorted(truth):
    if found.get(k, 0) != truth[k]:
        print(f"  {k}: expected {truth[k]}, got {found.get(k, 0)}")
print("\nFOUND BUT NOT ON SHELF:")
for k in sorted(found):
    if k not in truth:
        print(f"  {k}: {found[k]} box(es)")
