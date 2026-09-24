FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates git openssh-client iptables python3 util-linux passwd \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --home-dir /home/sync --shell /bin/bash workspace-sync
COPY workspace-sync.sh workspace-sync-entrypoint.sh /usr/local/bin/
COPY gitignore-workspace /etc/workspace-sync/excludes
RUN chmod 755 /usr/local/bin/workspace-sync*.sh
ENV HOME=/home/sync
WORKDIR /
ENTRYPOINT ["/usr/local/bin/workspace-sync-entrypoint.sh"]
CMD ["/usr/local/bin/workspace-sync.sh"]
