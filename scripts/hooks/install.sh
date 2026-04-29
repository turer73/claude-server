#!/bin/bash
# Klipper'a yari otonom hook sistemini kur.
# Idempotent: tekrar tekrar calistirilabilir.
#
# Kullanim:
#   bash scripts/hooks/install.sh                  # ~/.claude/settings.json'a kur
#   bash scripts/hooks/install.sh --project        # ./.claude/settings.json'a kur
#   bash scripts/hooks/install.sh --check          # Mevcut durumu raporla
#   bash scripts/hooks/install.sh --uninstall      # Hook'lari kaldir
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOOKS_SRC="$REPO_ROOT/scripts/hooks"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/linux-ai-server/scripts/hooks}"
LOG_DIR="${LOG_DIR:-/opt/linux-ai-server/data/hook-logs}"

MODE="user"   # user | project
ACTION="install"

while [ $# -gt 0 ]; do
  case "$1" in
    --project) MODE="project" ;;
    --user)    MODE="user" ;;
    --check)   ACTION="check" ;;
    --uninstall) ACTION="uninstall" ;;
    -h|--help)
      sed -n '1,12p' "$0"
      exit 0
      ;;
    *) echo "bilinmeyen flag: $1" >&2; exit 1 ;;
  esac
  shift
done

if [ "$MODE" = "user" ]; then
  TARGET_DIR="$HOME/.claude"
else
  TARGET_DIR="$REPO_ROOT/.claude"
fi
TARGET_FILE="$TARGET_DIR/settings.json"

case "$ACTION" in
  check)
    echo "=== Hook Sistemi Durumu ==="
    echo "Repo: $REPO_ROOT"
    echo "Mod:  $MODE"
    echo "Hedef settings: $TARGET_FILE"
    echo ""
    echo "Hook scriptleri:"
    for f in session-start.sh user-prompt-log.sh pre-bash-guard.sh post-bash-capture.sh stop-save-session.py; do
      if [ -x "$HOOKS_SRC/$f" ]; then
        echo "  [OK]   $f"
      else
        echo "  [EKSIK] $f"
      fi
    done
    echo ""
    if [ -f "$TARGET_FILE" ]; then
      echo "settings.json mevcut ($TARGET_FILE)"
      python3 -c "import json; d=json.load(open('$TARGET_FILE')); print('  Hook eventleri:', list((d.get('hooks') or {}).keys()))"
    else
      echo "settings.json YOK ($TARGET_FILE)"
    fi
    echo ""
    if [ -d "$DEPLOY_DIR" ]; then
      echo "Deploy dizini mevcut: $DEPLOY_DIR"
    else
      echo "Deploy dizini YOK: $DEPLOY_DIR (yetkisi varsa install --user kurulumda olusur)"
    fi
    if [ -d "$LOG_DIR" ]; then
      echo "Log dizini mevcut: $LOG_DIR"
      ls -la "$LOG_DIR" 2>/dev/null | tail -n +2
    else
      echo "Log dizini YOK: $LOG_DIR"
    fi
    exit 0
    ;;

  uninstall)
    if [ -f "$TARGET_FILE" ]; then
      python3 - "$TARGET_FILE" <<'PY'
import json, sys
p = sys.argv[1]
with open(p) as f: d = json.load(f)
hooks = d.get("hooks") or {}
# Sadece bizim hook'larimizi cikar (path'i scripts/hooks ile bitenler)
def is_ours(entry):
    cmd = (entry.get("command") or "")
    return "scripts/hooks/" in cmd
for evt, lst in list(hooks.items()):
    new_lst = []
    for group in lst:
        sub = [h for h in (group.get("hooks") or []) if not is_ours(h)]
        if sub:
            group["hooks"] = sub
            new_lst.append(group)
    if new_lst:
        hooks[evt] = new_lst
    else:
        hooks.pop(evt, None)
d["hooks"] = hooks
with open(p, "w") as f: json.dump(d, f, indent=2)
print("settings.json'dan klipper hook'lari cikarildi.")
PY
    else
      echo "settings.json zaten yok: $TARGET_FILE"
    fi
    exit 0
    ;;
esac

# === install ===

