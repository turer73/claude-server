#!/usr/bin/env bash
# Register the 3 custom kernel modules (proc/nf/usb_linux_ai) with DKMS so they
# auto-rebuild on every kernel upgrade. Idempotent — safe to re-run after a
# source change to re-sync /usr/src and rebuild for all installed kernels.
#
# Source of truth = this git dir. /usr/src/linux-ai-<ver> is a DKMS-managed
# COPY (only *.c + Makefile + dkms.conf) — copying clean source avoids dragging
# stale .o/.ko from a previous kernel into the DKMS build tree.
#
# Safety (PR #103 Codex P2): never remove an installed module before its
# replacement is ready. We build ALL target kernels first; only if every build
# succeeds do we install. A compile/header failure aborts under `set -e` while
# the previously-installed modules are still in place — a re-run can never leave
# a kernel module-less.
set -euo pipefail

NAME="linux-ai"
VER="1.0"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DKMS_SRC="/usr/src/${NAME}-${VER}"

echo "==> Sync source -> ${DKMS_SRC}"
sudo mkdir -p "${DKMS_SRC}"
sudo cp "${SRC_DIR}"/*.c "${SRC_DIR}/Makefile" "${SRC_DIR}/dkms.conf" "${DKMS_SRC}/"

# Boot autoload: without this, DKMS installs the .ko but nothing loads them at
# boot, so /proc/linux_ai* stay absent after a reboot (PR #104 Codex P2).
echo "==> Install boot autoload -> /etc/modules-load.d/linux-ai.conf"
sudo install -m 0644 "${SRC_DIR}/modules-load.conf" /etc/modules-load.d/linux-ai.conf

# Register source with DKMS if not already registered. We do NOT remove an
# existing registration — `dkms build --force` below picks up the freshly
# synced source, so re-runs need no destructive remove/add.
# NB: capture to a var (not `... | grep`) — under `pipefail`, grep -q closing
# the pipe early sends SIGPIPE to dkms, making the pipeline report failure and
# misfiring the add on an already-registered module.
if [ -z "$(dkms status -m "${NAME}" -v "${VER}")" ]; then
  echo "==> dkms add"
  sudo dkms add -m "${NAME}" -v "${VER}"
fi

# Target kernels = those with build headers available (not just the running one).
kvers=()
for kdir in /lib/modules/*/build; do
  [ -e "${kdir}" ] || continue
  kvers+=("$(basename "$(dirname "${kdir}")")")
done

# Phase 1 — build every target. If any build fails, set -e aborts HERE, before
# a single install has touched the currently-installed modules.
echo "==> Build (all target kernels)"
for kver in "${kvers[@]}"; do
  echo "    build -> ${kver}"
  sudo dkms build -m "${NAME}" -v "${VER}" -k "${kver}" --force
done

# Phase 2 — all builds succeeded; now swap them in (per-module overwrite).
echo "==> Install (all target kernels)"
for kver in "${kvers[@]}"; do
  echo "    install -> ${kver}"
  sudo dkms install -m "${NAME}" -v "${VER}" -k "${kver}" --force
  sudo depmod "${kver}"
done

echo
echo "==> dkms status"
dkms status "${NAME}"
