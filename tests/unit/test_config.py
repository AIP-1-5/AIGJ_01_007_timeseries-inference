import pytest

from collector.config import ConfigError, load_config, load_env_file, mqtt_connection
from tests.conftest import CONFIG

REPO_CONFIG = CONFIG


def write(tmp_path, text, name="c.toml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_repo_config_loads():
    cfg = load_config(REPO_CONFIG)
    assert cfg.mqtt.uplink_topic == "application/+/device/+/event/up"
    assert cfg.mqtt.join_topic == "application/+/device/+/event/join"
    assert cfg.fcnt.reset_max == 1000 and cfg.fcnt.min_drop == 100
    assert cfg.unmapped_policy == "store"
    assert cfg.models["AM107"].keys["co2"] == "co2"
    assert cfg.models["EM500-CO2"].interval_s == 120
    assert cfg.devices["24e124725d089152"].interval_s == 600
    assert not cfg.storage.db_path.startswith("~")


def test_defaults_filled(tmp_path):
    cfg = load_config(write(tmp_path, "[models.X.keys]\na = 'a'\n"))
    assert cfg.mqtt.qos == 1
    assert cfg.mqtt.clean_session is False
    assert cfg.storage.queue_max == 50000
    assert cfg.storage.heartbeat_s == 60
    assert cfg.models["X"].interval_s is None


def test_toml_syntax_error(tmp_path):
    with pytest.raises(ConfigError, match="TOML"):
        load_config(write(tmp_path, "[mqtt\n"))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


@pytest.mark.parametrize("key", ["password", "username", "token", "host"])
def test_secret_keys_forbidden(tmp_path, key):
    with pytest.raises(ConfigError, match=key):
        load_config(write(tmp_path, f"[mqtt]\n{key} = 'x'\n"))


def test_bad_policy(tmp_path):
    with pytest.raises(ConfigError, match="policy"):
        load_config(write(tmp_path, "[unmapped]\npolicy = 'keep'\n"))


def test_bad_types(tmp_path):
    with pytest.raises(ConfigError, match="reset_max"):
        load_config(write(tmp_path, "[fcnt]\nreset_max = 'big'\n"))
    with pytest.raises(ConfigError, match="keys"):
        load_config(write(tmp_path, "[models.X.keys]\na = 3\n"))


def test_env_file_parser(tmp_path):
    p = write(
        tmp_path,
        "# comment\n\nMQTT_HOST=broker.example\nexport MQTT_PORT=1884\n"
        "MQTT_USERNAME=\"user one\"\nMQTT_PASSWORD='p#w'\nBROKEN LINE\n",
        ".env",
    )
    env = load_env_file(p)
    assert env == {
        "MQTT_HOST": "broker.example",
        "MQTT_PORT": "1884",
        "MQTT_USERNAME": "user one",
        "MQTT_PASSWORD": "p#w",
    }


def test_env_file_missing_is_empty(tmp_path):
    assert load_env_file(tmp_path / "none") == {}


def test_environment_overrides_env_file(tmp_path, monkeypatch):
    p = write(tmp_path, "MQTT_HOST=from-file\nMQTT_PASSWORD=secret\n", ".env")
    monkeypatch.setenv("MQTT_HOST", "from-env")
    monkeypatch.delenv("MQTT_PASSWORD", raising=False)
    monkeypatch.delenv("MQTT_PORT", raising=False)
    conn = mqtt_connection(p)
    assert conn.host == "from-env"
    assert conn.port == 1883
    assert conn.password == "secret"
    assert "secret" not in repr(conn)


def test_empty_env_value_overrides(tmp_path, monkeypatch):
    # 로컬 브로커 시험: MQTT_USERNAME= 로 .env 계정을 지운다
    p = write(tmp_path, "MQTT_HOST=h\nMQTT_USERNAME=u\n", ".env")
    monkeypatch.setenv("MQTT_USERNAME", "")
    assert mqtt_connection(p).username == ""


def test_missing_host(tmp_path, monkeypatch):
    monkeypatch.delenv("MQTT_HOST", raising=False)
    with pytest.raises(ConfigError, match="MQTT_HOST"):
        mqtt_connection(tmp_path / "none")
