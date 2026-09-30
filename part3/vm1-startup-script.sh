#!/bin/bash
#
# Startup script for VM-1.
#
# Everything VM-1 needs was passed to it as instance metadata by part3.py:
#   vm1-launch-vm2-code  the python program that creates VM-2
#   vm2-startup-script   the startup script that VM-2 should run
#   service-credentials  the service account key VM-1 authenticates with
#   project              the project id to create VM-2 in
#
# We pull each one off the metadata server at 169.254.169.254 (aka `metadata`)
# and then run the program.
#
set -x
exec > >(tee -a /var/log/vm1-startup.log) 2>&1
echo "VM-1 startup script beginning at $(date)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-pip curl

mkdir -p /srv
cd /srv

MD="http://metadata/computeMetadata/v1/instance/attributes"
HDR="Metadata-Flavor: Google"

curl -s "$MD/vm1-launch-vm2-code" -H "$HDR" > vm1-launch-vm2.py
curl -s "$MD/vm2-startup-script"  -H "$HDR" > vm2-startup-script.sh
curl -s "$MD/service-credentials" -H "$HDR" > service-credentials.json

# The credentials are a secret: keep them readable only by root.
chmod 600 /srv/service-credentials.json

# NOTE: no space after the `=`, and the filename has to match what we
# actually downloaded above - those are the two bugs in the README example.
export GOOGLE_CLOUD_PROJECT=$(curl -s "$MD/project" -H "$HDR")
echo "VM-1 will create VM-2 in project $GOOGLE_CLOUD_PROJECT"

pip3 install --upgrade google-api-python-client google-auth google-auth-httplib2 google-auth-oauthlib \
  || pip3 install --break-system-packages --upgrade google-api-python-client google-auth google-auth-httplib2 google-auth-oauthlib

cd /srv
python3 ./vm1-launch-vm2.py

echo "VM-1 startup script finished at $(date)"
