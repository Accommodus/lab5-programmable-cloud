#!/usr/bin/env python3
"""Runs on VM-1. Uses an explicit service account to create VM-2.

This is the program `part3.py` ships to VM-1 as the `vm1-launch-vm2-code`
metadata attribute. VM-1's startup script downloads it, along with the service
account key and the startup script intended for VM-2, into /srv and runs it.

The interesting part is the authentication: this program is not running on
your laptop and has no user credentials, so it builds its Compute Engine
client from the service account key file that was handed to it in metadata.
"""

import os
import sys
import time

import googleapiclient.discovery
import googleapiclient.errors
import google.oauth2.service_account as service_account

# Files the VM-1 startup script dropped next to this program.
HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(HERE, 'service-credentials.json')
VM2_STARTUP_FILE = os.path.join(HERE, 'vm2-startup-script.sh')
INFO_FILE = os.path.join(HERE, 'vm2-info.txt')

ZONE = os.getenv('VM2_ZONE', 'us-west1-b')
MACHINE_TYPE = os.getenv('VM2_MACHINE_TYPE', 'e2-micro')
VM2_NAME = os.getenv('VM2_NAME', 'flask-vm2')
IMAGE_PROJECT = 'ubuntu-os-cloud'
IMAGE_FAMILY = 'ubuntu-2204-lts'
NETWORK = 'global/networks/default'
FIREWALL_RULE = 'allow-5000'
NETWORK_TAG = 'allow-5000'
FLASK_PORT = '5000'


def log(message):
    print(f'[vm1-launch-vm2] {message}', flush=True)


def wait_for_zone_operation(compute, project, zone, operation):
    log(f'waiting for zone operation {operation}')
    while True:
        result = compute.zoneOperations().get(
            project=project, zone=zone, operation=operation).execute()
        if result['status'] == 'DONE':
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result
        time.sleep(1)


def wait_for_global_operation(compute, project, operation):
    log(f'waiting for global operation {operation}')
    while True:
        result = compute.globalOperations().get(
            project=project, operation=operation).execute()
        if result['status'] == 'DONE':
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result
        time.sleep(1)


def ensure_firewall_rule(compute, project):
    """Create allow-5000 if it is not already in the project."""
    result = compute.firewalls().list(
        project=project, filter=f'name = "{FIREWALL_RULE}"').execute()
    if result.get('items'):
        log(f'firewall rule {FIREWALL_RULE} already exists')
        return
    log(f'creating firewall rule {FIREWALL_RULE}')
    body = {
        'name': FIREWALL_RULE,
        'network': NETWORK,
        'direction': 'INGRESS',
        'priority': 1000,
        'sourceRanges': ['0.0.0.0/0'],
        'targetTags': [NETWORK_TAG],
        'allowed': [{'IPProtocol': 'tcp', 'ports': [FLASK_PORT]}],
    }
    operation = compute.firewalls().insert(project=project, body=body).execute()
    wait_for_global_operation(compute, project, operation['name'])


def delete_if_present(compute, project, zone, name):
    try:
        operation = compute.instances().delete(
            project=project, zone=zone, instance=name).execute()
    except googleapiclient.errors.HttpError as err:
        if err.resp.status == 404:
            return
        raise
    log(f'deleting pre-existing instance {name}')
    wait_for_zone_operation(compute, project, zone, operation['name'])


def create_vm2(compute, project, zone, name, startup_script):
    image = compute.images().getFromFamily(
        project=IMAGE_PROJECT, family=IMAGE_FAMILY).execute()
    log(f'creating {name} from image {image["name"]}')

    config = {
        'name': name,
        'machineType': f'zones/{zone}/machineTypes/{MACHINE_TYPE}',
        'disks': [{
            'boot': True,
            'autoDelete': True,
            'initializeParams': {'sourceImage': image['selfLink']},
        }],
        'networkInterfaces': [{
            'network': NETWORK,
            'accessConfigs': [
                {'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}
            ],
        }],
        # Tag VM-2 so the allow-5000 firewall rule applies to it.
        'tags': {'items': [NETWORK_TAG]},
        'metadata': {
            'items': [
                {'key': 'startup-script', 'value': startup_script},
            ],
        },
        # Deliberately no `serviceAccounts` here: VM-2 only needs to serve a
        # blog, so it gets no cloud credentials at all. Nothing on VM-2 can
        # turn around and create more VMs, and the service account key never
        # leaves VM-1.
    }
    operation = compute.instances().insert(
        project=project, zone=zone, body=config).execute()
    wait_for_zone_operation(compute, project, zone, operation['name'])
    return compute.instances().get(
        project=project, zone=zone, instance=name).execute()


def external_ip(instance):
    for interface in instance.get('networkInterfaces', []):
        for access in interface.get('accessConfigs', []):
            if 'natIP' in access:
                return access['natIP']
    return None


def main():
    project = os.getenv('GOOGLE_CLOUD_PROJECT')
    if not project:
        log('GOOGLE_CLOUD_PROJECT is not set')
        return 1

    if not os.path.exists(CREDENTIALS_FILE):
        log(f'missing {CREDENTIALS_FILE}')
        return 1
    if not os.path.exists(VM2_STARTUP_FILE):
        log(f'missing {VM2_STARTUP_FILE}')
        return 1

    with open(VM2_STARTUP_FILE) as f:
        startup_script = f.read()

    # Authenticate as the service account rather than as a human user.
    credentials = service_account.Credentials.from_service_account_file(
        filename=CREDENTIALS_FILE)
    compute = googleapiclient.discovery.build(
        'compute', 'v1', credentials=credentials)
    log(f'authenticated as service account {credentials.service_account_email}')
    log(f'project {project}, zone {ZONE}')

    ensure_firewall_rule(compute, project)
    delete_if_present(compute, project, ZONE, VM2_NAME)
    instance = create_vm2(compute, project, ZONE, VM2_NAME, startup_script)

    ip = external_ip(instance)
    url = f'http://{ip}:{FLASK_PORT}'
    log(f'VM-2 {VM2_NAME} is {instance["status"]} at {ip}')
    log(f'the flask blog will be available at {url}')

    # Leave a breadcrumb on VM-1's disk so you can find the URL by ssh-ing in.
    with open(INFO_FILE, 'w') as f:
        f.write(f'{VM2_NAME} {ip} {url}\n')

    return 0


if __name__ == '__main__':
    sys.exit(main())
