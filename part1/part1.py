#!/usr/bin/env python3
"""Part 1 - Create a VM, install the flask tutorial app on it, and open port 5000.

This program performs, through the Compute Engine API, the same steps you would
otherwise click through in the Google Cloud Console:

  1. look up the current image in the `ubuntu-2204-lts` family
  2. create an `allow-5000` firewall rule (only if it does not already exist)
  3. insert an instance with a startup script that installs `flaskr`
  4. tag the instance `allow-5000` with setTags so the firewall rule applies
  5. read the instance's external IP back out of the API and print its URL

Adapted from the Google Cloud Python sample
https://github.com/GoogleCloudPlatform/python-docs-samples
(compute/api/create_instance.py), Apache 2.0 licensed.
"""

import argparse
import sys
import time

import google.auth
import googleapiclient.discovery
import googleapiclient.errors

# ---------------------------------------------------------------- configuration

ZONE = 'us-west1-b'
MACHINE_TYPE = 'f1-micro'
IMAGE_PROJECT = 'ubuntu-os-cloud'
IMAGE_FAMILY = 'ubuntu-2204-lts'
NETWORK = 'global/networks/default'

FIREWALL_RULE = 'allow-5000'
NETWORK_TAG = 'allow-5000'
FLASK_PORT = '5000'

FLASK_REPO = 'https://github.com/cu-csci-4253-datacenter/flask-tutorial'

# The startup script is run as root by the guest environment on first boot.
# It runs in `/` by default, so we do our work in a directory of our own, and
# everything is teed into a log file to make debugging easier than digging
# through /var/log/syslog.
STARTUP_SCRIPT = f"""#!/bin/bash
set -x
exec > >(tee -a /var/log/flaskr-startup.log) 2>&1
echo "flaskr startup script beginning at $(date)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git

mkdir -p /srv
cd /srv
rm -rf flask-tutorial
git clone {FLASK_REPO}
cd /srv/flask-tutorial

# Install the flaskr package (and its Flask dependency) system wide.
python3 setup.py install || true
pip3 install -e .

# Initialize the sqlite database the blog stores its posts in, then start the
# app listening on every interface so it is reachable via the external IP.
# nohup keeps flask alive after this startup script exits.
export FLASK_APP=flaskr
flask init-db
nohup flask run -h 0.0.0.0 -p {FLASK_PORT} > /var/log/flaskr.log 2>&1 &

echo "flaskr startup script finished at $(date)"
"""


# ------------------------------------------------------------------- API helpers

def wait_for_zone_operation(compute, project, zone, operation):
    """Block until a zonal operation finishes, then return it."""
    print(f'  waiting for zone operation {operation} ', end='', flush=True)
    while True:
        result = compute.zoneOperations().get(
            project=project, zone=zone, operation=operation).execute()
        if result['status'] == 'DONE':
            print(' done')
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result
        print('.', end='', flush=True)
        time.sleep(1)


def wait_for_global_operation(compute, project, operation):
    """Block until a global operation (e.g. a firewall change) finishes."""
    print(f'  waiting for global operation {operation} ', end='', flush=True)
    while True:
        result = compute.globalOperations().get(
            project=project, operation=operation).execute()
        if result['status'] == 'DONE':
            print(' done')
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result
        print('.', end='', flush=True)
        time.sleep(1)


def list_instances(compute, project, zone):
    """Stub code from the assignment template - list every instance in a zone."""
    result = compute.instances().list(project=project, zone=zone).execute()
    return result.get('items', [])


def get_instance(compute, project, zone, name):
    """Return the instance resource, or None if no such instance exists."""
    try:
        return compute.instances().get(
            project=project, zone=zone, instance=name).execute()
    except googleapiclient.errors.HttpError as err:
        if err.resp.status == 404:
            return None
        raise


# ------------------------------------------------------------------- firewall

def firewall_rule_exists(compute, project, name):
    """Check by name whether a firewall rule already exists.

    We use firewalls().list() with a filter instead of firewalls().get() so
    that a missing rule is an empty result rather than a 404 exception.
    """
    result = compute.firewalls().list(
        project=project, filter=f'name = "{name}"').execute()
    return len(result.get('items', [])) > 0


def create_firewall_rule(compute, project, name, tag, port):
    """Create a VPC firewall rule allowing TCP `port` from anywhere.

    The rule is scoped by a network tag so that it only opens the port on
    instances we explicitly tag, rather than on every VM in the network.
    """
    config = {
        'name': name,
        'description': f'Allow TCP {port} from anywhere to instances tagged {tag}',
        'network': NETWORK,
        'direction': 'INGRESS',
        'priority': 1000,
        'sourceRanges': ['0.0.0.0/0'],
        'targetTags': [tag],
        'allowed': [{'IPProtocol': 'tcp', 'ports': [port]}],
    }
    return compute.firewalls().insert(project=project, body=config).execute()


def ensure_firewall_rule(compute, project):
    """Create the allow-5000 rule if it is not already there."""
    if firewall_rule_exists(compute, project, FIREWALL_RULE):
        print(f'Firewall rule "{FIREWALL_RULE}" already exists - not creating it again.')
        return
    print(f'Creating firewall rule "{FIREWALL_RULE}" (tcp:{FLASK_PORT} from 0.0.0.0/0, '
          f'target tag "{NETWORK_TAG}")')
    operation = create_firewall_rule(
        compute, project, FIREWALL_RULE, NETWORK_TAG, FLASK_PORT)
    wait_for_global_operation(compute, project, operation['name'])


# ------------------------------------------------------------------- instances

