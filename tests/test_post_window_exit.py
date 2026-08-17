"""Tests for the post-window exit and the window-scoped CANDIDATE_EXIT timeout.

Background - Ramada elevator B, 2026-08-15.  Havdalah was 20:15; A and D left
Shabbat mode by 20:42, B stayed in it until 23:25.  B's Shabbat program stops at
*every* floor, so `STOPPING_FLOORS_UP | STOPPING_FLOORS_DOWN | terminals` covers
the whole building and no stop it ever makes can be "illegal".  Every
violation-driven exit path is therefore structurally dead for B, and the one
remaining path (a full non-matching round trip) depends on passenger traffic
happening to complete a terminal-to-terminal sweep.

These tests pin the replacement: outside the halachic window, staying in Shabbat
requires a still-arriving Shabbat cycle rather than requiring evidence of
misbehaviour to leave.
"""
import pytest

from shabbat_detector.cycle_analyzer import Cycle
from shabbat_detector.fsm import DetectorState, ElevatorFSM, Violation


HOUR = 3600.0

# Ramada B: stops at every floor on the way down - the config that makes every
# violation-based exit path unreachable.
CONFIG_B = {
    "TOP_FLOOR": "12",
    "BOTTOM_FLOOR": "-3",
    "TIME_PER_FLOOR": 27,
    "TIME_PASS_FLOOR": 1.4,
    "STOPPING_FLOORS_UP": ["-2", "-1", "0", "12"],
    "STOPPING_FLOORS_DOWN": ["-3", "-2", "-1", "0", "1", "2", "3", "4", "5",
                             "6", "7", "8", "9", "10", "11"],
    "FLOOR_WAITS": {"-1": 78},
}

# The live Ramada tuning: a 25-hour stickiness and no cadence check.
SETTINGS = {
    "SHABBAT_DETECTION": {
        "STICKINESS_MINUTES": 1500,
        "MISSED_CYCLE_FACTOR": 0,
        "CONSECUTIVE_NONMATCH_FOR_EXIT": 1,
        "CANDIDATE_EXIT_TIMEOUT_MIN": 10,
        "VIOLATIONS_FOR_EXIT": 2,
        "VIOLATION_WINDOW_MINUTES": 10,
    }
}


def make_fsm(settings=None, entered_at=1000.0, period=657.0):
    """An FSM already in SHABBAT, as it would be mid-Shabbat."""
    fsm = ElevatorFSM("B")
    fsm.update_settings(settings if settings is not None else SETTINGS)
    fsm.state = DetectorState.SHABBAT
    fsm._shabbat_entered_at = entered_at
    fsm._entered_state_at = entered_at
    fsm._last_clean_cycle_ts = entered_at
    fsm._expected_cycle_period = period
    return fsm


def matching_cycle(start_ts, duration=605.0):
    """A textbook Shabbat sweep for CONFIG_B: express up, every floor down."""
    return Cycle(
        start_terminal="BOTTOM",
        start_ts=start_ts,
        end_ts=start_ts + duration,
        up_stops=["-2", "-1", "0"],
        down_stops=["11", "10", "9", "8", "7", "6", "5", "4", "3", "2", "1", "0", "-1", "-2"],
        up_passes=[],
        down_passes=[],
        up_dwells={"-2": 27, "-1": 78, "0": 27},
        down_dwells={f: 27 for f in
                     ["11", "10", "9", "8", "7", "6", "5", "4", "3", "2", "1", "0", "-2"]} | {"-1": 78},
    )


