#!/usr/bin/env python3
"""Part 3 - Use a service account so that a VM can create another VM.

    laptop (part3.py)  --creates-->  VM-1  --creates-->  VM-2 (flask blog)

`part3.py` authenticates with an explicit service account key
(`service-credentials.json`) instead of your own user credentials, and creates
VM-1. Everything VM-1 needs is passed to it as instance metadata:

    startup-script        vm1-startup-script.sh  (run automatically on boot)
    vm1-launch-vm2-code   vm1-launch-vm2.py      (VM-1 runs this)
    vm2-startup-script    vm2-startup-script.sh  (VM-1 gives this to VM-2)
    service-credentials   service-credentials.json
    project               the project id

VM-1's startup script pulls those off the metadata server, installs the Google
API client, and runs `vm1-launch-vm2.py`, which authenticates as the service
account and creates VM-2 with the flask blog on it.

Create the service account and download its key per the instructions in
README.md, and save the key next to this script as `service-credentials.json`.
Do not commit that file.
"""

import argparse
import os
import sys
import time

import googleapiclient.discovery
import googleapiclient.errors
import google.oauth2.service_account as service_account

# ---------------------------------------------------------------- configuration

HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(HERE, 'service-credentials.json')
VM1_STARTUP_FILE = os.path.join(HERE, 'vm1-startup-script.sh')
VM1_LAUNCH_CODE = os.path.join(HERE, 'vm1-launch-vm2.py')
VM2_STARTUP_FILE = os.path.join(HERE, 'vm2-startup-script.sh')

ZONE = 'us-west1-b'
# VM-1 has to pip install the Google API client, which is miserable on an
# f1-micro, so it gets a slightly bigger (still free-tier) machine. VM-2 is
# the f1-micro the assignment asks for.
VM1_MACHINE_TYPE = 'e2-micro'
VM2_MACHINE_TYPE = 'e2-micro'  # f1-micro is exhausted in us-west1-b
IMAGE_PROJECT = 'ubuntu-os-cloud'
IMAGE_FAMILY = 'ubuntu-2204-lts'
NETWORK = 'global/networks/default'
FLASK_PORT = '5000'


# ------------------------------------------------------------------- API helpers

def wait_for_zone_operation(compute, project, zone, operation):
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


def list_instances(compute, project, zone):
    """Stub code from the assignment template - list every instance in a zone."""
    result = compute.instances().list(project=project, zone=zone).execute()
    return result.get('items', [])


def delete_if_present(compute, project, zone, name):
    try:
        operation = compute.instances().delete(
            project=project, zone=zone, instance=name).execute()
    except googleapiclient.errors.HttpError as err:
        if err.resp.status == 404:
            return
        raise
    print(f'Deleting pre-existing instance {name}')
    wait_for_zone_operation(compute, project, zone, operation['name'])


def external_ip(instance):
    for interface in instance.get('networkInterfaces', []):
        for access in interface.get('accessConfigs', []):
            if 'natIP' in access:
                return access['natIP']
    return None


def read(path):
    with open(path) as f:
        return f.read()


# ---------------------------------------------------------------------- VM-1

def create_vm1(compute, project, zone, name, machine_type, vm2_name):
    """Create VM-1, passing it everything it needs through metadata."""
    image = compute.images().getFromFamily(
        project=IMAGE_PROJECT, family=IMAGE_FAMILY).execute()
    print(f'Using image {image["name"]} from family {IMAGE_FAMILY}')

    config = {
        'name': name,
        'machineType': f'zones/{zone}/machineTypes/{machine_type}',
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
        # Metadata is how configuration and code get from here to VM-1. VM-1's
        # startup script reads each of these back off the metadata server at
        # http://metadata/computeMetadata/v1/instance/attributes/<key>.
        'metadata': {
            'items': [
                # Automatically executed by the guest environment on boot.
                {'key': 'startup-script', 'value': read(VM1_STARTUP_FILE)},
                # The program VM-1 runs to create VM-2.
                {'key': 'vm1-launch-vm2-code', 'value': read(VM1_LAUNCH_CODE)},
                # The startup script VM-1 hands on to VM-2.
                {'key': 'vm2-startup-script', 'value': read(VM2_STARTUP_FILE)},
                # The service account key VM-1 authenticates to Google with.
                {'key': 'service-credentials', 'value': read(CREDENTIALS_FILE)},
                {'key': 'project', 'value': project},
                {'key': 'vm2-name', 'value': vm2_name},
            ],
        },
    }
    return compute.instances().insert(
        project=project, zone=zone, body=config).execute()


