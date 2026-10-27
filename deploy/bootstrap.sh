#!/bin/sh
# Run on the server as root after cloning this repository.
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run as root (or invoke with sudo).' >&2
  exit 1
fi
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
  echo 'Usage: sudo sh deploy/bootstrap.sh /absolute/path/to/private-settings.yml' >&2
  exit 1
fi
case "$1" in /*) settings=$1 ;; *) echo 'Settings path must be absolute.' >&2; exit 1 ;; esac
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ansible-core git python3 util-linux
exec flock --nonblock /run/pos-deploy.lock ansible-playbook \
  --inventory localhost, --connection local \
  "$script_dir/../ansible/deploy.yml" --extra-vars "@$settings"
