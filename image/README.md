# Controller card 1.2.0

This is a Raspberry Pi 4 arm64 image built with official rpi-image-gen v2.9.0
(commit `d2d5705bcb37284c4b08c0ce43c039b1f1004e25`) and Debian Trixie.
It contains the Python environment, controller application and systemd services.
The first release uses Ethernet DHCP and runs without a desktop or browser.

## Flash and assign

1. Download `econtrol-controller-rpi4-arm64` from the successful development
   branch image workflow. Verify the image against its `SHA256SUMS` file.
2. In Raspberry Pi Imager select Use custom and the `.img.xz` file. Select the
   spare SD card, at least 8 GB. Writing erases that selected card. Skip OS
   customization: the image already defines its account and network setup.
3. Insert the card in a spare Pi 4, connect Ethernet and power. No RFID hardware
   is required. A new key and device ID are created on this card at first boot.
4. Once the companion backend is published, sign in at
   `https://elevator.econtrol.co.il/admin/controllers/`. Select the customer,
   project and elevator for the newly discovered controller and confirm.
5. Wait for controller ready. Cloud heartbeat and management services must pass
   the backend's independent check. Reader waiting and no floor report are
   expected before connecting a reader and mapped tags.

An elevator with another recently reporting controller cannot be assigned.
Use a spare elevator entry for the first test. Do not replace the active Ramada
controller or reuse its private configuration on the generic card.

## Maintenance without passwords

The build takes `CONTROLLER_ADMIN_PUBLIC_KEY`, an Ed25519 public key, from a
GitHub repository variable. The matching private key stays on the administrator's
computer. It is never included in the card or uploaded to GitHub. SSH accepts
public key authentication only; eco has no password and root SSH is disabled.
SSH host keys are generated separately on every new card. The maintenance
account can use sudo. Keep access to the private key for diagnostics.

Find the card's address in the router's DHCP leases, then connect from the
administrator's computer (substitute the actual private key path and address):

```powershell
ssh -i C:\path\to\admin-key eco@PI_ADDRESS
ssh -i C:\path\to\admin-key -L 8080:127.0.0.1:8080 eco@PI_ADDRESS
```

With the tunnel open, browse `http://127.0.0.1:8080/config` to edit RFID mappings.
Project identity and credentials are managed by enrollment and cannot be
replaced by an old configuration form. Never print the configuration file.

Useful diagnostics after connecting:

```sh
systemctl is-active controller-enrollment rfid-tracker shabbat-detector fleet-agent elevator-config-web
sudo journalctl -u controller-enrollment -n 30 --no-pager
ls -l /dev/ttyUSB* /dev/ttyACM*
```

Before assignment only enrollment and local web need to be active. The three
project services are skipped until approved configuration exists. After
assignment the tracker stays active while waiting for its serial port; missing
hardware cannot hold up system boot. Shut down with `sudo poweroff` before
removing power or the card.

## Build and release boundary

On an arm64 Linux builder with rpi-image-gen dependencies installed:

```sh
export CONTROLLER_ADMIN_PUBLIC_KEY="$(cat /path/to/admin-key.pub)"
bash image/build.sh /path/to/rpi-image-gen
```

The image audit rejects cloned device/customer state, machine IDs, SSH host keys
and private keys, and checks enabled services and key-only maintenance access.
The application is archived from the committed Git tree, without `.git`, real
configuration, logs or credentials.

The shared provisioning backend and console are a separate change in
`elibic/kiosk-device`, branch `feat/elevator-provisioning`. Until they are
published the image retries registration and cannot be assigned. Code and image
checks do not establish physical boot success: the spare card must still pass
boot, assignment without RFID, reboot and identity persistence checks.

Wi-Fi setup, remote reassignment, unattended firmware updates and GitHub log
backup credentials are not part of this first image release. Existing active
controllers keep their current setup. No token is requested during assignment.
