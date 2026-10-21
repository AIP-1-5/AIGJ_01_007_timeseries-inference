"""fcnt_epoch(재가입 구간 번호) 판정. 순수 함수만 둔다. research R2, data-model 상태 전이."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceState:
    fcnt_epoch: int
    last_f_cnt: int            # 현재 epoch의 마지막 fCnt. join 직후 -1
    last_measured_at: str | None


def decide(state: DeviceState | None, f_cnt: int, measured_at: str,
           reset_max: int, min_drop: int) -> tuple[int, DeviceState, bool]:
    """업링크 하나의 epoch을 정한다. 반환: (저장할 epoch, 새 상태, epoch을 올렸는가)."""
    if state is None:
        return 0, DeviceState(0, f_cnt, measured_at), False

    newer = state.last_measured_at is None or measured_at > state.last_measured_at
    # 늦게 온 메시지(측정 시각이 마지막보다 이름)는 상태를 움직이지 않는다
    if not newer:
        return state.fcnt_epoch, state, False

    if f_cnt >= state.last_f_cnt:
        return state.fcnt_epoch, DeviceState(state.fcnt_epoch, f_cnt, measured_at), False

    if f_cnt <= reset_max and state.last_f_cnt - f_cnt >= min_drop:
        epoch = state.fcnt_epoch + 1
        return epoch, DeviceState(epoch, f_cnt, measured_at), True

    # 그 밖의 감소(중복·늦게 옴): 원래 epoch에 저장, 상태 변경 없음
    return state.fcnt_epoch, state, False


def on_join(state: DeviceState | None) -> DeviceState:
    """join 이벤트: epoch+1, last_f_cnt=-1. 처음 보는 센서는 epoch 0에서 시작."""
    if state is None:
        return DeviceState(0, -1, None)
    return DeviceState(state.fcnt_epoch + 1, -1, state.last_measured_at)
