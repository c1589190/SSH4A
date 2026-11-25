#!/bin/sh
set -eu

mkdir -p /run/sshd
ssh-keygen -q -t ed25519 -N '' -f /run/sshd/host_key
cat > /run/sshd/fixture_config <<'CONFIG'
PasswordAuthentication yes
KbdInteractiveAuthentication no
PermitRootLogin no
UsePAM no
AllowUsers fixture
CONFIG

if [ "${SSH4A_TEST_NO_SFTP:-}" != 1 ]; then
    echo 'Subsystem sftp internal-sftp' >> /run/sshd/fixture_config
fi
exec /usr/sbin/sshd -D -e -h /run/sshd/host_key -f /run/sshd/fixture_config
