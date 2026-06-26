# koken-akademi self-hosted GitHub Actions runner (güvenli)

Docker-ephemeral, **owner-only** self-hosted runner. Eski host-tabanlı tasarımın
(job'lar NOPASSWD-root `klipperos` olarak host'ta koşuyordu — Codex #222: 3×P1)
güvenli yerine geçer (#1175 rework).

## Güvenlik modeli

| Katman | Tasarım |
|--------|---------|
| **İş yükü izolasyonu** | Her job TAZE container'da (`--rm` + `--ephemeral`), non-root `runner` olarak. Job-arası state-sızıntısı yok, host-mount/socket yok. |
| **Host yetkisi** | Job'lar host'a `sudo` yapamaz. Eski tasarımdaki `klipperos` NOPASSWD-root KULLANILMAZ. |
| **Orkestratör** | `kokenrunner` (system user, `nologin`, **sudo YOK**) yalnız token-mint + `docker run`. |
| **Credential** | Fine-grained PAT host'ta `/etc/koken-runner/pat` (root-only 0640). Container'a yalnız **kısa-ömürlü registration-token** girer — PAT job'a asla ulaşmaz. |
| **Kayıt** | `--replace` (bayat/offline kayıt devralma, çakışma fix). |
| **Restart** | Job-cycle wrapper-içi döngü → normal job'lar systemd-restart değil. `StartLimitBurst` YOK (eski guard normal yükü kırıyordu). Mint-fail'de exponential backoff (API-spam yok). |
| **Temizlik** | `KillMode` default (control-group) → durdurmada child-process'ler temizlenir. |

## Tehdit modeli: OWNER-ONLY
Yalnız sahip-tetikli build/deploy. **Fork/dış-PR kodu bu runner'da çalıştırılmamalı.**
Workflow'da `pull_request_target` + `self-hosted` label kombinasyonundan kaçının.

## Dosyalar
- `Dockerfile` — node20 + wrangler + actions-runner (sürüm pinli), non-root `runner`.
- `entrypoint.sh` — container-içi: REG_TOKEN ile `--ephemeral --replace` config + tek job.
- `run-ephemeral-loop.sh` — host orkestratör: mint→`docker run`→tekrar; backoff'lu.
- `koken-runner.service` — systemd unit (User=kokenrunner, Restart=always, StartLimitBurst yok).
- `../../scripts/setup-gh-runner.sh` — kurucu (kullanıcı + image build + unit).

## Kurulum
```bash
bash scripts/setup-gh-runner.sh
# sonra: PAT'ı yerleştir + 'sudo systemctl enable --now koken-runner' (script çıktısına bak)
```

## Test edilebilirlik
`run-ephemeral-loop.sh` env-override'ları (test/CI): `KOKEN_RUNNER_MAX_CYCLES` (döngüyü sınırla),
`KOKEN_RUNNER_BACKOFF_BASE` (backoff hızlandır), `KOKEN_RUNNER_PAT_FILE` / `_REPO` / `_IMAGE` / `_LOG`.
