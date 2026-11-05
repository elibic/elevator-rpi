"""Enroll an image, install its approved config, and report health separately from RFID."""
from __future__ import annotations
import json
import logging
import os
import pathlib
import random
import subprocess
import time
from .client import Client, Identity, ProvisioningError, atomic_write

log = logging.getLogger('controller-enrollment')
SERVICES = ('rfid-tracker','shabbat-detector','fleet-agent','elevator-config-web')

def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default

def install_assignment(root, state_dir, identity, decoded, restart=None, owner=None):
    path = root / 'rfid_config.json'
    current = read_json(path)
    if current and current.get('settings', {}).get('CONTROLLER_DEVICE_ID') != identity.device_id:
        raise ProvisioningError('existing_controller_configuration')
    state = read_json(state_dir / 'state.json', {})
    revision = decoded['assignment']['revision']
    if state.get('revision', 0) > revision:
        raise ProvisioningError('assignment_rollback_rejected')
    if state.get('revision') == revision and current is not None:
        # Reconnecting must preserve tag mappings edited after enrollment.
        return state
    config = decoded['config']
    if current != config:
        atomic_write(path, (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode(),
                     owner.pw_uid if owner else None, owner.pw_gid if owner else None)
    if state.get('revision') != revision or current is None:
        (restart or restart_services)()
    state = {'deviceId':identity.device_id, 'revision':revision,
             'assignment':decoded['assignment'], 'installedAt':int(time.time())}
    atomic_write(state_dir / 'state.json', json.dumps(state).encode())
    return state

def restart_services():
    # --no-block lets rfid-tracker wait for absent hardware without blocking setup.
    result = subprocess.run(['systemctl','start','--no-block',*SERVICES], capture_output=True, timeout=30)
    if result.returncode:
        raise ProvisioningError('service_start_failed')

def hardware_info(root):
    model = pathlib.Path('/proc/device-tree/model')
    serial = pathlib.Path('/proc/device-tree/serial-number')
    return {'model':model.read_text().strip('\0\n') if model.exists() else 'Raspberry Pi',
            'serial':serial.read_text().strip('\0\n') if serial.exists() else '',
            'version':(root / 'VERSION').read_text().strip()}

def run(root=pathlib.Path('/opt/elevator-rpi'), state_dir=pathlib.Path('/var/lib/elevator-enrollment')):
    import pwd
    identity = Identity.load_or_create(state_dir)
    client = Client(identity)
    owner = pwd.getpwnam('eco')
    log.info('Controller %s is waiting for assignment in management', identity.device_id)
    backoff, registered = 5, False
    while True:
        try:
            if not registered:
                client.request('time')
                result = client.request('register', hardware_info(root))
                registered = True
            else:
                state = read_json(state_dir / 'state.json', {})
                result = client.request('poll', {'revision':state.get('revision', 0)})
            if result.get('envelope'):
                decoded = identity.decrypt(result['envelope'])
                state = install_assignment(root, state_dir, identity, decoded, owner=owner)
                log.info('Approved assignment installed for controller %s', identity.device_id)
            state = read_json(state_dir / 'state.json', {})
            if state.get('revision'):
                cfg = read_json(root / 'rfid_config.json')
                port = cfg['settings']['SERIAL_PORT']
                result = client.request('report', {'revision':state['revision'], 'readerConnected':pathlib.Path(port).exists()})
                atomic_write(state_dir / 'health.json', json.dumps({'deviceId':identity.device_id, **result}).encode())
                log.info('Controller state: %s; reader: %s', result['state'], result['health']['readerState'])
            backoff = 5
            time.sleep((60 if state.get('revision') else 15) + random.uniform(0, 3))
        except Exception as error:
            # Exception messages can contain credentials; only fixed codes are logged.
            code = str(error) if isinstance(error, ProvisioningError) else type(error).__name__
            log.warning('Enrollment retry: %s', code)
            registered = False
            time.sleep(backoff + random.uniform(0, 3))
            backoff = min(backoff * 2, 300)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    run()