echo "[1/5] Script'lere executable bayragi"
chmod +x "$HOOKS_SRC"/*.sh "$HOOKS_SRC"/*.py 2>/dev/null || true

echo "[2/5] Deploy dizini ($DEPLOY_DIR)"
if [ "$REPO_ROOT" = "$(dirname "$DEPLOY_DIR")/.." ] || [ "$HOOKS_SRC" = "$DEPLOY_DIR" ]; then
  echo "  -> Repo zaten /opt/linux-ai-server altinda, deploy gerekmiyor."
elif [ -w "$(dirname "$DEPLOY_DIR")" ] 2>/dev/null; then
  mkdir -p "$DEPLOY_DIR"
  cp -r "$HOOKS_SRC/." "$DEPLOY_DIR/"
  chmod +x "$DEPLOY_DIR"/*.sh "$DEPLOY_DIR"/*.py 2>/dev/null || true
  echo "  -> Hook'lar $DEPLOY_DIR'a kopyalandi"
elif sudo -n true 2>/dev/null; then
  sudo mkdir -p "$DEPLOY_DIR"
  sudo cp -r "$HOOKS_SRC/." "$DEPLOY_DIR/"
  sudo chmod +x "$DEPLOY_DIR"/*.sh "$DEPLOY_DIR"/*.py 2>/dev/null || true
  echo "  -> Hook'lar $DEPLOY_DIR'a kopyalandi (sudo)"
else
  echo "  UYARI: Deploy dizinine yazma yetkisi yok. Settings.json doğrudan repo path'i kullanacak."
  DEPLOY_DIR="$HOOKS_SRC"
fi

echo "[3/5] Log dizini ($LOG_DIR)"
if [ -w "$(dirname "$LOG_DIR")" ] 2>/dev/null; then
  mkdir -p "$LOG_DIR"
elif sudo -n true 2>/dev/null; then
  sudo mkdir -p "$LOG_DIR"
  sudo chown "$(id -u):$(id -g)" "$LOG_DIR" 2>/dev/null || true
fi

echo "[4/5] settings.json hazirlik ($TARGET_FILE)"
mkdir -p "$TARGET_DIR"

EXAMPLE="$HOOKS_SRC/settings.json.example"
python3 - "$TARGET_FILE" "$EXAMPLE" "$DEPLOY_DIR" <<'PY'
import json, os, sys, shutil, time
target, example, deploy_dir = sys.argv[1], sys.argv[2], sys.argv[3]

with open(example) as f:
    new = json.load(f)

# Path'leri deploy_dir'e gore guncelle
def remap(cmd):
    if "/scripts/hooks/" in cmd:
        # ".../scripts/hooks/x.sh" parcasini al
        idx = cmd.find("/scripts/hooks/")
        tail = cmd[idx+len("/scripts/hooks/"):]
        # Once python3 prefiks'ini koru
        if cmd.startswith("python3 "):
            return f"python3 {deploy_dir}/{tail}"
        return f"{deploy_dir}/{tail}"
    return cmd

for evt, groups in (new.get("hooks") or {}).items():
    for group in groups:
        for h in (group.get("hooks") or []):
            if "command" in h:
                h["command"] = remap(h["command"])

# Mevcut settings varsa MERGE — kullanicinin diger ayarlarini bozma
if os.path.exists(target):
    with open(target) as f:
        try:
            cur = json.load(f)
        except Exception:
            cur = {}
    # Backup
    bak = f"{target}.bak.{int(time.time())}"
    shutil.copy2(target, bak)
    print(f"  -> Mevcut settings yedeklendi: {bak}")

    cur.setdefault("env", {}).update(new.get("env") or {})
    cur_hooks = cur.setdefault("hooks", {})
    for evt, groups in (new.get("hooks") or {}).items():
        existing = cur_hooks.get(evt) or []
        # Bizim hook'larimizi (path'i deploy_dir ile baslayan) once cikar, yeniden ekle
        cleaned = []
        for g in existing:
            sub = [h for h in (g.get("hooks") or []) if not (h.get("command","").find(deploy_dir) >= 0 or "/scripts/hooks/" in h.get("command",""))]
            if sub:
                g["hooks"] = sub
                cleaned.append(g)
        cleaned.extend(groups)
        cur_hooks[evt] = cleaned
    out = cur
else:
    out = new

with open(target, "w") as f:
    json.dump(out, f, indent=2)
print(f"  -> {target} guncellendi")
PY

echo "[5/5] Dogrulama"
python3 -c "import json; d=json.load(open('$TARGET_FILE')); print('  Olay sayisi:', len(d.get('hooks') or {}))"

echo ""
echo "Kurulum tamam."
echo "Sonraki adim: yeni bir Claude Code oturumu ac, SessionStart hook'u calismali."
echo "Kontrol:"
echo "  bash $HOOKS_SRC/install.sh --check"
echo "  tail -f $LOG_DIR/hooks.log"
