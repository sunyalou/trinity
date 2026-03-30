"""
Tests for cron expression parsing.
"""

# Path setup must happen before scheduler imports
import sys
from pathlib import Path
_this_file = Path(__file__).resolve()
_src_path = str(_this_file.parent.parent.parent / 'src')
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)
import os

from datetime import datetime
import pytest
import pytz
from apscheduler.triggers.cron import CronTrigger

from scheduler.service import SchedulerService


class TestCronParsing:
    """Tests for cron expression parsing."""

    def setup_method(self):
        """Set up test fixtures."""
        # Create a minimal service just for testing cron parsing
        self.service = SchedulerService.__new__(SchedulerService)

    def test_parse_standard_cron(self):
        """Test parsing standard cron expressions."""
        # Every day at 9:00 AM
        result = self.service._parse_cron("0 9 * * *")
        assert result == {
            'minute': '0',
            'hour': '9',
            'day': '*',
            'month': '*',
            'day_of_week': '*'
        }

    def test_parse_every_15_minutes(self):
        """Test parsing every 15 minutes cron."""
        result = self.service._parse_cron("*/15 * * * *")
        assert result == {
            'minute': '*/15',
            'hour': '*',
            'day': '*',
            'month': '*',
            'day_of_week': '*'
        }

    def test_parse_weekly_cron(self):
        """Test parsing weekly cron (Sunday at midnight)."""
        result = self.service._parse_cron("0 0 * * 0")
        assert result == {
            'minute': '0',
            'hour': '0',
            'day': '*',
            'month': '*',
            'day_of_week': '0'
        }

    def test_parse_complex_cron(self):
        """Test parsing complex cron expression."""
        # 9:30 AM on weekdays
        result = self.service._parse_cron("30 9 * * 1-5")
        assert result == {
            'minute': '30',
            'hour': '9',
            'day': '*',
            'month': '*',
            'day_of_week': '1-5'
        }

    def test_parse_invalid_cron_too_few_parts(self):
        """Test that invalid cron with too few parts raises error."""
        with pytest.raises(ValueError) as exc_info:
            self.service._parse_cron("0 9 * *")

        assert "Expected 5 parts" in str(exc_info.value)

    def test_parse_invalid_cron_too_many_parts(self):
        """Test that invalid cron with too many parts raises error."""
        with pytest.raises(ValueError) as exc_info:
            self.service._parse_cron("0 9 * * * *")

        assert "Expected 5 parts" in str(exc_info.value)

    def test_parse_cron_with_whitespace(self):
        """Test parsing cron with extra whitespace."""
        result = self.service._parse_cron("  0  9  *  *  *  ")
        assert result['minute'] == '0'
        assert result['hour'] == '9'


class TestNextRunTime:
    """Tests for next run time calculation."""

    def setup_method(self):
        """Set up test fixtures."""
        self.service = SchedulerService.__new__(SchedulerService)

    def test_next_run_utc(self):
        """Test next run time calculation in UTC."""
        next_run = self.service._get_next_run_time("0 9 * * *", "UTC")

        assert next_run is not None
        assert next_run.hour == 9
        assert next_run.minute == 0

    def test_next_run_with_timezone(self):
        """Test next run time calculation with timezone."""
        next_run = self.service._get_next_run_time("0 9 * * *", "America/New_York")

        assert next_run is not None
        # Verify timezone is applied
        tz = pytz.timezone("America/New_York")
        assert next_run.tzinfo is not None

    def test_next_run_every_15_minutes(self):
        """Test next run time for every 15 minutes."""
        next_run = self.service._get_next_run_time("*/15 * * * *", "UTC")

        assert next_run is not None
        assert next_run.minute in [0, 15, 30, 45]

    def test_next_run_invalid_expression(self):
        """Test next run time with invalid expression."""
        next_run = self.service._get_next_run_time("invalid cron", "UTC")
        assert next_run is None

    def test_next_run_invalid_timezone(self):
        """Test next run time with invalid timezone."""
        # Should fall back to UTC or return None
        next_run = self.service._get_next_run_time("0 9 * * *", "Invalid/Timezone")
        # Depending on implementation, this may return None or raise
        # In our implementation, it returns None on error
        assert next_run is None


