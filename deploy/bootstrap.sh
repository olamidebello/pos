#!/bin/sh
# Run on the server as root after cloning this repository.
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run as root (or invoke with sudo).' >&2
  exit 1
fi
if [ "$#" -lt 1 ] || [ "$#" -gt 2 ] || [ ! -f "$1" ]; then
  echo 'Usage: sudo sh deploy/bootstrap.sh /absolute/path/to/private-settings.yml [--olamidepanel]' >&2
  exit 1
fi
playbook=deploy.yml
if [ "$#" -eq 2 ]; then
  if [ "$2" != --olamidepanel ]; then
    echo 'Unknown deployment mode; use --olamidepanel or omit the second argument.' >&2
    exit 1
  fi
  playbook=deploy-olamidepanel.yml
fi
case "$1" in /*) settings=$1 ;; *) echo 'Settings path must be absolute.' >&2; exit 1 ;; esac
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ansible-core git python3 util-linux
exec flock --nonblock /run/pos-deploy.lock ansible-playbook \
  --inventory localhost, --connection local \
  "$script_dir/../ansible/$playbook" --extra-vars "@$settings"