class TestPostWindowExit:
    def test_exits_once_window_closed_and_sweeps_stopped(self):
        """The B case: no violations are possible, yet it must still leave."""
        fsm = make_fsm()
        now = 1000.0
        assert fsm.check_post_window_exit(now, True, CONFIG_B) is None   # in window

        now += HOUR                       # window closes; grace clock starts
        assert fsm.check_post_window_exit(now, False, CONFIG_B) is None
        now += 14 * 60                    # still inside the 15-minute grace
        assert fsm.check_post_window_exit(now, False, CONFIG_B) is None

        now += 2 * 60                     # grace elapsed, no cycle since entry
        result = fsm.check_post_window_exit(now, False, CONFIG_B)
        assert result is not None
        assert result.new_state == DetectorState.NORMAL
        assert result.shabbat_active is False
        assert fsm.state == DetectorState.NORMAL

    def test_never_fires_inside_the_window(self):
        """The anti-false-exit guarantee: silence inside Shabbat is not evidence.

        An in-window cadence check is what produced the mid-Shabbat false exits
        that got MISSED_CYCLE_FACTOR disabled fleet-wide.
        """
        fsm = make_fsm()
        now = 1000.0
        for _ in range(48):               # a full day of no cycles at all
            now += 1800
            assert fsm.check_post_window_exit(now, True, CONFIG_B) is None
        assert fsm.state == DetectorState.SHABBAT

    def test_running_program_past_havdalah_is_not_evicted(self):
        """A hotel whose sweeps continue past havdalah stays in Shabbat mode."""
        fsm = make_fsm()
        now = 1000.0 + HOUR
        fsm.check_post_window_exit(now, False, CONFIG_B)      # arm the grace

        for _ in range(10):               # ~100 minutes of continuing sweeps
            now += 605
            fsm.on_cycle_completed(matching_cycle(now - 605), CONFIG_B, SETTINGS, now, False)
            assert fsm.check_post_window_exit(now, False, CONFIG_B) is None
            assert fsm.state == DetectorState.SHABBAT

        now += 45 * 60                    # sweeps stop -> it leaves
        assert fsm.check_post_window_exit(now, False, CONFIG_B) is not None

    def test_matching_cycle_keeps_refreshing_the_anchor(self):
        fsm = make_fsm()
        now = 1000.0 + HOUR
        fsm.check_post_window_exit(now, False, CONFIG_B)
        now += 20 * 60                    # grace has elapsed...
        fsm.on_cycle_completed(matching_cycle(now - 605), CONFIG_B, SETTINGS, now, False)
        # ...but a fresh matching cycle means the program is demonstrably alive.
        assert fsm.check_post_window_exit(now + 60, False, CONFIG_B) is None

    def test_reentering_window_disarms_the_grace(self):
        fsm = make_fsm()
        now = 1000.0 + HOUR
        fsm.check_post_window_exit(now, False, CONFIG_B)
        assert fsm._left_window_at is not None
        fsm.check_post_window_exit(now + 60, True, CONFIG_B)
        assert fsm._left_window_at is None
        # The grace restarts from scratch rather than resuming mid-count.
        now += 300
        fsm.check_post_window_exit(now, False, CONFIG_B)
        assert fsm.check_post_window_exit(now + 14 * 60, False, CONFIG_B) is None

    def test_hebcal_outage_cannot_cause_an_exit(self):
        """HebcalGate is fail-open: unreachable Hebcal reports 'in window'."""
        fsm = make_fsm()
        now = 1000.0
        for _ in range(20):
            now += HOUR
            assert fsm.check_post_window_exit(now, True, CONFIG_B) is None
        assert fsm.state == DetectorState.SHABBAT

    def test_disabled_by_tunable(self):
        settings = {"SHABBAT_DETECTION": dict(SETTINGS["SHABBAT_DETECTION"],
                                              POST_WINDOW_EXIT_ENABLED=False)}
        fsm = make_fsm(settings)
        now = 1000.0 + HOUR
        fsm.check_post_window_exit(now, False, CONFIG_B)
        assert fsm.check_post_window_exit(now + 5 * HOUR, False, CONFIG_B) is None

    def test_ignores_states_other_than_shabbat(self):
        fsm = make_fsm()
        fsm.state = DetectorState.NORMAL
        assert fsm.check_post_window_exit(1000.0 + 5 * HOUR, False, CONFIG_B) is None

    def test_no_period_means_no_opinion(self):
        """Without a usable config-implied period there is no cadence to judge."""
        fsm = make_fsm(period=0.0)
        now = 1000.0 + HOUR
        fsm.check_post_window_exit(now, False, {})
        assert fsm.check_post_window_exit(now + 5 * HOUR, False, {}) is None


