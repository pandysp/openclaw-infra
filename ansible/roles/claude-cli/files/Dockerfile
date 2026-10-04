FROM openclaw-sandbox-custom:latest
USER root
RUN usermod --login ubuntu --home /home/ubuntu --move-home node
COPY claude-cli-ready.py /usr/local/bin/claude-cli-ready
RUN chmod 755 /usr/local/bin/claude-cli-ready
ENV HOME=/home/ubuntu
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/claude-cli-ready"]
