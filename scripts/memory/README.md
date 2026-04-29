# Memory Scripts

Hafiza sistemi scriptleri. Crontab ve hook'lar bu path'leri kullaniyor — tasimayin.

## Scriptler (ust dizinde)

| Script | Konum | Aciklama |
|--------|-------|----------|
| `claude-memory.sh` | `scripts/claude-memory.sh` | CLI helper: `memories`, `sessions`, `tasks`, `bugs`, `search`, `stats` |
| `memory-session-start.sh` | `scripts/memory-session-start.sh` | SessionStart hook — acik bug, okunmamis not, son oturum ozeti |
| `klipper-event.sh` | `scripts/klipper-event.sh` | Genel event POST: session/task/discovery kaydi |
| `klipper-cron-wrap.sh` | `scripts/klipper-cron-wrap.sh` | Cron job wrapper — komut calistir, sonucu command_log'a yaz |

## Hook Scriptleri

`scripts/hooks/` altinda, `settings.json` uzerinden Claude Code'a baglanir:

| Script | Event | Aciklama |
|--------|-------|----------|
| `session-start.sh` | SessionStart | Hafiza baglami yukle |
| `user-prompt-log.sh` | UserPromptSubmit | Kullanici prompt'larini TSV'ye logla |
| `pre-bash-guard.sh` | PreToolUse:Bash | Tehlikeli komutlari engelle |
| `post-bash-capture.sh` | PostToolUse:Bash | Test/build sonuclarini TSV + command_log'a yaz |
| `stop-save-session.py` | Stop | Oturum ozeti kaydet |

## DB

Tum veriler `/opt/linux-ai-server/data/claude_memory.db` SQLite DB'sinde.
Tablolar: `memories`, `sessions`, `tasks_log`, `discoveries`, `notes`, `devices`, `device_projects`, `command_log`
