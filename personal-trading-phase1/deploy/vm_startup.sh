#!/usr/bin/env bash
set -euo pipefail
exec > >(tee -a /var/log/personal-trading-bootstrap.log) 2>&1
export DEBIAN_FRONTEND=noninteractive
test -n "${PHASE1_REVISION:-}"
[[ "$PHASE1_REVISION" =~ ^[a-f0-9]{40}$ ]]
apt-get update
apt-get install -y ca-certificates curl git python3
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
ARCH=$(dpkg --print-architecture)
. /etc/os-release
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian %s stable\n' "$ARCH" "$VERSION_CODENAME" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
DEVICE=/dev/disk/by-id/google-phase1-data
for _ in $(seq 1 30); do test -b "$DEVICE" && break; sleep 2; done
test -b "$DEVICE"
TYPE=$(blkid -s TYPE -o value "$DEVICE" || true)
if test -z "$TYPE"; then
  mkfs.ext4 "$DEVICE"
elif test "$TYPE" != ext4; then
  echo 'Existing data disk is not ext4; refusing to format it.' >&2
  exit 1
fi
install -d -m 0755 /srv/personal-trading
UUID=$(blkid -s UUID -o value "$DEVICE")
if ! findmnt --mountpoint /srv/personal-trading >/dev/null; then
  printf 'UUID=%s /srv/personal-trading ext4 defaults,nofail 0 2\n' "$UUID" >> /etc/fstab
  mount /srv/personal-trading
fi
install -d -m 0755 /opt/personal-trading-source
if ! test -d /opt/personal-trading-source/.git; then
  git clone --no-checkout --filter=blob:none https://github.com/signatureproprt-boop/Theta-plus.git /opt/personal-trading-source
fi
git -C /opt/personal-trading-source fetch origin main
git -C /opt/personal-trading-source show "$PHASE1_REVISION:personal-trading-phase1/deploy/auto_update.py" > /usr/local/bin/personal-trading-update.py
python3 /usr/local/bin/personal-trading-update.py --revision "$PHASE1_REVISION"
cat > /etc/systemd/system/personal-trading-update.service <<'UNIT'
[Unit]
Description=Deploy CI-verified personal trading releases
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target
RequiresMountsFor=/srv/personal-trading
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/bin/personal-trading-update.py
TimeoutStartSec=1200
UNIT
cat > /etc/systemd/system/personal-trading-update.timer <<'UNIT'
[Unit]
Description=Check verified personal trading releases every five minutes
[Timer]
OnBootSec=2min
OnUnitInactiveSec=5min
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now personal-trading-update.timer
echo 'PHASE1_BOOTSTRAP_COMPLETE; database volumes retained on separate disk; MCX HOLD.'
