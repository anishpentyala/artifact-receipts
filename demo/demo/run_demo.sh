#!/usr/bin/env bash
# Offline demo: ordinary pytest passes while running the WRONG copy of a
# package (a source checkout shadowing the installed wheel), and
# artifact-receipts catches the difference. No network access needed.
set -euo pipefail
cd "$(dirname "$0")/.."
DEMO=$(mktemp -d)
trap 'rm -rf "$DEMO"' EXIT

echo "== building demopkg 1.0 wheel =="
python3 - "$DEMO" <<'PY'
import base64, hashlib, os, sys, zipfile
demo = sys.argv[1]
members = {
    "demopkg/__init__.py": b'"""demopkg."""\nVALUE = 1\n',
    "demopkg/sub.py": b"def value():\n    return 1\n",
    "demopkg-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: demopkg\nVersion: 1.0\n",
    "demopkg-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
}
rows = []
for arc, data in members.items():
    d = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    rows.append("%s,sha256=%s,%d" % (arc, d, len(data)))
rows.append("demopkg-1.0.dist-info/RECORD,,")
with zipfile.ZipFile(os.path.join(demo, "demopkg-1.0-py3-none-any.whl"), "w") as z:
    for arc, data in members.items():
        z.writestr(arc, data)
    z.writestr("demopkg-1.0.dist-info/RECORD", "\n".join(rows) + "\n")

# "installed" copy (site-packages stand-in) and shadowing source checkout.
def write_pkg(root, value):
    pkg = os.path.join(root, "demopkg")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w") as f:
        f.write('"""demopkg."""\nVALUE = %d\n' % value)
    with open(os.path.join(pkg, "sub.py"), "w") as f:
        f.write("def value():\n    return %d\n" % value)

write_pkg(os.path.join(demo, "site"), 1)      # matches the wheel
write_pkg(os.path.join(demo, "checkout"), 2)  # source checkout, different bytes

with open(os.path.join(demo, "test_demo.py"), "w") as f:
    f.write("import demopkg.sub\n\ndef test_value():\n"
            "    assert demopkg.sub.value() in (1, 2)  # passes either way\n")
PY

echo
echo "== case 1: tests run against the installed wheel copy =="
PYTHONPATH="$DEMO/site" python3 -m artifact_receipts run \
    --wheel "$DEMO/demopkg-1.0-py3-none-any.whl" --package demopkg \
    --json-report "$DEMO/ok.json" -- "$DEMO/test_demo.py" -q
echo "exit=$? (0 = verified)"

echo
echo "== case 2: a source checkout shadows the wheel (checkout first on PYTHONPATH) =="
set +e
PYTHONPATH="$DEMO/checkout:$DEMO/site" python3 -m artifact_receipts run \
    --wheel "$DEMO/demopkg-1.0-py3-none-any.whl" --package demopkg \
    --json-report "$DEMO/bad.json" -- "$DEMO/test_demo.py" -q
rc=$?
set -e
echo "exit=$rc (13 = provenance failure; pytest itself still passed)"
if [ "$rc" -ne 13 ]; then
    echo "Demo failed: expected provenance failure exit 13, got $rc" >&2
    exit 1
fi
