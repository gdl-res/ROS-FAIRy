from fair_ros.utils import topic_health
from tests.conftest import make_bag

SENSORS = [
    {"sensor_id": "gps0", "type": "gps", "make_model": "u-blox ZED-F9P",
     "topic": "/fix"},
    {"sensor_id": "sonar0", "type": "sonar", "make_model": "Ping2",
     "topic": "/depth"},
]

T0 = 1_750_000_000.0


def _steady(start, end, hz):
    n = int((end - start) * hz)
    return [start + i / hz for i in range(n + 1)]


def test_healthy_bag(tmp_path):
    bag = make_bag(tmp_path / "bag", {
        "/fix": _steady(T0, T0 + 60, 10),
        "/depth": _steady(T0, T0 + 60, 5),
    })
    assert topic_health.analyse_bag(bag, SENSORS) == []


def test_gap_detection_plain_text(tmp_path):
    # GPS silent from t=120 to t=360 (4 minutes), recording is 12+ min long
    stamps = _steady(T0, T0 + 120, 10) + _steady(T0 + 360, T0 + 720, 10)
    bag = make_bag(tmp_path / "bag", {
        "/fix": stamps,
        "/depth": _steady(T0, T0 + 720, 5),
    })
    warnings = topic_health.analyse_bag(bag, SENSORS)
    assert len(warnings) == 1
    w = warnings[0]
    assert w["kind"] == "gap"
    assert w["sensor_id"] == "gps0"
    assert 239 < w["duration_s"] < 241
    assert w["plain_text"] == \
        "GPS signal was lost for 4 minutes, starting 2 minutes in."


def test_slow_topic_not_flagged(tmp_path):
    # 0.2 Hz topic: 5 s intervals exceed 1 s but are its normal cadence
    bag = make_bag(tmp_path / "bag", {
        "/diagnostics": _steady(T0, T0 + 600, 0.2),
        "/fix": _steady(T0, T0 + 600, 10),
        "/depth": _steady(T0, T0 + 600, 5),
    })
    assert topic_health.analyse_bag(bag, SENSORS) == []


def test_never_published(tmp_path):
    bag = make_bag(tmp_path / "bag", {"/fix": _steady(T0, T0 + 60, 10)})
    warnings = topic_health.analyse_bag(bag, SENSORS)
    assert len(warnings) == 1
    w = warnings[0]
    assert w["kind"] == "never_published"
    assert w["sensor_id"] == "sonar0"
    assert "Sonar" in w["plain_text"]
    assert "no data at all" in w["plain_text"]


def test_trailing_gap(tmp_path):
    # depth stops 5 minutes before the end
    bag = make_bag(tmp_path / "bag", {
        "/fix": _steady(T0, T0 + 600, 10),
        "/depth": _steady(T0, T0 + 300, 5),
    })
    warnings = topic_health.analyse_bag(bag, SENSORS)
    assert len(warnings) == 1
    w = warnings[0]
    assert w["kind"] == "gap"
    assert w["sensor_id"] == "sonar0"
    assert "did not come back" in w["plain_text"]


def test_missing_metadata(tmp_path):
    bag = tmp_path / "bag"
    bag.mkdir()
    warnings = topic_health.analyse_bag(bag, SENSORS)
    assert warnings[0]["plain_text"] == \
        "The recording ended unexpectedly and may be incomplete."


def test_unknown_topic_friendly_name(tmp_path):
    stamps = _steady(T0, T0 + 100, 10) + _steady(T0 + 200, T0 + 300, 10)
    bag = make_bag(tmp_path / "bag", {
        "/mystery": stamps,
        "/fix": _steady(T0, T0 + 300, 10),
        "/depth": _steady(T0, T0 + 300, 5),
    })
    warnings = topic_health.analyse_bag(bag, SENSORS)
    assert len(warnings) == 1
    assert "(/mystery)" in warnings[0]["plain_text"]
    assert warnings[0]["sensor_id"] is None


def test_humanize_duration():
    h = topic_health.humanize_duration
    assert h(1) == "1 second"
    assert h(45) == "45 seconds"
    assert h(243.2) == "4 minutes"
    assert h(3600) == "1 hour"
    assert h(5460) == "1h 31m"


def test_mcap_bag_metadata_checks_only(tmp_path):
    bag = make_bag(tmp_path / "bag", {
        "/fix": [T0, T0 + 100],  # would be a huge gap if analysed
        "/depth": _steady(T0, T0 + 100, 5),
    }, storage="mcap")
    assert topic_health.analyse_bag(bag, SENSORS) == []


def test_metadata_without_storage_id_infers_from_distro(tmp_path, monkeypatch):
    """A bag whose metadata omits storage_identifier falls back to the
    recording distro's default rather than blindly assuming sqlite3."""
    bag = make_bag(tmp_path / "bag", {"/fix": [T0, T0 + 1]})
    meta_path = bag / "metadata.yaml"
    lines = [ln for ln in meta_path.read_text().splitlines()
             if "storage_identifier" not in ln]
    meta_path.write_text("\n".join(lines))

    monkeypatch.setenv("ROS_DISTRO", "jazzy")
    assert topic_health.parse_bag_metadata(bag)["storage_identifier"] == "mcap"
    monkeypatch.setenv("ROS_DISTRO", "humble")
    assert topic_health.parse_bag_metadata(bag)["storage_identifier"] == \
        "sqlite3"
