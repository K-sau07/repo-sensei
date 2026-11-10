#!/usr/bin/env bash
# The sandbox host (10.12): a dedicated Linux VM (Ubuntu 24.04, e.g. a Hetzner Cloud server)
# where fixes run a repository's own code. Docker with gVisor (runsc) as the runtime every
# sandbox container gets, a firewall that only lets SSH in, and the worker for the agent and
# sandbox queues (the fix stages), run natively as a systemd service: the sandbox bind-mounts
# checkouts by their paths on this host. Run as root on a fresh server:
#
#   REF=<tag or commit> bash setup.sh
#
# then put the secrets in /etc/repo-sensei/worker.env (see worker.env.example) and
#   systemctl enable --now sensei-sandbox-worker
set -euo pipefail

REF="${REF:?the repo-sensei tag or commit to run}"
REPO_URL="${REPO_URL:-https://github.com/K-sau07/repo-sensei.git}"
UV_VERSION="${UV_VERSION:-0.13.1}"  # as the images'
HOME_DIR=/opt/repo-sensei

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg git make ufw

# Docker Engine, from Docker's apt repository.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
# shellcheck source=/dev/null
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  > /etc/apt/sources.list.d/docker.list
# gVisor, from its apt repository (installing it registers runsc with Docker).
curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor --yes -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  > /etc/apt/sources.list.d/gvisor.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin runsc
runsc install
systemctl reload docker
docker info --format '{{json .Runtimes}}' | grep -q runsc || { echo "runsc isn't a Docker runtime" >&2; exit 1; }
# A container under gVisor really is: its kernel is gVisor's.
docker run --rm --runtime=runsc busybox:1.37 dmesg | grep -qi gvisor \
  || { echo "a runsc container didn't run under gVisor" >&2; exit 1; }

# Only SSH in; everything the worker does is outbound (Redis, Postgres, GitHub, the models).
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

# The worker's user. In the docker group: it starts the sandboxes (root-equivalent on this host,
# which is why the host runs nothing else and holds no secret beyond the worker's).
id sensei >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin sensei
usermod -aG docker sensei
install -d -o sensei -g sensei -m 0750 /data/repos
install -d -m 0750 -o root -g sensei /etc/repo-sensei

# The code at REF, and its environment (uv, pinned).
curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | env UV_INSTALL_DIR=/usr/local/bin sh
if [[ ! -d "$HOME_DIR/src/.git" ]]; then
  sudo -u sensei git clone --quiet "$REPO_URL" "$HOME_DIR/src"
fi
sudo -u sensei git -C "$HOME_DIR/src" fetch --quiet --tags origin
sudo -u sensei git -C "$HOME_DIR/src" checkout --quiet --detach "$REF"
sudo -u sensei -H bash -c "cd '$HOME_DIR/src' && uv sync --frozen --no-dev --package sensei-workers"
# The sandbox's images (the runners and the registry proxy).
sudo -u sensei -H bash -c "cd '$HOME_DIR/src' && make sandbox-image"

install -m 0644 "$HOME_DIR/src/infra/sandbox-host/sensei-sandbox-worker.service" /etc/systemd/system/
systemctl daemon-reload
echo "Set /etc/repo-sensei/worker.env (owner root:sensei, mode 0640), then:"
echo "  systemctl enable --now sensei-sandbox-worker"
