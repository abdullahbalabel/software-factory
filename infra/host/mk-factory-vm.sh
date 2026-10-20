#!/bin/bash
# Create a factory VM on a KVM host from the Ubuntu 24.04 cloud image (cloud-init NoCloud seed).
# Network factory-net (infra/host/factory-net.xml), static address, public DNS. Admin user `factory`, key-only.
# Usage (on the host, as root): mk-factory-vm.sh /root/factory/factory-core.pub   (the factory's own admin key)
# Defaults create the factory itself (factory-core, 10.88.0.10, 4 vCPU, 16 GiB, 150 GB). Another VM on the same
# network (a new server for a move, or a throwaway test of infra/factory.sh) takes its own name, address and MAC:
#   NAME=factory-restore-test IP=10.88.0.20 MAC=52:54:00:88:00:14 DISK=60 mk-factory-vm.sh key.pub
# Then install the factory on it with infra/factory.sh (README: "Move or reinstall the factory").
set -euo pipefail
NAME=${NAME:-factory-core}
BASE=/var/lib/libvirt/images/base/noble-server-cloudimg-amd64.img
IMG=/var/lib/libvirt/images
SEED=/var/lib/libvirt/images/seed
KEY=$(cat "${1:?admin public key}")
MAC=${MAC:-52:54:00:88:00:0a}
IP=${IP:-10.88.0.10}
VCPU=${VCPU:-4} MEM=${MEM:-16384} DISK=${DISK:-150}

if virsh dominfo "$NAME" >/dev/null 2>&1; then echo "exists: $NAME"; exit 0; fi
mkdir -p "$SEED"
d=$(mktemp -d)
cat >"$d/meta-data" <<EOF
instance-id: $NAME-1
local-hostname: $NAME
EOF
cat >"$d/user-data" <<EOF
#cloud-config
hostname: $NAME
manage_etc_hosts: true
disable_root: true
ssh_pwauth: false
users:
  - name: factory
    gecos: factory admin
    groups: [sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: true
    ssh_authorized_keys:
      - $KEY
timezone: Etc/UTC
package_update: true
package_upgrade: true
package_reboot_if_required: false
packages: [qemu-guest-agent]
write_files:
  - path: /etc/ssh/sshd_config.d/10-factory.conf
    content: |
      PasswordAuthentication no
      KbdInteractiveAuthentication no
      PermitRootLogin no
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent]
  - [touch, /etc/cloud/cloud-init.disabled]
power_state:
  mode: reboot
  message: factory first boot done
  timeout: 30
  condition: true
EOF
cat >"$d/network-config" <<EOF
version: 2
ethernets:
  fac0:
    match:
      macaddress: "$MAC"
    set-name: fac0
    addresses: [$IP/24]
    routes:
      - to: default
        via: 10.88.0.1
    nameservers:
      addresses: [1.1.1.1, 9.9.9.9]
EOF
cloud-localds -N "$d/network-config" "$SEED/$NAME-seed.iso" "$d/user-data" "$d/meta-data"
rm -rf "$d"
qemu-img convert -O qcow2 "$BASE" "$IMG/$NAME.qcow2"
qemu-img resize "$IMG/$NAME.qcow2" "${DISK}G"
virt-install --name "$NAME" --memory "$MEM" --vcpus "$VCPU" --cpu host-passthrough \
  --osinfo ubuntu24.04 --machine q35 --boot uefi \
  --disk "path=$IMG/$NAME.qcow2,format=qcow2,bus=virtio,discard=unmap" \
  --disk "path=$SEED/$NAME-seed.iso,device=cdrom,bus=sata" \
  --network "network=factory-net,model=virtio,mac=$MAC" \
  --graphics vnc,listen=127.0.0.1 --console pty,target_type=serial \
  --channel unix,target_type=virtio,name=org.qemu.guest_agent.0 \
  --memballoon none --rng /dev/urandom \
  --import --noautoconsole --autostart
echo "created: $NAME $MAC $IP"
