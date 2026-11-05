"""Fail the image build if a card contains a preassigned identity or credentials."""
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
app = root / 'opt/elevator-rpi'
forbidden = [app/'rfid_config.json', app/'.git', root/'var/lib/elevator-enrollment/identity.pem',
             root/'var/lib/elevator-enrollment/state.json', root/'root/.git-credentials']
for path in forbidden:
    if path.exists(): raise SystemExit('Image contains forbidden device state: '+str(path.relative_to(root)))
for pattern in ['var/lib/shabbat_detector/state_*.json','etc/ssh/ssh_host_*','opt/elevator-rpi/rfid_config.json.*']:
    if list(root.glob(pattern)): raise SystemExit('Image contains cloned state: '+pattern)
assert (root/'etc/machine-id').read_bytes() == b''
assert (app/'provisioning/agent.py').exists()
assert (root/'etc/systemd/system/multi-user.target.wants/controller-enrollment.service').is_symlink()
print('Image audit passed: application installed; no customer configuration or cloned identity.')
