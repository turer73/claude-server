# koken-akademi self-hosted GitHub Actions runner — Docker-ephemeral, owner-only.
# Job'lar bu container içinde non-root 'runner' olarak koşar; host'a sudo/mount YOK.
# Eski host-tabanlı tasarımın (NOPASSWD-root klipperos) güvenli yerine geçer (#1175 rework).
FROM node:20-bookworm-slim

# Runner çalışma-zamanı + koken build/deploy araçları (wrangler). libicu = .NET runner bağımlılığı.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git jq libicu72 \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g wrangler@3 && npm cache clean --force

# Reproducible: runner sürümü build-arg ile pinlenir (setup scripti latest'i geçirir).
ARG RUNNER_VERSION
RUN test -n "$RUNNER_VERSION" || { echo "RUNNER_VERSION build-arg zorunlu"; exit 1; }

RUN useradd -m -d /home/runner runner
WORKDIR /home/runner

RUN curl -fsSL "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
      | tar xz \
    && ./bin/installdependencies.sh \
    && chown -R runner:runner /home/runner

COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
USER runner
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
