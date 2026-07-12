"""Tests for SHABBAT_SOURCE resolution and offset parsing."""
from shabbat_detector.schedule_windows import resolve_source, schedule_offsets


class TestResolveSource:
    def test_absent_everywhere_is_auto(self):
        assert resolve_source({}, {}) == "auto"
        assert resolve_source(None, None) == "auto"

    def test_project_default_applies(self):
        assert resolve_source({}, {"SHABBAT_SOURCE_DEFAULT": "schedule"}) == "schedule"
        assert resolve_source({}, {"SHABBAT_SOURCE_DEFAULT": "none"}) == "none"

    def test_per_elevator_beats_default(self):
        settings = {"SHABBAT_SOURCE_DEFAULT": "schedule"}
        assert resolve_source({"SHABBAT_SOURCE": "auto"}, settings) == "auto"
        assert resolve_source({"SHABBAT_SOURCE": "none"}, settings) == "none"

    def test_junk_values_fall_through(self):
        settings = {"SHABBAT_SOURCE_DEFAULT": "schedule"}
        assert resolve_source({"SHABBAT_SOURCE": "banana"}, settings) == "schedule"
        assert resolve_source({"SHABBAT_SOURCE": True}, settings) == "schedule"
        assert resolve_source({"SHABBAT_SOURCE": "x"}, {"SHABBAT_SOURCE_DEFAULT": "y"}) == "auto"

    def test_value_object_unwrapped(self):
        assert resolve_source({"SHABBAT_SOURCE": {"value": "schedule"}}, {}) == "schedule"
        assert resolve_source({}, {"SHABBAT_SOURCE_DEFAULT": {"value": "none"}}) == "none"

    def test_none_value_inherits(self):
        # setup.html writes null to delete the key; a literal None inherits too
        assert resolve_source({"SHABBAT_SOURCE": None}, {"SHABBAT_SOURCE_DEFAULT": "schedule"}) == "schedule"


class TestScheduleOffsets:
    def test_defaults_match_browser_fallback(self):
        assert schedule_offsets({}) == (100.0, 60.0)
        assert schedule_offsets(None) == (100.0, 60.0)

    def test_configured_values(self):
        s = {"SHABBAT_SCHEDULE_BEFORE_MINUTES": 30, "SHABBAT_SCHEDULE_AFTER_MINUTES": 45}
        assert schedule_offsets(s) == (30.0, 45.0)

    def test_zero_is_respected_not_defaulted(self):
        s = {"SHABBAT_SCHEDULE_BEFORE_MINUTES": 0, "SHABBAT_SCHEDULE_AFTER_MINUTES": 0}
        assert schedule_offsets(s) == (0.0, 0.0)

    def test_junk_and_negative_fall_back_to_defaults(self):
        s = {"SHABBAT_SCHEDULE_BEFORE_MINUTES": "abc", "SHABBAT_SCHEDULE_AFTER_MINUTES": -5}
        assert schedule_offsets(s) == (100.0, 60.0)

    def test_numeric_strings_accepted(self):
        s = {"SHABBAT_SCHEDULE_BEFORE_MINUTES": "90", "SHABBAT_SCHEDULE_AFTER_MINUTES": "30"}
        assert schedule_offsets(s) == (90.0, 30.0)
