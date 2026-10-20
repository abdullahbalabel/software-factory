# Job image of the lab hand (runner label `lab`): QEMU/KVM, a NoCloud seed builder and a baked Alpine cloud image,
# so a gates job can boot a throwaway VM in about a minute without downloading anything. Built on the factory VM by
# infra/hands/lab/install.py as factory-lab:1. Node is there for actions/checkout.
FROM node:22-bookworm-slim
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends qemu-system-x86 qemu-utils genisoimage git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
ARG ALPINE=3.22.4
ARG IMAGE=nocloud_alpine-${ALPINE}-x86_64-bios-cloudinit-r0.qcow2
RUN mkdir -p /opt/lab && cd /opt/lab \
 && curl -fsSLO "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE%.*}/releases/cloud/${IMAGE}" \
 && curl -fsSLO "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE%.*}/releases/cloud/${IMAGE}.sha512" \
 && echo "$(cut -c1-128 "${IMAGE}.sha512")  ${IMAGE}" | sha512sum -c - \
 && mv "${IMAGE}" alpine.qcow2 && rm "${IMAGE}.sha512" && chmod 444 alpine.qcow2 && echo "${IMAGE}" > IMAGE
COPY factory-vm-run /usr/local/bin/factory-vm-run
RUN chmod 755 /usr/local/bin/factory-vm-run
