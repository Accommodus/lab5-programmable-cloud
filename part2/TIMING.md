# Part 2 Timing Results

Times measured by `part2.py` using `time.perf_counter()` around the Compute Engine API calls.

## Setup

- Base instance: `flask-vm`
- Clones created from custom image: `base-image-flask-vm`
- Machine type: `e2-micro`
- Zone: `us-west1-b`
- Clones created serially, one after another

## One-time preparation

| Step | Time (s) |
| --- | --- |
| Create snapshot from the Part 1 boot disk | 58.00 |
| Create custom image from the snapshot | 94.50 |

## Instance creation times

| Instance | insert() returned (s) | create operation DONE (s) | instance RUNNING (s) |
| --- | --- | --- | --- |
| `flask-vm-clone-1` | 0.92 | 12.15 | 12.36 |
| `flask-vm-clone-2` | 1.19 | 9.87 | 10.09 |
| `flask-vm-clone-3` | 0.96 | 8.44 | 8.63 |
| **mean** | **1.02** | **10.15** | **10.36** |

## Notes

- `insert() returned` is just the round trip of the API call; Compute Engine replies immediately with a pending operation, so this measures the API, not the provisioning.
- `create operation DONE` is when Compute Engine finished provisioning the VM and its boot disk.
- `instance RUNNING` is when the instance reported the RUNNING status. The guest OS still has to boot and run the startup script after this point before flask answers on port 5000.
- The clones do **not** repeat the Part 1 install work (`apt-get install`, `git clone`, `pip3 install`), because that is already captured in the image. That is the whole benefit of baking a configured environment into an image: the expensive configuration is paid once, and each additional instance only costs provisioning time.