def wait_for_vm2(compute, project, zone, name, timeout):
    """Poll from the laptop until VM-1 has created VM-2 and it has an IP.

    VM-1 does the real work; this just watches the project so the user gets a
    URL without having to ssh into VM-1.
    """
    print(f'Waiting for VM-1 to create {name} (up to {timeout}s)')
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            instance = compute.instances().get(
                project=project, zone=zone, instance=name).execute()
        except googleapiclient.errors.HttpError as err:
            if err.resp.status != 404:
                raise
            instance = None
        if instance:
            ip = external_ip(instance)
            if ip and instance['status'] == 'RUNNING':
                return instance, ip
        print('.', end='', flush=True)
        time.sleep(10)
    print()
    return None, None


# ------------------------------------------------------------------------ main

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm1-name', default='launcher-vm',
                        help='name of VM-1, the launcher (default: launcher-vm)')
    parser.add_argument('--vm2-name', default='flask-vm2',
                        help='name VM-1 should give VM-2 (default: flask-vm2)')
    parser.add_argument('--zone', default=ZONE, help=f'zone (default: {ZONE})')
    parser.add_argument('--machine-type', default=VM1_MACHINE_TYPE,
                        help=f'machine type for VM-1 (default: {VM1_MACHINE_TYPE})')
    parser.add_argument('--project', default=None,
                        help='project id (default: $GOOGLE_CLOUD_PROJECT, else '
                             'the project_id in the credentials file)')
    parser.add_argument('--credentials', default=CREDENTIALS_FILE,
                        help='service account key file '
                             '(default: service-credentials.json beside this script)')
    # VM-1 has to apt-get and pip install on an e2-micro before it can create
    # VM-2, which took about 11 minutes on 2026-10-08.
    parser.add_argument('--wait', type=int, default=900,
                        help='seconds to wait for VM-2 to appear, 0 to skip '
                             '(default: 900)')
    parser.add_argument('--recreate', action='store_true',
                        help='delete VM-1 first if it already exists')
    parser.add_argument('--list', action='store_true',
                        help='just list the instances in the zone and exit')
    args = parser.parse_args()

    if not os.path.exists(args.credentials):
        print(f'No service account key at {args.credentials}.\n'
              f'Create a service account with the Compute Admin and Service '
              f'Account User roles, download its JSON key, and save it there. '
              f'See README.md.', file=sys.stderr)
        return 1

    # Authenticate as the service account, not as a user. This is the same
    # mechanism VM-1 will use once it has the key file.
    credentials = service_account.Credentials.from_service_account_file(
        filename=args.credentials)
    project = (args.project or os.getenv('GOOGLE_CLOUD_PROJECT')
               or credentials.project_id)
    if not project:
        print('Could not determine the project id; pass --project.',
              file=sys.stderr)
        return 1

    service = googleapiclient.discovery.build(
        'compute', 'v1', credentials=credentials)

    print(f'Authenticated as service account {credentials.service_account_email}')
    print(f'Project: {project}   Zone: {args.zone}')

    if args.list:
        print(f'Your running instances in {args.zone} are:')
        for instance in list_instances(service, project, args.zone):
            print(' ', instance['name'], instance['status'])
        return 0

    # Step 1 - the laptop creates VM-1.
    if args.recreate:
        delete_if_present(service, project, args.zone, args.vm1_name)
    print(f'Creating VM-1 {args.vm1_name} ({args.machine_type})')
    operation = create_vm1(service, project, args.zone, args.vm1_name,
                           args.machine_type, args.vm2_name)
    wait_for_zone_operation(service, project, args.zone, operation['name'])

    vm1 = service.instances().get(
        project=project, zone=args.zone, instance=args.vm1_name).execute()
    vm1_ip = external_ip(vm1)
    print(f'VM-1 {args.vm1_name} is {vm1["status"]} at {vm1_ip}')
    print()
    print('VM-1 is now booting. Its startup script will fetch the launcher')
    print('program and the service account key from its metadata, install the')
    print(f'Google API client, and create VM-2 ({args.vm2_name}).')
    print()
    print('To watch it happen:')
    print(f'    gcloud compute ssh {args.vm1_name} --zone {args.zone} '
          f'--command "sudo tail -f /var/log/vm1-startup.log"')
    print()

    # Step 2 - VM-1 creates VM-2. Watch for it from here.
    if args.wait <= 0:
        return 0

    vm2, vm2_ip = wait_for_vm2(service, project, args.zone, args.vm2_name,
                               args.wait)
    if not vm2:
        print(f'VM-2 ({args.vm2_name}) did not appear within {args.wait}s.')
        print('ssh into VM-1 and check /var/log/vm1-startup.log.')
        return 1

    print()
    print('=' * 60)
    print(f'VM-1 {args.vm1_name} ({vm1_ip}) created VM-2 {args.vm2_name} ({vm2_ip}).')
    print()
    print('Once VM-2 finishes its own startup script, the flask blog is at:')
    print()
    print(f'    http://{vm2_ip}:{FLASK_PORT}')
    print()
    print('=' * 60)
    return 0


if __name__ == '__main__':
    sys.exit(main())
