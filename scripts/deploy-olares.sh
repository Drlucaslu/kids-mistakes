#!/usr/bin/env bash
# Deploy 错题本 (cuotiben) to Olares with nothing but olares-cli (no image registry, no SSH):
#   1. build a code bundle (source + linux/amd64 cp312 wheels)
#   2. upload it to the app data dir:  drive/Data/cuotiben/bundle/   (olares-cli files upload)
#   3. lint + package the chart, upload it, install/upgrade             (olares-cli market)
# Same approach as kinnet-ai-coo: the chart runs python:3.12-slim and installs the bundle offline.
#
# Usage:  scripts/deploy-olares.sh install
#         scripts/deploy-olares.sh upgrade [x.y.z]   # bumps Chart / Manifest / bundle / package versions
#         scripts/deploy-olares.sh version [x.y.z]   # only bump the versions
#         scripts/deploy-olares.sh bundle
# Env:    install-time settings come from ~/.cache/cuotiben/olares-deploy.env (created on first run, mode 600)
set -euo pipefail
cd "$(dirname "$0")/.."
ACTION="${1:-install}"
CHART=deploy/olares/cuotiben
PY="${PYTHON:-.venv/bin/python}"

bump_version() {
  # Olares refuses to re-upload an existing version: every upgrade gets a new one, written everywhere.
  local cur new
  cur="$(grep '^version:' $CHART/Chart.yaml | awk '{print $2}')"
  new="${1:-$(echo "$cur" | awk -F. '{printf "%d.%d.%d", $1, $2, $3 + 1}')}"
  [[ "$new" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "invalid version: $new"; exit 2; }
  "$PY" - "$CHART" "$cur" "$new" <<'PY'
import re, sys
chart, cur, new = sys.argv[1:4]
edits = {
    f"{chart}/Chart.yaml": [(r"^version: .*$", f"version: {new}"), (r"^appVersion: .*$", f"appVersion: '{new}'")],
    f"{chart}/OlaresManifest.yaml": [(r"^  version: .*$", f"  version: '{new}'"), (r"^  versionName: .*$", f"  versionName: '{new}'")],
    f"{chart}/values.yaml": [(r"^  version: .*$", f"  version: '{new}'")],
    "cuoti/__init__.py": [(r"^__version__ = .*$", f'__version__ = "{new}"')],
    "pyproject.toml": [(r'^version = .*$', f'version = "{new}"')],
}
for path, subs in edits.items():
    s = open(path).read()
    for pat, rep in subs:
        s, n = re.subn(pat, rep, s, count=1, flags=re.M)
        assert n == 1, (path, pat)
    open(path, "w").write(s)
print(f"==> version {cur} -> {new}")
PY
}

if [ "$ACTION" = upgrade ] || [ "$ACTION" = version ]; then bump_version "${2:-}"; fi
if [ "$ACTION" = version ]; then exit 0; fi
VERSION="$(grep '^version:' $CHART/Chart.yaml | awk '{print $2}')"
BUNDLE_VERSION="$(grep -A1 '^bundle:' $CHART/values.yaml | awk '/version:/{print $2}' | tr -d "'\"")"
OUT="${TMPDIR:-/tmp}/cuotiben-deploy"; rm -rf "$OUT"; mkdir -p "$OUT/app/wheels"
ENVF="$HOME/.cache/cuotiben/olares-deploy.env"

build_bundle() {
  echo "==> building bundle $BUNDLE_VERSION"
  grep -v '^\s*#' requirements.lock | sed 's/ *#.*//' | grep -v '^\s*$' > "$OUT/req.txt"
  uvx pip download -q --only-binary=:all: --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 \
    --platform manylinux_2_17_x86_64 --python-version 3.12 --implementation cp -d "$OUT/app/wheels" -r "$OUT/req.txt"
  mkdir -p "$OUT/app/src"
  cp -R cuoti "$OUT/app/src/" && cp "$OUT/req.txt" "$OUT/app/requirements.lock"
  find "$OUT/app/src" -name __pycache__ -prune -exec rm -rf {} +
  COPYFILE_DISABLE=1 tar czf "$OUT/cuotiben-$BUNDLE_VERSION.tgz" -C "$OUT/app" .
  echo "==> uploading bundle to drive/Data/cuotiben/bundle/"
  olares-cli files mkdir -p drive/Data/cuotiben/bundle >/dev/null 2>&1 || true
  olares-cli files upload "$OUT/cuotiben-$BUNDLE_VERSION.tgz" drive/Data/cuotiben/bundle/
}

package_chart() {
  olares-cli chart lint "$CHART" --with-security-context
  tar czf "$OUT/cuotiben-$VERSION.tgz" -C deploy/olares cuotiben
  olares-cli market upload "$OUT/cuotiben-$VERSION.tgz"
}

ensure_env() {
  if [ ! -f "$ENVF" ]; then
    mkdir -p "$(dirname "$ENVF")"; umask 077
    printf 'INIT_PARENT_PIN=\nANTHROPIC_API_KEY=\nMODEL_PROVIDER=anthropic\n' > "$ENVF"
    echo "created $ENVF — fill in INIT_PARENT_PIN (6+ digits) and ANTHROPIC_API_KEY, then rerun"; exit 1
  fi
  set -a; . "$ENVF"; set +a
  [[ "${INIT_PARENT_PIN:-}" =~ ^[0-9]{6,}$ ]] || { echo "INIT_PARENT_PIN in $ENVF must be 6+ digits"; exit 1; }
}

env_flags() {
  for name in INIT_PARENT_PIN ANTHROPIC_API_KEY MODEL_PROVIDER MODEL_NAME MODEL_BASE_URL MODEL_API_KEY; do
    if [ -n "${!name:-}" ]; then printf '%s\n' "--env" "$name=${!name}"; fi
  done
}

case "$ACTION" in
  bundle) build_bundle ;;
  install|upgrade)
    [ "$ACTION" = install ] && ensure_env
    build_bundle; package_chart
    if [ "$ACTION" = install ]; then
      FLAGS=(); while IFS= read -r l; do FLAGS+=("$l"); done < <(env_flags)
      olares-cli market install cuotiben -s upload --version "$VERSION" --watch --watch-timeout 20m "${FLAGS[@]}"
    else
      olares-cli market upgrade cuotiben -s upload --version "$VERSION" --watch --watch-timeout 20m  # envs persist
    fi ;;
  *) echo "usage: $0 [install|upgrade|version|bundle]"; exit 2 ;;
esac
echo "==> done"
