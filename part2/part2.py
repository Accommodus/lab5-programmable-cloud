#!/usr/bin/env python3
"""Part 2 - Clone the Part 1 machine via a snapshot and a custom image.

Steps:

  1. find the boot disk of the Part 1 instance
  2. snapshot that disk as `base-snapshot-<instance>` (disks.createSnapshot)
  3. build a custom image `base-image-<instance>` from the snapshot
  4. create three new instances from that image, timing each creation
  5. write the measurements to TIMING.md

The point of the exercise is that all of the slow work from Part 1 - apt-get,
the git clone, the pip install - is already baked into the image, so bringing
up a configured clone only costs the time to provision a VM.

Adapted from the Google Cloud Python sample
https://github.com/GoogleCloudPlatform/python-docs-samples
(compute/api/create_instance.py), Apache 2.0 licensed.
"""

import argparse
import os
import sys
import time

import google.auth
import googleapiclient.discovery
import googleapiclient.errors

# ---------------------------------------------------------------- configuration

ZONE = 'us-west1-b'
MACHINE_TYPE = 'e2-micro'  # see part1.py: f1-micro is exhausted in us-west1-b
NETWORK = 'global/networks/default'
NETWORK_TAG = 'allow-5000'
FLASK_PORT = '5000'
CLONE_COUNT = 3

# The clone's disk already has /srv/flask-tutorial installed from Part 1, but
# the `nohup flask run` process from the original VM obviously does not survive
# into a new instance. This startup script just relaunches the app.
CLONE_STARTUP_SCRIPT = f"""#!/bin/bash
set -x
exec > >(tee -a /var/log/flaskr-startup.log) 2>&1
echo "flaskr clone startup script beginning at $(date)"

cd /srv/flask-tutorial
export FLASK_APP=flaskr
flask init-db
nohup flask run -h 0.0.0.0 -p {FLASK_PORT} > /var/log/flaskr.log 2>&1 &

echo "flaskr clone startup script finished at $(date)"
"""


# ------------------------------------------------------------------- API helpers

def wait_for_zone_operation(compute, project, zone, operation, quiet=False):
    """Block until a zonal operation finishes, then return it."""
    if not quiet:
        print(f'  waiting for zone operation {operation} ', end='', flush=True)
    while True:
        result = compute.zoneOperations().get(
            project=project, zone=zone, operation=operation).execute()
        if result['status'] == 'DONE':
            if not quiet:
                print(' done')
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result
        if not quiet:
            print('.', end='', flush=True)
        time.sleep(1)


def wait_for_global_operation(compute, project, operation, quiet=False):
    """Block until a global operation (snapshot, image, firewall) finishes."""
    if not quiet:
        print(f'  waiting for global operation {operation} ', end='', flush=True)
    while True:
        result = compute.globalOperations().get(
            project=project, operation=operation).execute()
        if result['status'] == 'DONE':
            if not quiet:
                print(' done')
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result
        if not quiet:
            print('.', end='', flush=True)
        time.sleep(1)


def list_instances(compute, project, zone):
    """Stub code from the assignment template - list every instance in a zone."""
    result = compute.instances().list(project=project, zone=zone).execute()
    return result.get('items', [])


def resource_name(url):
    """The API returns fully qualified selfLinks; we often just want the name."""
    return url.rsplit('/', 1)[-1]


# ----------------------------------------------------------- snapshot and image

def boot_disk_of(compute, project, zone, instance_name):
    """Return the name of the instance's boot disk.

    We do not assume the disk is named after the instance - we read the
    attached disk list and pick the one marked as the boot disk.
    """
    instance = compute.instances().get(
        project=project, zone=zone, instance=instance_name).execute()
    for disk in instance.get('disks', []):
        if disk.get('boot'):
            return resource_name(disk['source'])
    raise RuntimeError(f'instance {instance_name} has no boot disk')


def get_snapshot(compute, project, name):
    try:
        return compute.snapshots().get(project=project, snapshot=name).execute()
    except googleapiclient.errors.HttpError as err:
        if err.resp.status == 404:
            return None
        raise


def create_snapshot(compute, project, zone, disk, snapshot_name):
    """Snapshot a persistent disk with disks.createSnapshot."""
    existing = get_snapshot(compute, project, snapshot_name)
    if existing:
        print(f'Snapshot {snapshot_name} already exists - reusing it.')
        return existing

    print(f'Creating snapshot {snapshot_name} from disk {disk}')
    body = {'name': snapshot_name}
    operation = compute.disks().createSnapshot(
        project=project, zone=zone, disk=disk, body=body).execute()
    # createSnapshot is a zonal operation even though the snapshot it produces
    # is a global resource.
    wait_for_zone_operation(compute, project, zone, operation['name'])
    return compute.snapshots().get(project=project, snapshot=snapshot_name).execute()


