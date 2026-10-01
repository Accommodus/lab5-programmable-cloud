# Lab 5 - Programmable Cloud: Solution Notes

How to run each part, what each program does, and the design decisions behind it.

## Setup

The programs need the Compute Engine API and the Google API python client:

```bash
gcloud services enable compute.googleapis.com
gcloud auth application-default login      # parts 1 and 2 use these credentials

python3 -m venv .venv
.venv/bin/pip install google-api-python-client google-auth \
                      google-auth-httplib2 google-auth-oauthlib
```

Everything runs in `us-west1-b` on `e2-micro` instances by default. Each program
takes `--zone`, `--machine-type` and `--project` if you want to override that
(an `e2-medium` is much less painful while developing).

### Why `e2-micro` and not `f1-micro`

The assignment suggests `f1-micro`. It is a deprecated legacy machine type, and
in `us-west1-b` it currently fails outright:

```
RuntimeError: {'errors': [{'code': 'ZONE_RESOURCE_POOL_EXHAUSTED',
  'message': "The zone 'projects/.../zones/us-west1-b' does not have enough
  resources available to fulfill the request. ..."
```

`part1/README.md` allows "`f1-micro` or `e2` family", and `e2-micro` is
likewise free-tier eligible in `us-west1`, so that is the default. Pass
`--machine-type f1-micro` to try the legacy type anyway.

---

## Part 1 - Create a VM and install the flask app

```bash
.venv/bin/python part1/part1.py                 # creates an instance named flask-vm
.venv/bin/python part1/part1.py --recreate      # replace an existing flask-vm
.venv/bin/python part1/part1.py --list          # just list instances in the zone
```

`part1/part1.py` does, entirely through the Compute Engine API:

1. **Resolves the image family.** `images().getFromFamily('ubuntu-os-cloud',
   'ubuntu-2204-lts')` so no specific Ubuntu version is hard-coded - Google
   picks the current image in the family.
2. **Creates the firewall rule, but only if it is missing.** `allow-5000` is a
   VPC-level resource, so creating it per-VM would be wrong. The program calls
   `firewalls().list(filter='name = "allow-5000"')` first; a filtered list
   returns an empty result for a missing rule, whereas `firewalls().get()`
   would raise a 404 that we would then have to catch. If the rule is absent it
   is created allowing `tcp:5000` from `0.0.0.0/0`, with `targetTags:
   ['allow-5000']`.
3. **Inserts the instance** on the `default` network with an
   `ONE_TO_ONE_NAT` access config, which is what gives it an ephemeral external
   IP, and with the install script as the `startup-script` metadata key.
4. **Tags the instance** with `instances().setTags()`. `setTags` needs the
   current tags *fingerprint*, which is an optimistic-concurrency token, so the
   program reads the instance first and passes the fingerprint back. (The tag
   could also be set inline at insert time - Part 2 does exactly that - but the
   assignment asks specifically for `setTags`.)
5. **Reads the external IP back out of the API** -
   `networkInterfaces[0].accessConfigs[0].natIP` - and prints the URL.

### The `allow-5000` tag

The firewall rule and the network tag are two halves of one mechanism:

```
firewall rule allow-5000  -- targetTags -->  tag "allow-5000"  -->  flask-vm
```

Scoping the rule to a tag matters: without `targetTags`, the rule would open
port 5000 on *every* VM in the default network, including ones created later
for unrelated reasons.

### The startup script

The startup script is run as root by the guest environment on first boot. Two
practical notes:

- It runs with `/` as the working directory, so the script `mkdir -p /srv` and
  works there.
- It `tee`s all its output to `/var/log/flaskr-startup.log`, which is far
  easier to read than grepping `/var/log/syslog`. To debug a failed install:
  `gcloud compute ssh flask-vm --zone us-west1-b --command "sudo tail -100 /var/log/flaskr-startup.log"`.

`nohup flask run -h 0.0.0.0 &` is what keeps the app alive after the startup
script exits; `-h 0.0.0.0` makes flask listen on the external interface instead
of only on loopback, which is required for the external IP to be of any use.

The app takes a minute or two after the instance reports `RUNNING`, because
`apt-get`, `git clone` and `pip install` all still have to happen inside the
guest. Part 2 is about removing exactly that delay.

---

## Part 2 - Clone the machine with a snapshot and an image

```bash
.venv/bin/python part2/part2.py                        # snapshot -> image -> 3 clones
.venv/bin/python part2/part2.py --source snapshot      # clone straight from the snapshot
.venv/bin/python part2/part2.py --count 5
```

`part2/part2.py`:

1. **Finds the boot disk** of `flask-vm`. It does not assume the disk shares the
   instance's name; it reads the attached disk list and picks the entry with
   `boot: true`.
2. **Snapshots that disk** with `disks().createSnapshot()`, naming it
   `base-snapshot-flask-vm`. Note the asymmetry: `createSnapshot` is a *zonal*
   operation (the disk is zonal) even though the snapshot it produces is a
   *global* resource - so it is polled with `zoneOperations()` but fetched with
   `snapshots().get()`.
3. **Creates a custom image** `base-image-flask-vm` from the snapshot with
   `images().insert({'sourceSnapshot': ...})`, polled with `globalOperations()`.
4. **Creates three instances** from that image, timing each one, and tagging
   them `allow-5000` at insert time so they are reachable as soon as they boot.
5. **Writes `part2/TIMING.md`** with the measurements.

Both readings of the assignment are supported: `--source image` (the default)
goes snapshot → image → instances, and `--source snapshot` creates the
instances directly from the snapshot, skipping the image.

### What is being timed

Three separate numbers, because they mean different things:

