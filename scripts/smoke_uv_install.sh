#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-local}"
VENV_PATH="${REBASE_TOOLKIT_TEST_VENV:-/tmp/rebase-toolkit-smoke-venv}"
GIT_REF="${REBASE_TOOLKIT_GIT_REF:-master}"

case "$MODE" in
  local)
    INSTALL_SPEC="rebase-toolkit @ file://${ROOT}"
    ;;
  github)
    INSTALL_SPEC="rebase-toolkit @ git+ssh://git@github.com/rebase-energy/rebase-toolkit.git@${GIT_REF}"
    ;;
  *)
    INSTALL_SPEC="$MODE"
    ;;
esac

rm -rf "$VENV_PATH"
uv venv "$VENV_PATH"
uv pip install --python "$VENV_PATH/bin/python" "$INSTALL_SPEC"

"$VENV_PATH/bin/python" - <<'PY'
import rebase as rb

print(f"rebase-toolkit {rb.__version__}")
assert callable(rb.project)
assert callable(rb.function)
assert callable(rb.workflow)
PY

"$VENV_PATH/bin/rebase" --help >/dev/null
"$VENV_PATH/bin/rebase" workspace --help >/dev/null

echo "Smoke install succeeded in ${VENV_PATH}"
