"""Fail the image build if a card contains a preassigned identity or credentials."""
import pathlib
import subprocess
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
assert (app/'venv/bin/python').exists()
assert 'DHCP=yes' in (root/'etc/systemd/network/01-eth0.network').read_text()
assert (root/'etc/systemd/system/multi-user.target.wants/controller-enrollment.service').is_symlink()
assert (root/'etc/systemd/system/multi-user.target.wants/ssh.service').is_symlink()
assert (root/'etc/systemd/system/multi-user.target.wants/ssh-hostkeys-generate.service').is_symlink()
authorized = (root/'home/eco/.ssh/authorized_keys')
assert authorized.read_text().strip().startswith('ssh-ed25519 ')
assert authorized.stat().st_mode & 0o777 == 0o600
ssh_config = (root/'etc/ssh/sshd_config.d/01pubkey-only.conf').read_text()
assert 'AuthenticationMethods publickey' in ssh_config and 'PasswordAuthentication no' in ssh_config
assert not list((root/'home/eco/.ssh').glob('id_*'))
account = next(line.split(':') for line in (root/'etc/shadow').read_text().splitlines() if line.startswith('eco:'))
assert account[1].startswith(('!', '*'))
for unit in (root/'etc/systemd/system').glob('*.service'):
    if unit.is_symlink(): continue
    directives=[line.strip() for line in unit.read_text().splitlines() if line.strip() and not line.lstrip().startswith(('#',';'))]
    if any(line.startswith('WantedBy=') and 'multi-user.target' in line.split('=',1)[1].split() for line in directives):
        assert not any(line.startswith('After=') and 'multi-user.target' in line.split('=',1)[1].split() for line in directives)
tracker=(root/'etc/systemd/system/rfid-tracker.service').read_text()
assert 'ExecStart=/opt/elevator-rpi/venv/bin/python -m provisioning.wait_for_reader' in tracker
assert 'ExecStartPre=/opt/elevator-rpi/venv/bin/python -m provisioning.wait_for_reader' not in tracker
subprocess.run(['chroot', str(root), '/opt/elevator-rpi/venv/bin/python', '-c',
    'import requests, serial, flask, sseclient, cryptography; '
    'import provisioning.agent, shabbat_detector.detector, shabbat_detector.fleet_agent, installer.web'],
    check=True)
print('Image audit passed: application installed; no customer configuration or cloned identity.')