class TestCronDowConversion:
    """
    Tests for _cron_dow_to_apscheduler (Issue #220).

    Unix cron numbers day-of-week as 0=Sun, 1=Mon, … 6=Sat.
    APScheduler numbers it as 0=Mon, 1=Tue, … 6=Sun.
    Passing raw cron numbers to CronTrigger shifts every weekday schedule
    by one day.  The fix converts numeric tokens to APScheduler named-day
    abbreviations (mon, tue, …) which are unambiguous.
    """

    def setup_method(self):
        self.service = SchedulerService.__new__(SchedulerService)

    # --- single numeric values ---

    def test_sunday_cron_0(self):
        assert self.service._cron_dow_to_apscheduler('0') == 'sun'

    def test_monday_cron_1(self):
        assert self.service._cron_dow_to_apscheduler('1') == 'mon'

    def test_tuesday_cron_2(self):
        assert self.service._cron_dow_to_apscheduler('2') == 'tue'

    def test_wednesday_cron_3(self):
        assert self.service._cron_dow_to_apscheduler('3') == 'wed'

    def test_thursday_cron_4(self):
        assert self.service._cron_dow_to_apscheduler('4') == 'thu'

    def test_friday_cron_5(self):
        assert self.service._cron_dow_to_apscheduler('5') == 'fri'

    def test_saturday_cron_6(self):
        assert self.service._cron_dow_to_apscheduler('6') == 'sat'

    def test_sunday_cron_7(self):
        """Cron allows 7 as an alias for Sunday."""
        assert self.service._cron_dow_to_apscheduler('7') == 'sun'

    # --- wildcard ---

    def test_wildcard(self):
        assert self.service._cron_dow_to_apscheduler('*') == '*'

    # --- comma-separated list ---

    def test_comma_list(self):
        """1,3,5 (Mon,Wed,Fri) → mon,wed,fri"""
        assert self.service._cron_dow_to_apscheduler('1,3,5') == 'mon,wed,fri'

    def test_comma_list_with_sunday(self):
        """0,6 (Sun,Sat) → sun,sat"""
        assert self.service._cron_dow_to_apscheduler('0,6') == 'sun,sat'

    # --- range ---

    def test_weekday_range(self):
        """1-5 (Mon–Fri) → mon-fri"""
        assert self.service._cron_dow_to_apscheduler('1-5') == 'mon-fri'

    def test_full_week_range(self):
        """0-6 → sun-sat"""
        assert self.service._cron_dow_to_apscheduler('0-6') == 'sun-sat'

    # --- named days passed through unchanged ---

    def test_named_day_passthrough(self):
        """Named days already in APScheduler format are left unchanged."""
        assert self.service._cron_dow_to_apscheduler('mon') == 'mon'
        assert self.service._cron_dow_to_apscheduler('fri') == 'fri'
        assert self.service._cron_dow_to_apscheduler('sun') == 'sun'

    # --- step expressions passed through unchanged ---

    def test_step_passthrough(self):
        assert self.service._cron_dow_to_apscheduler('*/2') == '*/2'


class TestAPSchedulerDayOfWeek:
    """
    Integration-level check: verify that a CronTrigger built from a parsed
    cron expression (after DOW translation) fires on the correct calendar
    day (Issue #220).
    """

    def setup_method(self):
        self.service = SchedulerService.__new__(SchedulerService)

    def _next_fire(self, cron_expr: str, from_dt: datetime) -> datetime:
        """Build a CronTrigger for cron_expr and return the next fire time."""
        cron_kwargs = self.service._parse_cron(cron_expr)
        trigger_kwargs = dict(cron_kwargs)
        trigger_kwargs['day_of_week'] = self.service._cron_dow_to_apscheduler(
            cron_kwargs['day_of_week']
        )
        trigger = CronTrigger(timezone=pytz.UTC, **trigger_kwargs)
        return trigger.get_next_fire_time(None, from_dt)

    def test_monday_schedule_fires_on_monday(self):
        """'5 9 * * 1' must fire on a Monday, not a Tuesday."""
        # Base time: Wednesday 2026-03-25 (unambiguous — well before target)
        base = datetime(2026, 3, 25, 0, 0, 0, tzinfo=pytz.UTC)
        nxt = self._next_fire('5 9 * * 1', base)
        assert nxt.strftime('%A') == 'Monday', (
            f"Expected Monday, got {nxt.strftime('%A %Y-%m-%d')}"
        )
        assert nxt == datetime(2026, 3, 30, 9, 5, 0, tzinfo=pytz.UTC)

    def test_monday_schedule_does_not_skip_after_eu_dst(self):
        """
        Regression for Issue #220: weekly Monday schedule must not skip the
        Monday immediately after the European DST transition (Mar 29 2026).

        Without the fix, CronTrigger(day_of_week='1') fires on Tuesday Mar 31
        instead of Monday Mar 30, and the displayed next_run_at (from croniter)
        diverges from the actual APScheduler fire time, creating an apparent
        two-week gap in the UI.
        """
        # Simulate the state just after EU DST (clocks sprang forward Mar 29)
        after_dst = datetime(2026, 3, 29, 12, 0, 0, tzinfo=pytz.UTC)
        nxt = self._next_fire('5 9 * * 1', after_dst)

        assert nxt.strftime('%A') == 'Monday', (
            f"Schedule skipped Monday; next fire is {nxt.strftime('%A %Y-%m-%d')}"
        )
        assert nxt.date() == datetime(2026, 3, 30).date(), (
            f"Expected 2026-03-30, got {nxt.date()}"
        )

    def test_sunday_schedule_fires_on_sunday(self):
        """'0 0 * * 0' (cron 0=Sun) must fire on Sunday."""
        base = datetime(2026, 3, 25, 0, 0, 0, tzinfo=pytz.UTC)  # Wednesday
        nxt = self._next_fire('0 0 * * 0', base)
        assert nxt.strftime('%A') == 'Sunday', (
            f"Expected Sunday, got {nxt.strftime('%A %Y-%m-%d')}"
        )

    def test_weekday_range_fires_on_weekdays(self):
        """'0 9 * * 1-5' must fire on weekdays Mon–Fri."""
        base = datetime(2026, 3, 28, 12, 0, 0, tzinfo=pytz.UTC)  # Saturday
        nxt = self._next_fire('0 9 * * 1-5', base)
        assert nxt.strftime('%A') == 'Monday', (
            f"Expected Monday after Saturday, got {nxt.strftime('%A %Y-%m-%d')}"
        )
