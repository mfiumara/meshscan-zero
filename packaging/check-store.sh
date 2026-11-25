#!/bin/bash
# Run the CardputerZero store's own checks against dist/meshscan_<ver>_all.deb and app-builder.json,
# offline-equivalent to what `czdev publish` and the packages repo's validate-pr workflow do.
#
#   packaging/check-store.sh [path/to/CardputerZero-packages-clone]
#
# Without a clone it downloads the policy scripts from the packages repo's main branch.
# Checks that need your GitHub login (ownership, uploaded_by, version vs. published) are left to CI.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
PKGS=${1:-}
VERSION=$(python3 -c 'import json; print(json.load(open("app-builder.json"))["version"])')
DEB="$ROOT/dist/meshscan_${VERSION}_all.deb"
[ -f "$DEB" ] || { echo "build first: packaging/build-deb.sh"; exit 1; }
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
fetch() {
    if [ -n "$PKGS" ]; then cp "$PKGS/.github/scripts/$1" "$WORK/$1"
    else curl -fsSL "https://raw.githubusercontent.com/CardputerZero/packages/main/.github/scripts/$1" -o "$WORK/$1"; fi
}
fetch install_path_policy.py
fetch store_meta_policy.py
fail=0
ok() { echo "  ✓ $1"; }
bad() { echo "  ✗ $1"; fail=1; }

echo "Package $DEB"
pkg=$(dpkg-deb -f "$DEB" Package); ver=$(dpkg-deb -f "$DEB" Version); arch=$(dpkg-deb -f "$DEB" Architecture)
[ "$(basename "$DEB")" = "${pkg}_${ver}_${arch}.deb" ] && ok "file name matches control fields" || bad "file name"
case "$arch" in arm64|all) ok "architecture $arch";; *) bad "architecture $arch";; esac
echo "$pkg" | grep -qE '^[a-z0-9][a-z0-9.+-]+$' && ok "package name $pkg" || bad "package name"
dpkg-deb -c "$DEB" > "$WORK/contents.txt"
grep -q 'usr/share/APPLaunch/applications/.*\.desktop$' "$WORK/contents.txt" && ok ".desktop in usr/share/APPLaunch/applications" || bad "missing .desktop"
awk '{print $1}' "$WORK/contents.txt" | grep -qE '^[-l].{2}[sS]|^[-l].{5}[sS]' && bad "setuid/setgid files" || ok "no setuid/setgid files"
awk '{print $1}' "$WORK/contents.txt" | grep -qE '^[bc]' && bad "device files" || ok "no device files"
python3 "$WORK/install_path_policy.py" "$pkg" "$DEB" "$WORK/bad-paths.txt" "$WORK/help.md"
if [ -s "$WORK/bad-paths.txt" ]; then bad "install paths:"; sed 's/^/      /' "$WORK/bad-paths.txt"; else ok "install paths (install_path_policy.py)"; fi
mkdir "$WORK/ctrl"; dpkg-deb -e "$DEB" "$WORK/ctrl"
danger=0
for scr in preinst postinst prerm postrm; do
    [ -f "$WORK/ctrl/$scr" ] || continue
    if grep -qE 'rm[[:space:]]+(-[a-zA-Z]+[[:space:]]+)*(/([[:space:]]|$)|/(bin|etc|usr|var|home|boot|lib))|(curl|wget)[^|]*\|[[:space:]]*(ba)?sh|mkfs|/etc/(passwd|shadow|sudoers)|dd[[:space:]][^|]*of=/dev/' "$WORK/ctrl/$scr"; then
        bad "dangerous pattern in $scr"; danger=1
    fi
done
[ $danger = 0 ] && ok "maintainer scripts pass the dangerous-pattern check"
python3 - "$DEB" <<'PY' && echo "  ✓ no systemd service runs as root" || { echo "  ✗ root services"; exit 1; }
import io, subprocess, sys, tarfile
DIRS = ("lib/systemd/system/", "usr/lib/systemd/system/", "etc/systemd/system/")
def runs_as_root(text):
    in_service = False; user = None; dyn = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;": continue
        if line.startswith("[") and line.endswith("]"):
            in_service = line.lower() == "[service]"; continue
        if not in_service or "=" not in line: continue
        k, _, v = line.partition("="); k = k.strip().lower(); v = v.strip()
        if k == "user": user = v
        elif k == "dynamicuser": dyn = v.lower() in ("1", "yes", "true", "on")
    return False if dyn else (not user or user in ("root", "0"))
tb = subprocess.run(["dpkg-deb", "--fsys-tarfile", sys.argv[1]], capture_output=True, check=True).stdout
bad = []
with tarfile.open(fileobj=io.BytesIO(tb)) as tf:
    for m in tf.getmembers():
        n = m.name.lstrip("./")
        if m.isfile() and n.endswith(".service") and n.startswith(DIRS):
            if runs_as_root(tf.extractfile(m).read().decode()): bad.append(n)
print("\n".join(bad)); sys.exit(1 if bad else 0)
PY
size=$(stat -f%z "$DEB" 2>/dev/null || stat -c%s "$DEB")
[ "$size" -lt 2147483648 ] && ok "size $((size / 1024)) KiB" || bad "too large"

echo "Store listing (app-builder.json -> meta.json, as czdev builds it)"
mkdir -p "$WORK/pool/$pkg/screenshots"
python3 - "$ROOT" "$WORK/pool/$pkg" <<'PY'
import json, shutil, sys
from pathlib import Path
root, out = Path(sys.argv[1]), Path(sys.argv[2])
raw = json.load(open(root / "app-builder.json"))
s = raw["store"]
meta = {"title": raw["app_name"], "summary": s["summary"], "categories": s["categories"],
        "screenshots": [f"screenshots/{Path(p).name}" for p in s["screenshots"]]}
for k in ("description", "locales", "license", "source_repo", "permissions", "share_code"):
    if s.get(k): meta[k] = s[k]
meta["icon"] = Path(s["icon"]).name
meta["author"] = s["author"]
for p in s["screenshots"]: shutil.copy2(root / p, out / "screenshots" / Path(p).name)
shutil.copy2(root / s["icon"], out / Path(s["icon"]).name)
(out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
PY
POOL=-
[ -n "$PKGS" ] && POOL="$PKGS/pool/main"
DEB_MAINTAINER=$(dpkg-deb -f "$DEB" Maintainer) python3 "$WORK/store_meta_policy.py" "$pkg" "$WORK/pool/$pkg" "$POOL" "$WORK/errors.txt" "$WORK/warnings.txt" || true
[ -s "$WORK/warnings.txt" ] && sed 's/^/  ⚠ /' "$WORK/warnings.txt"
if [ -s "$WORK/errors.txt" ]; then bad "store metadata:"; sed 's/^/      /' "$WORK/errors.txt"; else
    ok "store metadata (store_meta_policy.py$([ "$POOL" != - ] && echo ', incl. title/share_code uniqueness'))"; fi
[ $fail = 0 ] && echo "All offline store checks passed." || { echo "Store checks FAILED."; exit 1; }
