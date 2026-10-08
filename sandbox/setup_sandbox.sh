#!/bin/bash
# Provision a disposable coding-agent sandbox (Ubuntu 24.04 container/VM) for scripts/code_task.py.
# Run as root INSIDE the sandbox:
#
#   bash setup_sandbox.sh "<hermes ssh public key>" <hermes-host-ip>
#
# Installs git, build tools, python3/venv, uv, Node.js 22 (NodeSource), Docker, gh, Codex CLI and
# Claude Code CLI; creates user `agent` (in the docker group) with ~/jobs; authorizes ONLY the given key,
# restricted to connections from <hermes-host-ip>; sshd allows only `agent`, keys only.
# The sandbox holds no git/forge credentials; the only secret that ever lands here is the coding
# agent's own login (e.g. ~/.codex/auth.json after `codex login --device-auth`).
set -euo pipefail
PUBKEY="${1:?usage: setup_sandbox.sh '<ssh public key>' <hermes-ip>}"
FROM_IP="${2:?usage: setup_sandbox.sh '<ssh public key>' <hermes-ip>}"
export DEBIAN_FRONTEND=noninteractive

apt-get update -q
apt-get -y -q full-upgrade
apt-get install -y -q git curl ca-certificates gnupg build-essential python3 python3-venv python3-pip \
    python3-dev rsync jq unzip ripgrep openssh-server locales
locale-gen en_US.UTF-8 >/dev/null && update-locale LANG=en_US.UTF-8

# Node.js 22 LTS
curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/nodesource_setup.sh
bash /tmp/nodesource_setup.sh
apt-get install -y -q nodejs

# Docker (for test suites that need it; the container needs nesting=1,keyctl=1 on Proxmox) + gh
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
chmod a+r /etc/apt/keyrings/docker.asc /etc/apt/keyrings/githubcli-archive-keyring.gpg
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
apt-get update -q
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin gh

# uv (system-wide) and the coding agents
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
npm i -g @openai/codex @anthropic-ai/claude-code

# agent user
id agent >/dev/null 2>&1 || useradd -m -s /bin/bash agent
AH="$(getent passwd agent | cut -d: -f6)"
usermod -aG docker agent
install -d -m 700 -o agent -g agent $AH/.ssh
install -d -m 755 -o agent -g agent $AH/jobs
printf 'restrict,from="%s" %s\n' "$FROM_IP" "$PUBKEY" > $AH/.ssh/authorized_keys
chown agent:agent $AH/.ssh/authorized_keys
chmod 600 $AH/.ssh/authorized_keys

cat > /etc/ssh/sshd_config.d/60-sandbox.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers agent
EOF
systemctl enable --now ssh
systemctl restart ssh

echo "node $(node --version) | $(uv --version) | $(su - agent -c 'codex --version') | claude $(su - agent -c 'claude --version')"
echo "Next: log the agent in once:  su - agent -c 'codex login --device-auth'"
