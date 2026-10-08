#!/usr/bin/env bash
# Create .venv next to this script and install py-libp2p (macOS / Linux / WSL2).
#   ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

# Pick a supported Python: 3.12 preferred, 3.10-3.13 accepted.
PY=""
for c in python3.12 python3.13 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 &&
     "$c" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Need Python 3.10-3.13 (3.12 recommended)."
  echo "  macOS:  brew install python@3.12"
  echo "  Ubuntu: sudo apt install python3.12 python3.12-venv"
  exit 1
fi
echo "Using $PY ($("$PY" --version))"

# fastecdsa (a py-libp2p dependency outside Windows) may compile from source; it needs GMP.
case "$(uname -s)" in
  Darwin)
    if ! "$PY" -c 'import sys,platform; sys.exit(0 if platform.machine()=="arm64" and sys.version_info[:2]<=(3,12) else 1)'; then
      if command -v brew >/dev/null 2>&1; then
        brew list gmp >/dev/null 2>&1 || brew install gmp
        GMP="$(brew --prefix gmp)"
        export CFLAGS="${CFLAGS:-} -I$GMP/include" LDFLAGS="${LDFLAGS:-} -L$GMP/lib"
      else
        echo "This Mac/Python combination compiles fastecdsa, which needs GMP: install Homebrew, then 'brew install gmp'."
        exit 1
      fi
    fi
    ;;
  Linux)
    if ! printf '#include <gmp.h>\nint main(void){return 0;}\n' | ${CC:-cc} ${CFLAGS:-} -x c - -o /dev/null >/dev/null 2>&1; then
      echo "fastecdsa will be compiled and needs the GMP headers, a C compiler and Python headers:"
      echo "  Debian/Ubuntu: sudo apt install libgmp-dev build-essential python3-dev python3-venv"
      echo "  Fedora:        sudo dnf install gmp-devel gcc python3-devel"
      echo "(No sudo? See README 'Linux install gotcha'. Then re-run with CFLAGS/LDFLAGS set.)"
      exit 1
    fi
    ;;
esac

"$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c "import libp2p, p2p_spike; print('OK: py-libp2p', __import__('importlib.metadata').metadata.version('libp2p'), 'installed in', __import__('sys').prefix)"
