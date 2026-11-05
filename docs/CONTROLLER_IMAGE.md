# Ready-to-assign controller image

## Confirmed scope

- One reusable Raspberry Pi 4 arm64 image contains the controller code, Python
  environment, USB driver support and all services before the card is flashed.
- The operator connects power and a network, then uses the existing management
  console to select a customer, project and elevator. No credentials are entered
  on the Pi. Project credentials come from the existing protected project record.
- Each new card creates its own key and device identity on first boot. The image
  contains no customer data, shared write key, GitHub credential or device identity.
- Controller readiness and RFID readiness are separate. A configured controller
  with a fresh authenticated heartbeat and healthy management/detector services
  is ready even without a USB reader or tags. The console also shows reader and
  floor-report status without inventing a floor.
- Existing mapped elevators import their sanitized tag snapshot automatically.
  A new elevator can be assigned before any tags are mapped.
- The repaired controller at 192.168.1.22 is not a build host or test target.
  The user may disconnect it. A spare Pi and blank SD card are available for the
  eventual physical boot test.

## Delivery and authorization

The existing `kiosk-device/fleet` backend authenticates administrators. A new
controller section uses that same login and navigation. Device requests prove
possession of a newly generated P-256 key. The public key determines the immutable
device ID. A one-time approved assignment is encrypted to that device using
P-256 ECDH and AES-256-GCM. Polling never exposes project credentials in plaintext.

The protected `projects` records are authoritative for database URL and
`secret_key`. The backend validates the requested elevator against its project.
Registration cannot assign a project. A live existing controller or an occupied
assignment blocks a second controller for the same elevator.

The Pi persists its key and assignment atomically, starts the installed services,
then reports installation progress. The backend independently checks the real
project heartbeat and management services before displaying ready, with the RFID
tracker allowed to wait for its port. RFID detection is reported separately.
An outage retains the last installed assignment and local state. No configuration
is overwritten by a failed download or incomplete assignment.

## Verification

- Unauthorized administrators, forged device signatures, replay and conflicting
  assignments are rejected. Admin/device responses and logs contain no secrets.
- Python and Node interoperability is tested with actual cryptography.
- Test the full register, approve, decrypt, install and heartbeat confirmation
  journey against synthetic project data before deployment.
- Inspect a built image for customer configuration, private keys, cached state and
  credentials, and verify the boot partitions, package payload and enabled units.
- A real SD boot and reboot on the spare Pi are required before calling the image
  field-verified. Linux/image tests do not substitute for that check.

Code stays on development branches. Publishing the shared backend/console and
testing the boot on the spare card follow review of the concrete result. The
production controller checkout and elevator-rpi main remain unchanged.
