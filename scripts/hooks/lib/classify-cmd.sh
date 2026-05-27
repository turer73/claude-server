#!/bin/bash
# classify-cmd.sh — komutu test/lint/build sinifina map eder.
#
# Tek-kaynak: post-bash-capture.sh hook'u bu lib'i source eder.
# Test: scripts/hooks/tests/test_classify_cmd.sh (executor+tool kombinasyonu)
#
# Tasarim:
#   Salt anahtar kelime ("vitest", "playwright" vs.) yakalamak FP yaratir.
#   Ornek: 'docker images | grep playwright' rc=1 -> CLASS=playwright -> sahte bug
#   (#485, #499 vakalari). Bu yuzden executor (npx/pnpm/yarn) VEYA tool'un
#   kendi subcommand'i ('playwright test', 'vitest run') zorunlu kilindi.
#
# Kullanim:
#   . "$(dirname "$0")/lib/classify-cmd.sh"
#   CLASS=$(classify_cmd "$CMD")   # bos string ise eslem yok

classify_cmd() {
    local CMD="$1"
    local CLASS=""
    case "$CMD" in
        *pytest*)                                                                                  CLASS="pytest" ;;
        *"npm test"*|*"npm run test"*|*"npm run build"*|*"npm run lint"*|*"npm run typecheck"*)    CLASS="npm" ;;
        *"yarn test"*|*"yarn build"*|*"yarn lint"*)                                                CLASS="yarn" ;;
        *"pnpm test"*|*"pnpm build"*|*"pnpm lint"*|*"pnpm exec tsc"*|*"pnpm typecheck"*)           CLASS="pnpm" ;;
        *" tsc "*|*" tsc"|"tsc "*|"tsc")                                                           CLASS="tsc" ;;
        *" ruff "*|*" ruff"|"ruff "*|"ruff")                                                       CLASS="ruff" ;;
        *" mypy "*|*" mypy"|"mypy "*|"mypy")                                                       CLASS="mypy" ;;
        # Executor+tool kombinasyonu — salt 'grep playwright', 'docker images | grep vitest'
        # gibi sorgu komutlarini hatali sekilde test runner zannetmemek icin (bug #485, #499).
        *"npx vitest"*|*"pnpm vitest"*|*"pnpm exec vitest"*|*"yarn vitest"*|*"vitest run"*)        CLASS="vitest" ;;
        *"npx jest"*|*"pnpm jest"*|*"pnpm exec jest"*|*"yarn jest"*|*"jest --"*|*"jest "*)         CLASS="jest" ;;
        *"npx playwright"*|*"pnpm playwright"*|*"pnpm exec playwright"*|*"yarn playwright"*|*"playwright test"*|*"playwright install"*)  CLASS="playwright" ;;
        *"npx eslint"*|*"pnpm eslint"*|*"pnpm exec eslint"*|*"yarn eslint"*|*"eslint --"*|*"eslint ."*|*"eslint src"*) CLASS="eslint" ;;
        *"cargo test"*|*"cargo build"*|*"cargo check"*)                                            CLASS="cargo" ;;
        *"go test"*|*"go build"*)                                                                  CLASS="go" ;;
        *"make test"*|*"make check"*|*"make build"*)                                               CLASS="make" ;;
        *" black "*|"black "*)                                                                     CLASS="black" ;;
    esac
    printf '%s' "$CLASS"
}
