from collector.epoch import DeviceState, decide, on_join

T0 = "2026-10-21T05:00:00.000000Z"
T1 = "2026-10-21T05:01:00.000000Z"
T2 = "2026-10-21T05:02:00.000000Z"
KW = dict(reset_max=1000, min_drop=100)


def test_first_seen_device():
    epoch, st, bumped = decide(None, 212964, T0, **KW)
    assert (epoch, bumped) == (0, False)
    assert st == DeviceState(0, 212964, T0)


def test_increase_same_epoch():
    epoch, st, bumped = decide(DeviceState(0, 100, T0), 101, T1, **KW)
    assert (epoch, bumped) == (0, False)
    assert st == DeviceState(0, 101, T1)


def test_duplicate_no_change():
    s = DeviceState(0, 212969, T1)
    epoch, st, bumped = decide(s, 212969, T0, **KW)
    assert (epoch, st, bumped) == (0, s, False)


def test_small_decrease_no_change():
    s = DeviceState(0, 212969, T1)
    epoch, st, bumped = decide(s, 212965, T0, **KW)
    assert (epoch, st, bumped) == (0, s, False)


def test_reset_bumps_epoch():
    epoch, st, bumped = decide(DeviceState(2, 212969, T0), 3, T1, **KW)
    assert (epoch, bumped) == (3, True)
    assert st == DeviceState(3, 3, T1)


def test_reset_shaped_but_late_message_no_bump():
    s = DeviceState(0, 5000, T1)
    epoch, st, bumped = decide(s, 3, T0, **KW)  # measured_at이 더 이름
    assert (epoch, st, bumped) == (0, s, False)


def test_decrease_above_reset_max_no_bump():
    s = DeviceState(0, 212969, T0)
    epoch, st, bumped = decide(s, 5000, T1, **KW)
    assert (epoch, st, bumped) == (0, s, False)


def test_decrease_smaller_than_min_drop_no_bump():
    s = DeviceState(0, 150, T0)
    epoch, st, bumped = decide(s, 60, T1, **KW)  # 감소 90 < 100
    assert (epoch, st, bumped) == (0, s, False)


def test_join_then_zero_no_double_bump():
    st = on_join(DeviceState(0, 212969, T0))
    assert st == DeviceState(1, -1, T0)
    epoch, st2, bumped = decide(st, 0, T1, **KW)
    assert (epoch, bumped) == (1, False)
    assert st2 == DeviceState(1, 0, T1)


def test_join_unseen_device():
    assert on_join(None) == DeviceState(0, -1, None)


def test_late_higher_fcnt_does_not_move_state():
    # 재가입 직후, 이전 구간의 늦은 메시지(큰 fCnt, 이른 시각)가 상태를 끌어올리지 않는다
    s = DeviceState(1, 5, T2)
    epoch, st, bumped = decide(s, 212000, T0, **KW)
    assert (epoch, st, bumped) == (1, s, False)
