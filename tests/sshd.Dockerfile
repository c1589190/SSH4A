FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server less python3 && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_*
RUN useradd --create-home --shell /bin/bash fixture && echo 'fixture:ssh4a-fixture-only' | chpasswd
COPY tests/start-sshd.sh /usr/local/bin/start-sshd
ENTRYPOINT ["sh", "/usr/local/bin/start-sshd"]