def create_instance(compute, project, zone, name, machine_type):
    """Insert a VM running the latest image of the ubuntu-2204-lts family."""
    # Resolve the image family to a concrete image so we do not hard-code a
    # particular Ubuntu version.
    image_response = compute.images().getFromFamily(
        project=IMAGE_PROJECT, family=IMAGE_FAMILY).execute()
    source_disk_image = image_response['selfLink']
    print(f'Using image {image_response["name"]} from family {IMAGE_FAMILY}')

    config = {
        'name': name,
        'machineType': f'zones/{zone}/machineTypes/{machine_type}',

        # A single boot disk, created from the Ubuntu image and deleted with
        # the instance.
        'disks': [{
            'boot': True,
            'autoDelete': True,
            'initializeParams': {
                'sourceImage': source_disk_image,
            },
        }],

        # Attach to the default network and give the VM an ephemeral external
        # IP address via one-to-one NAT so it is reachable from the Internet.
        'networkInterfaces': [{
            'network': NETWORK,
            'accessConfigs': [
                {'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}
            ],
        }],

        # The instance needs read/write access to storage and logging so the
        # guest environment can report startup script output.
        'serviceAccounts': [{
            'email': 'default',
            'scopes': [
                'https://www.googleapis.com/auth/devstorage.read_write',
                'https://www.googleapis.com/auth/logging.write',
            ],
        }],

        # Metadata is readable from inside the instance; the guest environment
        # automatically executes the value of the `startup-script` key on boot.
        'metadata': {
            'items': [
                {'key': 'startup-script', 'value': STARTUP_SCRIPT},
            ],
        },
    }

    return compute.instances().insert(
        project=project, zone=zone, body=config).execute()


def set_network_tag(compute, project, zone, name, tag):
    """Apply a network tag to a running instance with setTags.

    setTags requires the current tags fingerprint, which acts as an optimistic
    concurrency check, so we have to read the instance first.
    """
    instance = compute.instances().get(
        project=project, zone=zone, instance=name).execute()
    tags = instance.get('tags', {})
    existing = tags.get('items', [])
    if tag in existing:
        print(f'Instance {name} is already tagged "{tag}".')
        return
    body = {
        'items': existing + [tag],
        'fingerprint': tags.get('fingerprint', ''),
    }
    print(f'Applying network tag "{tag}" to {name} with setTags')
    operation = compute.instances().setTags(
        project=project, zone=zone, instance=name, body=body).execute()
    wait_for_zone_operation(compute, project, zone, operation['name'])


def external_ip(instance):
    """Pull the ephemeral external IP out of an instance resource."""
    for interface in instance.get('networkInterfaces', []):
        for access in interface.get('accessConfigs', []):
            if 'natIP' in access:
                return access['natIP']
    return None


def delete_instance(compute, project, zone, name):
    print(f'Deleting instance {name}')
    operation = compute.instances().delete(
        project=project, zone=zone, instance=name).execute()
    wait_for_zone_operation(compute, project, zone, operation['name'])


# ------------------------------------------------------------------------ main

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='flask-vm',
                        help='name of the instance to create (default: flask-vm)')
    parser.add_argument('--zone', default=ZONE, help=f'zone (default: {ZONE})')
    parser.add_argument('--machine-type', default=MACHINE_TYPE,
                        help=f'machine type (default: {MACHINE_TYPE})')
    parser.add_argument('--project', default=None,
                        help='project id (default: from application default credentials)')
    parser.add_argument('--recreate', action='store_true',
                        help='delete the instance first if it already exists')
    parser.add_argument('--list', action='store_true',
                        help='just list the instances in the zone and exit')
    args = parser.parse_args()

    # Application default credentials: `gcloud auth application-default login`
    # on a laptop, or the attached service account when running on a VM.
    credentials, default_project = google.auth.default()
    project = args.project or default_project
    compute = googleapiclient.discovery.build(
        'compute', 'v1', credentials=credentials)

    if args.list:
        print(f'Your running instances in {args.zone} are:')
        for instance in list_instances(compute, project, args.zone):
            print(' ', instance['name'], instance['status'])
        return 0

    print(f'Project: {project}   Zone: {args.zone}')

    # Step 1 - the firewall rule is a VPC-level resource, so create it only once.
    ensure_firewall_rule(compute, project)

    # Step 2 - create the instance (or reuse/replace an existing one).
    existing = get_instance(compute, project, args.zone, args.name)
    if existing and args.recreate:
        delete_instance(compute, project, args.zone, args.name)
        existing = None
    if existing:
        print(f'Instance {args.name} already exists - reusing it. '
              f'Pass --recreate to replace it.')
    else:
        print(f'Creating instance {args.name} ({args.machine_type})')
        operation = create_instance(
            compute, project, args.zone, args.name, args.machine_type)
        wait_for_zone_operation(compute, project, args.zone, operation['name'])

    # Step 3 - tag the instance so the allow-5000 firewall rule applies to it.
    set_network_tag(compute, project, args.zone, args.name, NETWORK_TAG)

    # Step 4 - read the external IP back out of the API and tell the user.
    instance = compute.instances().get(
        project=project, zone=args.zone, instance=args.name).execute()
    ip = external_ip(instance)
    if not ip:
        print('Could not determine the external IP address of the instance.',
              file=sys.stderr)
        return 1

    print()
    print('=' * 60)
    print(f'Instance {args.name} is {instance["status"]} at external IP {ip}')
    print()
    print('The startup script still needs a minute or two to install and')
    print('launch the application. Then visit the flask blog at:')
    print()
    print(f'    http://{ip}:{FLASK_PORT}')
    print()
    print('=' * 60)
    return 0


if __name__ == '__main__':
    sys.exit(main())
