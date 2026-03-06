#!/usr/bin/env bash
set -euo pipefail

PORT="8000"
CLEAN_INSTALL="1"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BUILD_ID="$(date +%s)"
ZIP_NAME="JLCImport-dev-${BUILD_ID}.zip"

print_usage() {
  cat <<EOF
Usage: ./tools/run_local_pcm_repo.sh [port] [--no-clean]

Options:
  port        Local HTTP port (default: 8000)
  --no-clean  Do not remove existing KiCad plugin install directory
EOF
}

for arg in "$@"; do
  case "$arg" in
    --no-clean)
      CLEAN_INSTALL="0"
      ;;
    --help|-h)
      print_usage
      exit 0
      ;;
    *)
      if [[ "${PORT}" != "8000" ]]; then
        echo "error: only one port argument is supported" >&2
        print_usage >&2
        exit 1
      fi
      PORT="$arg"
      ;;
  esac
done

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "error: ${PYTHON_BIN} not found in PATH" >&2
  exit 1
fi

cd "${ROOT_DIR}"

INSTALL_DIR="$("${PYTHON_BIN}" - <<'PY'
import os
import sys

if sys.platform == "darwin":
    base = os.path.expanduser("~/Documents/KiCad")
elif sys.platform == "win32":
    base = os.path.join(os.environ.get("APPDATA", ""), "kicad")
else:
    base = os.path.expanduser("~/.local/share/kicad")

best = "9.0"
if os.path.isdir(base):
    for d in os.listdir(base):
        try:
            if float(d) > float(best):
                best = d
        except ValueError:
            continue

print(os.path.join(base, best, "3rdparty", "plugins", "com_github_jvanderberg_kicad-jlcimport"))
PY
)"

if [[ "${CLEAN_INSTALL}" == "1" ]]; then
  case "${INSTALL_DIR}" in
    */3rdparty/plugins/com_github_jvanderberg_kicad-jlcimport)
      ;;
    *)
      echo "error: refusing to remove unexpected path: ${INSTALL_DIR}" >&2
      exit 1
      ;;
  esac

  if [[ -d "${INSTALL_DIR}" ]]; then
    rm -rf "${INSTALL_DIR}"
    echo "Removed existing KiCad install: ${INSTALL_DIR}"
  else
    echo "No existing KiCad install found at: ${INSTALL_DIR}"
  fi
fi

"${PYTHON_BIN}" tools/build_pcm.py \
  --zip-name "${ZIP_NAME}" \
  --download-url "http://localhost:${PORT}/${ZIP_NAME}" \
  --packages-url "http://localhost:${PORT}/packages.json" \
  --resources-url "http://localhost:${PORT}/resources.zip"

cat <<EOF

Local PCM repository is ready.

In KiCad:
1. Open Tools > Plugin and Content Manager
2. Open repository settings
3. Add this URL:
   http://localhost:${PORT}/repository.json
4. Refresh repositories and install JLCImport

Serving dist/ at http://localhost:${PORT}
ZIP URL: http://localhost:${PORT}/${ZIP_NAME}
Press Ctrl+C to stop.
EOF

exec "${PYTHON_BIN}" -m http.server "${PORT}" --directory dist
