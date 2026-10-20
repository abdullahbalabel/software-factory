#!/bin/bash
# Turn on signed release packages for a product (README, "Signed release packages"). Run as root on the factory:
#
#   sudo bash /opt/factory/software-factory/infra/signer/add-product.sh <org/repo> <artifact> <file> <key> \
#     [--new-key [--dn "CN=…, O=…, C=…"] | --import <keystore> <info-file>] \
#     [--kind android] [--workflow gates.yml] [--branch main] [--prefix <name>]
#
#   <artifact> <file>   what the product's CI uploads: the unsigned release package (the CI contract)
#   <key>               key name; /srv/factory/signer/keys/<key>.jks and <key>.txt (alias, passwords, SHA-256)
#   --new-key           make the key here (RSA 4096, 30 years); copy it to the owner's vault afterwards
#   --import            bring an existing keystore; <info-file> holds alias=, store_password=, key_password=
#                       (sha256= optional, checked); delete both copies afterwards
#   neither             reuse a key that is already here (several products, one key)
#   --prefix            start of the published file name, <prefix>-<version>-<shortsha>.apk (default: <file> up to
#                       its first '-')
# Running it again for the same <org/repo> and <artifact> replaces that product's entry.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
[ -x /usr/local/sbin/factory-signer ] || bash "$(dirname "$(readlink -f "$0")")/install.sh"
exec /usr/bin/python3 /usr/local/sbin/factory-signer add "$@"
