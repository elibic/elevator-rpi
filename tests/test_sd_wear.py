"""Tests for the opt-in SD-wear reduction step (settings.REDUCE_SD_WEAR).

הצעד עצמו נוגע במערכת (systemd/apt) ולכן נבדק כאן רק דרך dry_run + העזרים
הטהורים (fstab / דגל / marker) - בלי לגעת במכונה שמריצה את הבדיקות.
"""
import json

from installer import core


def _inst(monkeypatch, settings=None, dry_run=True):
    inst = core.Installer(dry_run=dry_run, progress=lambda msg, level="info": None)
    monkeypatch.setattr(inst, "load_config",
                        lambda: {"settings": settings or {}, "tags": {}})
    return inst


class TestFlagParsing:
    def test_default_off(self, monkeypatch):
        assert _inst(monkeypatch, {})._sd_wear_enabled() is False

    def test_missing_settings_off(self, monkeypatch):
        inst = _inst(monkeypatch)
        monkeypatch.setattr(inst, "load_config", lambda: {})
        assert inst._sd_wear_enabled() is False

    def test_truthy_values(self, monkeypatch):
        for v in (True, "true", "TRUE", "yes", "on", 1, "1"):
            assert _inst(monkeypatch, {"REDUCE_SD_WEAR": v})._sd_wear_enabled() is True, v

    def test_falsy_values(self, monkeypatch):
        for v in (False, "false", "no", "off", 0, "", None, "banana"):
            assert _inst(monkeypatch, {"REDUCE_SD_WEAR": v})._sd_wear_enabled() is False, v

    def test_broken_config_off(self, monkeypatch):
        inst = core.Installer(dry_run=True, progress=lambda m, l="info": None)
        monkeypatch.setattr(inst, "load_config",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert inst._sd_wear_enabled() is False


class TestFstabHelpers:
    FSTAB = ("proc            /proc           proc    defaults          0       0\n"
             "PARTUUID=11-01  /boot/firmware  vfat    defaults          0       2\n"
             "PARTUUID=11-02  /               ext4    defaults          0       1\n")
    FSTAB_NOATIME = FSTAB.replace("ext4    defaults", "ext4    defaults,noatime")

    def test_detect_missing_noatime(self, monkeypatch, tmp_path):
        p = tmp_path / "fstab"
        p.write_text(self.FSTAB)
        assert _inst(monkeypatch)._root_fstab_has_noatime(str(p)) is False

    def test_detect_present_noatime(self, monkeypatch, tmp_path):
        p = tmp_path / "fstab"
        p.write_text(self.FSTAB_NOATIME)
        assert _inst(monkeypatch)._root_fstab_has_noatime(str(p)) is True

    def test_no_root_line(self, monkeypatch, tmp_path):
        p = tmp_path / "fstab"
        p.write_text("# only comments\n")
        assert _inst(monkeypatch)._root_fstab_has_noatime(str(p)) is None

    def test_missing_file(self, monkeypatch, tmp_path):
        assert _inst(monkeypatch)._root_fstab_has_noatime(str(tmp_path / "none")) is None

    def test_add_touches_only_root_line(self, monkeypatch, tmp_path):
        p = tmp_path / "fstab"
        p.write_text(self.FSTAB)
        inst = _inst(monkeypatch)
        assert inst._add_root_noatime(str(p)) is True
        out = p.read_text()
        # שורת ה-root קיבלה noatime; boot ו-proc לא נגעו בהן
        assert "ext4    defaults,noatime" in out
        assert "vfat    defaults          0       2" in out
        assert out.count("noatime") == 1
        assert inst._root_fstab_has_noatime(str(p)) is True

    def test_add_is_noop_when_present(self, monkeypatch, tmp_path):
        p = tmp_path / "fstab"
        p.write_text(self.FSTAB_NOATIME)
        assert _inst(monkeypatch)._add_root_noatime(str(p)) is False
        assert p.read_text() == self.FSTAB_NOATIME


class TestConfigureStep:
    def test_disabled_without_marker_is_noop(self, monkeypatch, tmp_path):
        # לא הופעל מעולם + הדגל כבוי ⇒ לא נוגעים בכלום, גם בלי root
        monkeypatch.setattr(core, "SDWEAR_MARKER", str(tmp_path / "marker.json"))
        res = _inst(monkeypatch, {"REDUCE_SD_WEAR": False}).configure_sd_wear()
        assert res.ok is True
        assert "לא שונה" in res.detail

    def test_enable_dry_run(self, monkeypatch, tmp_path):
        monkeypatch.setattr(core, "SDWEAR_MARKER", str(tmp_path / "marker.json"))
        res = _inst(monkeypatch, {"REDUCE_SD_WEAR": True}).configure_sd_wear()
        assert res.name == "sd_wear"
        # dry_run: לא נכתב marker אמיתי
        assert not (tmp_path / "marker.json").exists()

    def test_disable_after_enable_uses_marker(self, monkeypatch, tmp_path):
        marker = tmp_path / "marker.json"
        marker.write_text(json.dumps({"journald_dropin": True}))
        monkeypatch.setattr(core, "SDWEAR_MARKER", str(marker))
        res = _inst(monkeypatch, {"REDUCE_SD_WEAR": False}).configure_sd_wear()
        assert res.ok is True
        assert "DRY-RUN" in res.detail

    def test_marker_load_broken_file(self, monkeypatch, tmp_path):
        marker = tmp_path / "marker.json"
        marker.write_text("{not json")
        monkeypatch.setattr(core, "SDWEAR_MARKER", str(marker))
        assert _inst(monkeypatch)._load_sdwear_marker() == {}
