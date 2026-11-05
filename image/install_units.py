"""Render the existing units into an offline image; never change the build host."""
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
if root == pathlib.Path('/') or not (root / 'etc/os-release').exists():
    raise SystemExit('Expected an offline image root')
app = root / 'opt/elevator-rpi'
units = root / 'etc/systemd/system'
units.mkdir(parents=True, exist_ok=True)
wants = units / 'multi-user.target.wants'
wants.mkdir(exist_ok=True)
mapping = {'USER':'eco','PROJECT_DIR':'/opt/elevator-rpi','PYTHON':'/opt/elevator-rpi/venv/bin/python',
           'VENV':'/opt/elevator-rpi/venv','SERIAL_PORT':'/dev/ttyUSB0'}
for template in (app / 'systemd').glob('*.service.in'):
    text = template.read_text()
    for key, value in mapping.items():
        text = text.replace('{{'+key+'}}', value)
    name = template.name.removesuffix('.in')
    if name in ('rfid-tracker.service','shabbat-detector.service','fleet-agent.service'):
        text = text.replace('[Unit]\n', '[Unit]\nConditionPathExists=/opt/elevator-rpi/rfid_config.json\n', 1)
    if name == 'rfid-tracker.service':
        text = '\n'.join(line for line in text.splitlines() if not line.startswith('ExecStartPre=/bin/sh'))+'\n'
        # Wait inside the Type=simple main process. ExecStartPre would hold up
        # multi-user.target forever on the next boot without a reader.
        text = text.replace('ExecStart=/opt/elevator-rpi/venv/bin/python -u /opt/elevator-rpi/elevator_tracker_rfid.py',
            'ExecStart=/opt/elevator-rpi/venv/bin/python -m provisioning.wait_for_reader --config /opt/elevator-rpi/rfid_config.json --tracker /opt/elevator-rpi/elevator_tracker_rfid.py')
    (units / name).write_text(text)
    link = wants / name
    if not link.exists():
        link.symlink_to('../'+name)
enrollment = '''[Unit]
Description=Enroll this elevator controller in ECONTROL management
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=root
WorkingDirectory=/opt/elevator-rpi
ExecStart=/opt/elevator-rpi/venv/bin/python -m provisioning.agent
Restart=always
RestartSec=10
UMask=0077
StandardOutput=journal+console
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
'''
(units / 'controller-enrollment.service').write_text(enrollment)
(wants / 'controller-enrollment.service').symlink_to('../controller-enrollment.service')
driver = root / 'usr/local/bin/fix_cp210x.sh'
driver.parent.mkdir(parents=True, exist_ok=True)
driver.write_bytes((app / 'installer/fix_cp210x.sh').read_bytes())
driver.chmod(0o755)
# Look up the target account in the image, never on the build host.
account = next(line.split(':') for line in (root / 'etc/passwd').read_text().splitlines() if line.startswith('eco:'))
import os
for directory in (app / 'logs', root / 'var/lib/shabbat_detector'):
    directory.mkdir(parents=True, exist_ok=True)
    os.chown(directory, int(account[2]), int(account[3]))
state = root / 'var/lib/elevator-enrollment'
state.mkdir(mode=0o700, parents=True, exist_ok=True)
