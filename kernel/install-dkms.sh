#!/usr/bin/env bash
# Register the 3 custom kernel modules (proc/nf/usb_linux_ai) with DKMS so they
# auto-rebuild on every kernel upgrade. Idempotent — safe to re-run after a
# source change to re-sync /usr/src and rebuild for all installed kernels.
#
# Source of truth = this git dir. /usr/src/linux-ai-<ver> is a DKMS-managed
# COPY (only *.c + Makefile + dkms.conf) — copying clean source avoids dragging
# stale .o/.ko from a previous kernel into the DKMS build tree.
set -euo pipefail

NAME="linux-ai"
VER="1.0"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DKMS_SRC="/usr/src/${NAME}-${VER}"

echo "==> Sync source -> ${DKMS_SRC}"
sudo rm -rf "${DKMS_SRC}"
sudo mkdir -p "${DKMS_SRC}"
sudo cp "${SRC_DIR}"/*.c "${SRC_DIR}/Makefile" "${SRC_DIR}/dkms.conf" "${DKMS_SRC}/"

# Drop any prior registration (ignore if absent), then re-add fresh.
echo "==> dkms add"
sudo dkms remove -m "${NAME}" -v "${VER}" --all 2>/dev/null || true
sudo dkms add -m "${NAME}" -v "${VER}"

# Build+install for every kernel that has headers available (not just running).
echo "==> Build+install for all installed kernels with headers"
for kdir in /lib/modules/*/build; do
  kver="$(basename "$(dirname "${kdir}")")"
  [ -e "${kdir}" ] || continue
  echo "    -> ${kver}"
  sudo dkms build   -m "${NAME}" -v "${VER}" -k "${kver}"
  sudo dkms install -m "${NAME}" -v "${VER}" -k "${kver}" --force
done

echo "==> depmod for all kernels"
for kver in /lib/modules/*/; do sudo depmod "$(basename "${kver}")"; done

echo
echo "==> dkms status"
dkms status "${NAME}"
