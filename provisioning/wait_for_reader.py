"""An absent reader is a normal wait, not a tracker crash loop."""
import argparse
import json
import pathlib
import time

def wait(config):
    print('Waiting for the configured RFID port; controller management remains available.', flush=True)
    while True:
        try:
            settings = json.loads(pathlib.Path(config).read_text())['settings']
            if pathlib.Path(settings['SERIAL_PORT']).exists():
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(2)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    wait(parser.parse_args().config)