def get_image(compute, project, name):
    try:
        return compute.images().get(project=project, image=name).execute()
    except googleapiclient.errors.HttpError as err:
        if err.resp.status == 404:
            return None
        raise


def create_image_from_snapshot(compute, project, snapshot, image_name):
    """Create a private custom image whose contents come from a snapshot."""
    existing = get_image(compute, project, image_name)
    if existing:
        print(f'Image {image_name} already exists - reusing it.')
        return existing

    print(f'Creating image {image_name} from snapshot {snapshot["name"]}')
    body = {
        'name': image_name,
        'sourceSnapshot': snapshot['selfLink'],
        'description': 'Ubuntu 22.04 with the flaskr tutorial app preinstalled',
    }
    operation = compute.images().insert(project=project, body=body).execute()
    wait_for_global_operation(compute, project, operation['name'])
    return compute.images().get(project=project, image=image_name).execute()


# ------------------------------------------------------------------- the clones

def create_clone(compute, project, zone, name, source, machine_type):
    """Insert an instance whose boot disk comes from `source`.

    `source` is a dict with either a `sourceImage` or a `sourceSnapshot` key,
    which is the only part of the instance body that differs between cloning
    from an image and cloning straight from a snapshot.
    """
    init_params = dict(source)
    config = {
        'name': name,
        'machineType': f'zones/{zone}/machineTypes/{machine_type}',
        'disks': [{
            'boot': True,
            'autoDelete': True,
            'initializeParams': init_params,
        }],
        'networkInterfaces': [{
            'network': NETWORK,
            'accessConfigs': [
                {'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}
            ],
        }],
        # Tagging at insert time means the allow-5000 firewall rule applies as
        # soon as the clone boots - no separate setTags round trip needed.
        'tags': {'items': [NETWORK_TAG]},
        'metadata': {
            'items': [
                {'key': 'startup-script', 'value': CLONE_STARTUP_SCRIPT},
            ],
        },
    }
    return compute.instances().insert(
        project=project, zone=zone, body=config).execute()


def external_ip(instance):
    for interface in instance.get('networkInterfaces', []):
        for access in interface.get('accessConfigs', []):
            if 'natIP' in access:
                return access['natIP']
    return None


def delete_instance_if_present(compute, project, zone, name):
    try:
        operation = compute.instances().delete(
            project=project, zone=zone, instance=name).execute()
    except googleapiclient.errors.HttpError as err:
        if err.resp.status == 404:
            return
        raise
    print(f'  deleting pre-existing instance {name}')
    wait_for_zone_operation(compute, project, zone, operation['name'], quiet=True)


def time_clone_creation(compute, project, zone, name, source, machine_type):
    """Create one clone and return timing measurements in seconds.

    Two numbers are interesting and they are not the same thing:

      `insert`  - how long the instances.insert API call itself took to return
                  (it returns immediately with a pending operation)
      `operation` - how long until the create operation reached DONE
      `running` - how long until the instance reported status RUNNING
    """
    delete_instance_if_present(compute, project, zone, name)

    start = time.perf_counter()
    operation = create_clone(compute, project, zone, name, source, machine_type)
    insert_returned = time.perf_counter()

    wait_for_zone_operation(compute, project, zone, operation['name'], quiet=True)
    operation_done = time.perf_counter()

    while True:
        instance = compute.instances().get(
            project=project, zone=zone, instance=name).execute()
        if instance['status'] == 'RUNNING':
            break
        time.sleep(1)
    running = time.perf_counter()

    return {
        'name': name,
        'insert': insert_returned - start,
        'operation': operation_done - start,
        'running': running - start,
        'ip': external_ip(instance),
    }


# ------------------------------------------------------------------- reporting

def write_timing_report(path, results, base_instance, source_kind, source_name,
                        machine_type, zone, prep):
    """Write TIMING.md with the measurements from this run."""
    lines = []
    lines.append('# Part 2 Timing Results')
    lines.append('')
    lines.append('Times measured by `part2.py` using `time.perf_counter()` '
                 'around the Compute Engine API calls.')
    lines.append('')
    lines.append('## Setup')
    lines.append('')
    lines.append(f'- Base instance: `{base_instance}`')
    lines.append(f'- Clones created from {source_kind}: `{source_name}`')
    lines.append(f'- Machine type: `{machine_type}`')
    lines.append(f'- Zone: `{zone}`')
    lines.append(f'- Clones created serially, one after another')
    lines.append('')
    if prep:
        lines.append('## One-time preparation')
        lines.append('')
        lines.append('| Step | Time (s) |')
        lines.append('| --- | --- |')
        for label, seconds in prep:
            lines.append(f'| {label} | {seconds:.2f} |')
        lines.append('')
    lines.append('## Instance creation times')
    lines.append('')
    lines.append('| Instance | insert() returned (s) | create operation DONE (s) '
                 '| instance RUNNING (s) |')
    lines.append('| --- | --- | --- | --- |')
    for r in results:
        lines.append(f'| `{r["name"]}` | {r["insert"]:.2f} | {r["operation"]:.2f} '
                     f'| {r["running"]:.2f} |')
    if results:
        n = len(results)
        lines.append(f'| **mean** | '
                     f'**{sum(r["insert"] for r in results) / n:.2f}** | '
                     f'**{sum(r["operation"] for r in results) / n:.2f}** | '
                     f'**{sum(r["running"] for r in results) / n:.2f}** |')
    lines.append('')
    lines.append('## Notes')
    lines.append('')
    lines.append('- `insert() returned` is just the round trip of the API call; '
                 'Compute Engine replies immediately with a pending operation, '
                 'so this measures the API, not the provisioning.')
    lines.append('- `create operation DONE` is when Compute Engine finished '
                 'provisioning the VM and its boot disk.')
    lines.append('- `instance RUNNING` is when the instance reported the RUNNING '
                 'status. The guest OS still has to boot and run the startup '
                 'script after this point before flask answers on port 5000.')
    lines.append('- The clones do **not** repeat the Part 1 install work '
                 '(`apt-get install`, `git clone`, `pip3 install`), because that '
                 'is already captured in the image. That is the whole benefit of '
                 'baking a configured environment into an image: the expensive '
                 'configuration is paid once, and each additional instance only '
                 'costs provisioning time.')
    lines.append('')

    with open(path, 'w') as f:
        f.write('\n'.join(lines))
    print(f'Wrote timing report to {path}')


# ------------------------------------------------------------------------ main

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', default='flask-vm',
                        help='the Part 1 instance to clone (default: flask-vm)')
    parser.add_argument('--zone', default=ZONE, help=f'zone (default: {ZONE})')
    parser.add_argument('--machine-type', default=MACHINE_TYPE,
                        help=f'machine type for the clones (default: {MACHINE_TYPE})')
    parser.add_argument('--project', default=None,
                        help='project id (default: from application default credentials)')
    parser.add_argument('--count', type=int, default=CLONE_COUNT,
                        help=f'how many clones to create (default: {CLONE_COUNT})')
    parser.add_argument('--source', choices=['image', 'snapshot'], default='image',
                        help='create the clones from the custom image (default) '
                             'or directly from the snapshot')
    parser.add_argument('--timing-file', default=None,
                        help='where to write the timing report '
                             '(default: TIMING.md next to this script)')
    parser.add_argument('--list', action='store_true',
                        help='just list the instances in the zone and exit')
    args = parser.parse_args()

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

    snapshot_name = f'base-snapshot-{args.instance}'
    image_name = f'base-image-{args.instance}'
    prep = []

    # Step 1 - find the boot disk of the Part 1 instance.
    disk = boot_disk_of(compute, project, args.zone, args.instance)
    print(f'Instance {args.instance} boots from disk {disk}')

    # Step 2 - snapshot it. A reused snapshot or image is not timed: the
    # report should only show preparation work that this run actually did.
    reused_snapshot = get_snapshot(compute, project, snapshot_name) is not None
    t0 = time.perf_counter()
    snapshot = create_snapshot(compute, project, args.zone, disk, snapshot_name)
    if not reused_snapshot:
        prep.append(('Create snapshot from the Part 1 boot disk',
                     time.perf_counter() - t0))

    # Step 3 - build a custom image from the snapshot.
    if args.source == 'image':
        reused_image = get_image(compute, project, image_name) is not None
        t0 = time.perf_counter()
        image = create_image_from_snapshot(compute, project, snapshot, image_name)
        if not reused_image:
            prep.append(('Create custom image from the snapshot',
                         time.perf_counter() - t0))
        source = {'sourceImage': image['selfLink']}
        source_kind, source_name = 'custom image', image_name
    else:
        source = {'sourceSnapshot': snapshot['selfLink']}
        source_kind, source_name = 'snapshot', snapshot_name

    # Step 4 - create the clones, timing each one.
    print()
    print(f'Creating {args.count} clones from the {source_kind} {source_name}')
    results = []
    for i in range(1, args.count + 1):
        name = f'{args.instance}-clone-{i}'
        print(f'[{i}/{args.count}] creating {name} ...', end='', flush=True)
        r = time_clone_creation(
            compute, project, args.zone, name, source, args.machine_type)
        results.append(r)
        print(f' RUNNING after {r["running"]:.2f}s at {r["ip"]}')

    # Step 5 - write the report.
    print()
    timing_file = args.timing_file or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'TIMING.md')
    write_timing_report(timing_file, results, args.instance, source_kind,
                        source_name, args.machine_type, args.zone, prep)

    print()
    print('=' * 60)
    print('Clones are up. Once each one has booted and run its startup script,')
    print('the flask blog is available at:')
    for r in results:
        print(f'    http://{r["ip"]}:{FLASK_PORT}   ({r["name"]})')
    print('=' * 60)
    return 0


if __name__ == '__main__':
    sys.exit(main())
