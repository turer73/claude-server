"""Installer-paketleme regresyon testleri (Codex-re4, PR#290).

Sınıf: automation/ altındaki bir dosya runtime'da fail-CLOSED bağımlılıksa
(yokluğu özelliği sessizce devre-dışı bırakır ya da abort ettirir), install.sh
onu /opt'a KOPYALAMAK zorunda. CI bunu normal testlerle yakalayamıyordu çünkü
testler path'leri repo-lokal dosyalara override ediyor (ör. WRITE_GUARD).

Bilinen iki üye: ci-fixer-settings.json (#242) + spawn-write-guard.sh (Codex-re4;
yokluğunda make_spawn_settings FAIL → her izole-spawn shared-fallback = izolasyon
sessizce kapalı). Yeni fail-closed automation-bağımlılığı eklenirse listeye ekle.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
CONFIG_ENV = (REPO_ROOT / "config" / "env").read_text(encoding="utf-8")

# automation/-altı fail-closed runtime-bağımlılıkları: (dosya, +x-gerekli-mi)
FAIL_CLOSED_DEPS = [
    ("ci-fixer-settings.json", False),
    ("spawn-write-guard.sh", True),
    # klipper-review #316-P2: note-poller.sh (AUTONOMOUS_MODE=1) bu modülü MUTLAK-yoldan çağırır;
    # yoksa spawn `|| echo $last_seen` ile sessizce no-op'lar, Faz-A kill-switch/audit yolu hiç
    # canlı olmaz (docs/autonomous-comms-design.md §5+§10).
    ("note_poller_decide.py", True),
]


def test_fail_closed_deps_exist_in_repo():
    for name, _ in FAIL_CLOSED_DEPS:
        assert (REPO_ROOT / "automation" / name).is_file(), f"automation/{name} repo'da yok"


def test_install_sh_copies_fail_closed_deps():
    """install.sh her fail-closed bağımlılığı /opt/linux-ai-server/automation/'a koymalı."""
    for name, _ in FAIL_CLOSED_DEPS:
        # cp/install satırı: kaynak automation/<name>, hedef /opt/.../automation
        pattern = rf"(cp|install)[^\n]*automation/{re.escape(name)}[^\n]*/opt/linux-ai-server/automation"
        assert re.search(pattern, INSTALL_SH), (
            f"install.sh automation/{name} kopyalamıyor — temiz-kurulumda fail-closed bağımlılık eksik kalır (Codex-re4 sınıfı)"
        )


def test_write_guard_installed_executable():
    """Guard hook'u bash ile çağrılsa da +x kaybı sinsi kırılma sınıfı (disc#1250
    git-mode-644 dersi) — install-satırı -m 0755 kullanmalı."""
    m = re.search(r"install\s+-m\s+0?755[^\n]*spawn-write-guard\.sh", INSTALL_SH)
    assert m, "spawn-write-guard.sh 'install -m 0755' ile kopyalanmıyor (+x garantisi yok)"


def test_lib_default_matches_install_target():
    """Lib'in WRITE_GUARD varsayılan path'i installer'ın koyduğu yerle AYNI olmalı —
    biri değişirse bu test kopukluğu yakalar."""
    lib = (REPO_ROOT / "automation" / "_spawn-worktree-lib.sh").read_text(encoding="utf-8")
    m = re.search(r'WRITE_GUARD="\$\{WRITE_GUARD:-([^}]+)\}"', lib)
    assert m, "lib'de WRITE_GUARD varsayılanı bulunamadı"
    default_path = m.group(1)
    assert default_path == "/opt/linux-ai-server/automation/spawn-write-guard.sh", default_path
    # installer aynı dizine koyuyor mu (dosya-adı + hedef-dizin eşleşmesi)
    assert "/opt/linux-ai-server/automation/" in INSTALL_SH


def test_hardened_service_allows_only_opt_runtime_data_to_be_written():
    """Code-review queue/heartbeat and legacy runtime DBs must survive ProtectSystem=strict."""
    assert "mkdir -p /opt/linux-ai-server/data" in INSTALL_SH
    read_write_line = re.search(r"^ReadWritePaths=(.+)$", INSTALL_SH, re.MULTILINE)
    assert read_write_line is not None
    writable_paths = read_write_line.group(1).split()
    assert "/opt/linux-ai-server/data" in writable_paths
    assert "/opt/linux-ai-server" not in writable_paths


def test_installed_agents_share_one_memory_database():
    """Consciousness, Critic and MemoryConsolidator all honor this EnvironmentFile value."""
    assert re.search(r"^MEMORY_DB=/opt/linux-ai-server/data/claude_memory\.db$", CONFIG_ENV, re.MULTILINE)
