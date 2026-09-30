#!/bin/bash
#
# Startup script for VM-2 - installs and runs the flask tutorial blog.
# This is the same work Part 1 did; here it is shipped from the laptop to VM-1
# as instance metadata, and VM-1 hands it to VM-2 as VM-2's startup-script.
#
set -x
exec > >(tee -a /var/log/flaskr-startup.log) 2>&1
echo "VM-2 flaskr startup script beginning at $(date)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git

mkdir -p /srv
cd /srv
rm -rf flask-tutorial
git clone https://github.com/cu-csci-4253-datacenter/flask-tutorial
cd /srv/flask-tutorial

python3 setup.py install || true
pip3 install -e .

export FLASK_APP=flaskr
flask init-db
nohup flask run -h 0.0.0.0 -p 5000 > /var/log/flaskr.log 2>&1 &

echo "VM-2 flaskr startup script finished at $(date)"
