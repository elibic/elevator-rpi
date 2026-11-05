"""Managed assignment fields cannot be overwritten by a stale local form."""
import copy
from types import SimpleNamespace
from installer.core import Installer

def setup_installer(monkeypatch):
    config={'settings':{'CONTROLLER_DEVICE_ID':'lift-'+'a'*24,
                        'FIREBASE_URL':'https://test.firebaseio.com/elevators.json',
                        'ELEVATOR_ID':'C','SECRET_KEY':'synthetic-new'},'tags':{}}
    inst=Installer(SimpleNamespace(is_root=False),dry_run=True)
    monkeypatch.setattr(inst,'load_config',lambda:copy.deepcopy(config))
    written=[]
    monkeypatch.setattr(inst,'config_path',lambda:'/unused/rfid_config.json')
    monkeypatch.setattr(inst,'_write_file',lambda *args,**kwargs:written.append(args))
    return inst,written

def test_stale_form_cannot_restore_old_authentication(monkeypatch):
    inst,written=setup_installer(monkeypatch)
    assert not inst.write_config({'SECRET_KEY':'synthetic-old'},{}).ok
    assert not written

def test_local_tag_mapping_changes_remain_available(monkeypatch):
    inst,written=setup_installer(monkeypatch)
    assert inst.write_config({'SECRET_KEY':'synthetic-new'}, {'AABBCCDDEEFF':'3'}).ok
    assert len(written)==1