| measurement | what it captures |
| --- | --- |
| `insert() returned` | the API round trip only. Compute Engine replies immediately with a *pending* operation, so this is ~a tenth of a second and says nothing about provisioning. |
| `create operation DONE` | Compute Engine finished provisioning the VM and its boot disk. |
| `instance RUNNING` | the instance reports status `RUNNING`. |

Even `RUNNING` is not "the blog is up" - the guest still has to boot and run
its startup script. The reason the clones are fast is that their startup script
only has to run `flask init-db` and `flask run`; the `apt-get`/`git`/`pip` work
from Part 1 is already baked into the disk image.

The clones are created serially so that each measurement is independent.

See [part2/TIMING.md](part2/TIMING.md) for the actual numbers.

---

## Part 3 - A VM that creates a VM, using a service account

```
laptop (part3.py)  --creates-->  VM-1 (launcher-vm)  --creates-->  VM-2 (flask-vm2)
```

### Files

| file | runs where | role |
| --- | --- | --- |
| `part3/part3.py` | laptop | authenticates as the service account, creates VM-1 |
| `part3/vm1-startup-script.sh` | VM-1 | pulls everything out of metadata, runs the launcher |
| `part3/vm1-launch-vm2.py` | VM-1 | authenticates as the service account, creates VM-2 |
| `part3/vm2-startup-script.sh` | VM-2 | installs and runs the flask blog |
| `part3/service-credentials.json` | laptop, VM-1 | the service account key - **not committed** |

### Creating the service account

Follow the screenshots in `part3/README.md`, or equivalently:

```bash
PROJECT=$(gcloud config get-value project)
SA=lab5-launcher

gcloud iam service-accounts create $SA --display-name "Lab 5 VM launcher"

gcloud projects add-iam-policy-binding $PROJECT \
  --member "serviceAccount:$SA@$PROJECT.iam.gserviceaccount.com" \
  --role roles/compute.admin
gcloud projects add-iam-policy-binding $PROJECT \
  --member "serviceAccount:$SA@$PROJECT.iam.gserviceaccount.com" \
  --role roles/iam.serviceAccountUser

gcloud iam service-accounts keys create part3/service-credentials.json \
  --iam-account "$SA@$PROJECT.iam.gserviceaccount.com"
```

`roles/compute.admin` lets it create VMs and firewall rules.
`roles/iam.serviceAccountUser` is the "act as" permission, needed to attach a
service account to an instance you are creating.

### Running it

```bash
.venv/bin/python part3/part3.py               # creates launcher-vm, waits for flask-vm2
.venv/bin/python part3/part3.py --recreate
.venv/bin/python part3/part3.py --wait 0      # don't wait around for VM-2
```

`part3.py` builds its Compute Engine client from
`service_account.Credentials.from_service_account_file()` rather than
`google.auth.default()` - so it is authenticating as the service account, not
as you, even when run from your laptop.

### Getting code and secrets to VM-1

VM-1 needs three files and a project id, so all four go into the instance's
metadata at insert time:

| metadata key | contents |
| --- | --- |
| `startup-script` | `vm1-startup-script.sh` - executed automatically on boot |
| `vm1-launch-vm2-code` | `vm1-launch-vm2.py` |
| `vm2-startup-script` | `vm2-startup-script.sh`, to be passed on to VM-2 |
| `service-credentials` | the service account key JSON |
| `project` | the project id |

VM-1's startup script reads each one back off the metadata server, a link-local
"fake" web server at `169.254.169.254` reachable as the hostname `metadata`:

```bash
curl http://metadata/computeMetadata/v1/instance/attributes/vm2-script \
     -H "Metadata-Flavor: Google"
```

The `Metadata-Flavor: Google` header is required - it is a
[SSRF](https://en.wikipedia.org/wiki/Server-side_request_forgery) guard, since
a plain `curl`-able URL could otherwise be triggered by a tricked application
on the VM. This is also exactly how the `startup-script` itself gets to the
guest: the guest environment fetches that same metadata key and executes it.

> The example in `part3/README.md` has two deliberate bugs:
> `export GOOGLE_CLOUD_PROJECT= $(curl ...)` has a space after the `=` (so the
> variable is set to empty and the project id is run as a command), and it
> downloads `vm1-launch-vm2-code.py` but then runs `vm1-launch-code.py`.
> Both are fixed in `vm1-startup-script.sh`.

### Security

VM-2 gets **no** credentials at all - `vm1-launch-vm2.py` deliberately omits
the `serviceAccounts` property when creating it. VM-2 only serves a blog, so
nothing on it can create further cloud resources, and the key never travels
past VM-1. On VM-1 the key file is `chmod 600`.

The key is in `.gitignore`, because a leaked service account key is a
standing ability to create resources in the project.

### Why you might not want the key file at all

Setting the `serviceAccounts` property on VM-1 would attach an identity to the
instance and let the code there use `google.auth.default()`, with Google
rotating short-lived tokens through the metadata server - no key file to
leak, steal, or forget to rotate. That is the better practice *when the code
runs inside Google Cloud*.

The explicit key file is what you need when the program runs somewhere else -
on a laptop, in CI, or in another cloud - which is precisely the case `part3.py`
itself demonstrates when you run it from your own machine.

---

## Cleaning up

VMs are not the only thing that costs money; snapshots, images and disks
outlive the instances they came from.

```bash
Z=us-west1-b
gcloud compute instances delete flask-vm flask-vm-clone-1 flask-vm-clone-2 \
    flask-vm-clone-3 launcher-vm flask-vm2 --zone $Z
gcloud compute images delete base-image-flask-vm
gcloud compute snapshots delete base-snapshot-flask-vm
gcloud compute firewall-rules delete allow-5000
```