class TestStickinessIsWindowAnchored:
    def test_lapses_outside_the_window(self):
        """A 25-hour stickiness must not outlive Shabbat itself.

        B enters ~14 minutes later than A and D, so an entry-anchored 1500-minute
        block held it past the point where its evidence had come and gone.
        """
        fsm = make_fsm()
        now = 1000.0 + 2 * HOUR           # far short of 1500 minutes
        assert fsm._stickiness_expired(now, hebcal_in_window=True) is False
        assert fsm._stickiness_expired(now, hebcal_in_window=False) is True

    def test_still_protects_inside_the_window(self):
        fsm = make_fsm()
        now = 1000.0 + 2 * HOUR
        v = Violation(ts=now, floor="4", reason="illegal stop")
        result = fsm.process_violation(v, CONFIG_B, now, hebcal_in_window=True)
        assert fsm.state == DetectorState.SHABBAT
        assert "הדבקה" in result.reason_he


class TestCandidateExitTimeout:
    def _in_candidate_exit(self, started_at):
        fsm = make_fsm()
        fsm.state = DetectorState.CANDIDATE_EXIT
        fsm._candidate_exit_started = started_at
        fsm._entered_state_at = started_at
        return fsm

    def test_fires_on_the_clock_outside_the_window(self):
        """B sat in CANDIDATE_EXIT from 21:59 with a 10-minute timeout and only
        left at 23:25, when an unrelated no-report violation woke the FSM."""
        fsm = self._in_candidate_exit(1000.0)
        assert fsm.check_candidate_exit_timeout(1000.0 + 9 * 60, False) is None
        result = fsm.check_candidate_exit_timeout(1000.0 + 11 * 60, False)
        assert result is not None
        assert result.new_state == DetectorState.NORMAL
        assert result.shabbat_active is False

    def test_never_fires_inside_the_window(self):
        """Replaying Ramada C over 2026-08-01 with an unconditional clock timeout
        exited 13 minutes *before* havdalah, pre-empting the clean cycle that
        would have rescued CANDIDATE_EXIT back to SHABBAT."""
        fsm = self._in_candidate_exit(1000.0)
        assert fsm.check_candidate_exit_timeout(1000.0 + 5 * HOUR, True) is None
        assert fsm.state == DetectorState.CANDIDATE_EXIT

    def test_clean_cycle_still_rescues(self):
        fsm = self._in_candidate_exit(1000.0)
        now = 1000.0 + 605
        fsm.on_cycle_completed(matching_cycle(now - 605), CONFIG_B, SETTINGS, now, True)
        assert fsm.state == DetectorState.SHABBAT


class TestPersistence:
    def test_left_window_at_survives_a_restart(self):
        """The grace clock must not restart on every detector restart."""
        fsm = make_fsm()
        fsm.check_post_window_exit(1000.0 + HOUR, False, CONFIG_B)
        restored = ElevatorFSM.from_dict("B", fsm.to_dict())
        assert restored._left_window_at == fsm._left_window_at

    def test_absent_key_restores_as_none(self):
        fsm = ElevatorFSM.from_dict("B", {"state": "SHABBAT"})
        assert fsm._left_window_at is None


class TestEveryFloorConfigHasNoIllegalStops:
    def test_b_config_covers_the_whole_building(self):
        """The structural reason B can never produce a violation-based exit."""
        valid = (set(CONFIG_B["STOPPING_FLOORS_UP"])
                 | set(CONFIG_B["STOPPING_FLOORS_DOWN"])
                 | {CONFIG_B["TOP_FLOOR"], CONFIG_B["BOTTOM_FLOOR"]})
        every_floor = {str(f) for f in range(-3, 13)}
        assert every_floor <= valid, "a floor outside the stop-set would be catchable"
