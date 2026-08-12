"""Autopilot settings are attacker-controlled input from the dashboard.

Every field arrives as JSON from a browser form, then drives unattended spending
(YouTube quota, Gemini calls, social posts). Validation is the only thing between
a typo and a machine that publishes 40 times a day, so it is tested as a
boundary, not as a formality.
"""
import pytest

from automation.config import (
    POLICY_CREATIVE_COMMONS, POLICY_OWNED_OR_ALLOWLISTED, ConfigError, normalise,
    validate_timezone,
)


class TestTimezone:
    def test_accepts_a_real_iana_zone(self):
        assert validate_timezone("America/New_York") == "America/New_York"

    def test_rejects_an_unknown_zone(self):
        with pytest.raises(ConfigError):
            validate_timezone("Mars/Olympus_Mons")

    def test_rejects_a_shell_injection_shaped_value(self):
        # Timezones end up in filenames and log lines; never accept punctuation.
        with pytest.raises(ConfigError):
            validate_timezone("UTC; rm -rf /")

    def test_rejects_a_utc_offset_string(self):
        # "+02:00" is not an IANA identifier and would silently ignore DST.
        with pytest.raises(ConfigError):
            validate_timezone("+02:00")


class TestNumericBounds:
    def test_posts_per_day_is_clamped_not_rejected(self):
        config = normalise({"schedule": {"max_posts_per_day": 9999}})
        assert config["schedule"]["max_posts_per_day"] == 12

    def test_negative_values_clamp_to_the_floor(self):
        config = normalise({"schedule": {"min_spacing_minutes": -500}})
        assert config["schedule"]["min_spacing_minutes"] == 0

    def test_non_numeric_input_is_an_error(self):
        with pytest.raises(ConfigError):
            normalise({"schedule": {"max_posts_per_day": "as many as possible"}})

    def test_duration_window_must_be_ordered(self):
        with pytest.raises(ConfigError):
            normalise({"eligibility": {"min_duration_seconds": 600,
                                       "max_duration_seconds": 300}})

    def test_clip_window_must_be_ordered(self):
        with pytest.raises(ConfigError):
            normalise({"clips": {"min_clip_seconds": 90, "max_clip_seconds": 30}})


class TestSchedules:
    def test_publish_times_must_be_hhmm(self):
        with pytest.raises(ConfigError):
            normalise({"schedule": {"publish_times": ["25:00"]}})

    def test_publish_times_are_sorted_and_deduplicated(self):
        config = normalise({"schedule": {"publish_times": ["21:00", "09:00", "21:00"]}})
        assert config["schedule"]["publish_times"] == ["09:00", "21:00"]

    def test_a_cron_expression_is_rejected(self):
        # No cron parser exists in Autopilot on purpose — an expression field is
        # an evaluation surface, and HH:MM covers every real posting schedule.
        with pytest.raises(ConfigError):
            normalise({"schedule": {"discovery_times": ["*/5 * * * *"]}})

    def test_days_of_week_outside_range_are_dropped(self):
        config = normalise({"schedule": {"days_of_week": [0, 3, 9, -1]}})
        assert config["schedule"]["days_of_week"] == [0, 3]

    def test_all_days_invalid_is_an_error(self):
        with pytest.raises(ConfigError):
            normalise({"schedule": {"days_of_week": [12, 13]}})


class TestRightsPolicy:
    def test_default_is_creative_commons(self):
        assert normalise(None)["rights"]["policy"] == POLICY_CREATIVE_COMMONS

    def test_unknown_policy_is_rejected(self):
        with pytest.raises(ConfigError):
            normalise({"rights": {"policy": "ANYTHING_GOES"}})

    def test_owned_channels_policy_requires_channel_ids(self):
        with pytest.raises(ConfigError):
            normalise({"rights": {"policy": POLICY_OWNED_OR_ALLOWLISTED,
                                  "allowlisted_channel_ids": []}})

    def test_channel_ids_must_look_like_youtube_ids(self):
        with pytest.raises(ConfigError):
            normalise({"rights": {"policy": POLICY_OWNED_OR_ALLOWLISTED,
                                  "allowlisted_channel_ids": ["@some-handle"]}})

    def test_valid_channel_id_is_kept(self):
        channel = "UC" + "a" * 22
        config = normalise({"rights": {"policy": POLICY_OWNED_OR_ALLOWLISTED,
                                       "allowlisted_channel_ids": [channel]}})
        assert config["rights"]["allowlisted_channel_ids"] == [channel]


class TestPlatformsAndStrategies:
    def test_unknown_platforms_are_dropped(self):
        config = normalise({"publishing": {"platforms": ["tiktok", "myspace"]}})
        assert config["publishing"]["platforms"] == ["tiktok"]

    def test_no_valid_platform_is_an_error(self):
        with pytest.raises(ConfigError):
            normalise({"publishing": {"platforms": ["myspace"]}})

    def test_niche_search_without_topics_is_rejected(self):
        # Enabling it with no topics would spend 100 quota units on an empty
        # query every run, which is exactly the misconfiguration to catch early.
        with pytest.raises(ConfigError):
            normalise({"discovery": {"strategies": ["niche_search"], "topics": []}})

    def test_region_code_must_be_two_letters(self):
        with pytest.raises(ConfigError):
            normalise({"discovery": {"region_code": "United States"}})


class TestNormalisationShape:
    def test_unknown_keys_are_dropped(self):
        config = normalise({"totally_made_up": {"x": 1}})
        assert "totally_made_up" not in config

    def test_round_trips_unchanged(self):
        # Normalising an already-normalised document must be a fixed point,
        # otherwise every settings save would drift the stored config.
        once = normalise({"timezone": "Asia/Tokyo",
                          "schedule": {"publish_times": ["08:00"]}})
        assert normalise(once) == once
