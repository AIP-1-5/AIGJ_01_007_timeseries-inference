"""무거운 작업은 맥에서만: 보드면 데이터를 읽기 전에 멈추고, 학습 장치는 MPS뿐이다."""

import numpy as np
import pytest
import torch

from tsdata import guard


@pytest.fixture
def board(monkeypatch):
    monkeypatch.setattr(guard, "on_jetson", lambda: True)


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(guard, "on_jetson", lambda: False)


def test_board_detected_by_tegra_release(tmp_path, monkeypatch):
    f = tmp_path / "nv_tegra_release"
    f.write_text("# R39")
    monkeypatch.setattr(guard, "TEGRA_RELEASE", f)
    assert guard.on_jetson()


def test_board_detected_by_device_tree(tmp_path, monkeypatch):
    m = tmp_path / "model"
    m.write_text("NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super\0")
    monkeypatch.setattr(guard, "TEGRA_RELEASE", tmp_path / "none")
    monkeypatch.setattr(guard, "DT_MODEL", m)
    assert guard.on_jetson()


def test_not_board_without_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "TEGRA_RELEASE", tmp_path / "none")
    monkeypatch.setattr(guard, "DT_MODEL", tmp_path / "none2")
    assert not guard.on_jetson()


@pytest.mark.parametrize("cmd", ["forecast", "anomaly", "state"])
def test_tsmodel_training_stops_on_board_before_reading_data(board, monkeypatch, cmd):
    import tsmodel.__main__ as tm
    monkeypatch.setattr(tm, "load_config", lambda *_: pytest.fail("데이터·설정을 읽기 전에 멈춰야 한다"))
    with pytest.raises(SystemExit, match="보드에서 하지 않는다"):
        tm.main([cmd])


def test_tsdata_analysis_stops_on_board(board, monkeypatch):
    import tsdata.__main__ as td
    monkeypatch.setattr(td, "load_config", lambda *_: pytest.fail("설정을 읽기 전에 멈춰야 한다"))
    with pytest.raises(SystemExit, match="보드에서 하지 않는다"):
        td.main(["all"])


def test_board_measurement_commands_are_not_blocked(board, monkeypatch):
    monkeypatch.setattr(guard, "available_mb", lambda: 8000)
    import tsmodel.__main__ as tm
    called = []
    monkeypatch.setattr(tm, "load_config", lambda *_: called.append(1))
    monkeypatch.setattr(tm, "cmd_edge", lambda cfg: None)
    tm.main(["edge"])
    assert called


@pytest.mark.parametrize("fn", ["forecast", "anomaly", "state"])
def test_training_function_stops_on_board(board, fn):
    """CLI를 거치지 않고 함수를 직접 불러도(노트북 등) 학습 장치 선택에서 멈춘다."""
    from tsmodel import anomaly, forecast, state
    x = np.zeros((4, 3), np.float32)
    call = {"forecast": lambda: forecast.train({"train": {}, "val": {}}, {}),
            "anomaly": lambda: anomaly.train_ae(x),
            "state": lambda: state.train_mlp(x, np.array([0, 1, 0, 1]))}[fn]
    with pytest.raises(SystemExit, match="보드에서 하지 않는다"):
        call()


def test_no_cpu_fallback_on_mac_without_mps(mac, monkeypatch):
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    with pytest.raises(SystemExit, match="MPS"):
        guard.train_device()


def test_mac_with_mps_uses_mps(mac, monkeypatch):
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
    assert guard.train_device().type == "mps"


def test_board_measurement_refuses_when_memory_low(monkeypatch):
    import tsmodel.__main__ as tm
    monkeypatch.setattr(guard, "available_mb", lambda: 1200)
    monkeypatch.setattr(tm, "load_config", lambda *_: pytest.fail("메모리가 모자라면 설정도 읽지 않는다"))
    with pytest.raises(SystemExit, match="남은 메모리 1200MB"):
        tm.main(["edge"])


def test_available_mb_reads_meminfo():
    assert guard.available_mb() > 0
