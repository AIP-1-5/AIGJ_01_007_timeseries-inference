"""무거운 작업(모델 학습, 1년치 분석)은 맥에서만 한다. 보드에서 시작하면 데이터를 읽기 전에 멈춘다.

2026-10-21 보드 B에서 학습·측정을 이어 돌리다 메모리가 98%(7,375/7,485MB)까지 찼고 보드가 재부팅됐다.
보드 B는 팀원 4명과 각자의 수집기가 같이 쓴다. 그래서 우회 옵션(환경 변수 등)은 두지 않는다 (decisions R9).
"""

from __future__ import annotations

from pathlib import Path

TEGRA_RELEASE = Path("/etc/nv_tegra_release")
DT_MODEL = Path("/proc/device-tree/model")


def on_jetson() -> bool:
    if TEGRA_RELEASE.exists():
        return True
    try:
        return "jetson" in DT_MODEL.read_text(errors="ignore").lower()
    except OSError:
        return False


def refuse_on_board(what: str) -> None:
    """보드(Jetson)면 바로 종료한다. 학습·분석 명령의 맨 앞과 학습 장치 선택에서 부른다."""
    if on_jetson():
        raise SystemExit(
            f"[중지] {what}은(는) 보드에서 하지 않는다. 맥에서 실행한다.\n"
            "  보드에서 할 일: 수집기, InfluxDB 내려받기(python -m tsdata.influx), TensorRT 변환·측정, 서비스.\n"
            "  데이터 옮기기: scp -r board-b:~/data/tsinfer ~/data/  /  모델 돌려보내기: scp ~/data/tsinfer/models/*.pt board-b:~/data/tsinfer/models/")


def available_mb() -> int:
    """/proc/meminfo의 MemAvailable(MB). 보드는 CPU·GPU가 이 메모리를 같이 쓴다."""
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def require_free_memory(min_mb: int, what: str) -> None:
    """보드에서만 해야 하는 측정(TensorRT, 사전학습 모델)도 남은 메모리가 모자라면 시작하지 않는다.
    보드 B는 팀원 4명과 수집기·서비스가 같이 쓰므로, 남이 쓰는 중이면 기다렸다가 다시 한다."""
    free = available_mb()
    if free < min_mb:
        raise SystemExit(f"[중지] {what}: 남은 메모리 {free}MB < 필요 {min_mb}MB. "
                         "다른 작업이 끝난 뒤(free -m) 다시 실행한다.")


def train_device():
    """학습 장치 = 맥 GPU(MPS)만. 보드면 중지, MPS가 없으면 CPU로 넘어가지 않고 중지."""
    import torch

    refuse_on_board("모델 학습")
    if not torch.backends.mps.is_available():
        raise SystemExit("[중지] 학습은 맥 GPU(MPS)에서만 한다. torch.backends.mps.is_available()가 False다 "
                         "(Apple Silicon 맥, macOS 12.3 이상, arm64 파이썬인지 확인).")
    return torch.device("mps")
