from app.services.capacity import planning_current, spike_indexes, without_spikes

# MJO-MLR-OLT1-FH SFP+ 8/1 <> MJO-MGR-ME1-6120HS xxvgei-1/1/0/15, a 10G member of smartgroup84: daily peaks around
# 4 G, then a counter glitch read as 15 G on one day (both members of the 2×10G bundle the same day).
GLITCH = [4.0, 4.2, 3.7, 3.9, 4.2, 4.4, 4.0, 4.1, 4.2, 4.0, 3.9, 4.4, 4.2, 4.4, 4.2, 4.3, 4.0, 4.6, 15.07, 2.5]


def days(peaks: list[float], first_day: float = 20_000) -> list[tuple[float, float]]:
    return [(first_day + k, peak) for k, peak in enumerate(peaks)]


def test_should_flag_one_day_peak_above_capacity_when_surrounding_days_stay_low():
    assert spike_indexes(days(GLITCH), 10) == {18}


def test_should_keep_step_up_when_the_next_day_stays_high():
    step_up = [4.0] * 14 + [9.0, 9.2, 9.1]
    assert spike_indexes(days(step_up), 10) == set()


def test_should_keep_burst_smaller_than_the_minimum_rise_of_the_capacity():
    burst = [0.2] * 10 + [0.8] + [0.2] * 5  # 4× the usual, but only 6% of a 10G link
    assert spike_indexes(days(burst), 10) == set()


def test_should_flag_nothing_when_capacity_is_unknown():
    assert spike_indexes(days(GLITCH), None) == set()


def test_should_leave_spike_out_of_the_kept_series():
    kept, spikes = without_spikes(days(GLITCH), 10)
    assert spikes == [(20_018, 15.07)] and len(kept) == len(GLITCH) - 1


def test_should_take_current_from_the_day_before_when_newest_complete_day_is_a_spike():
    series = days(GLITCH[:-1])  # the 15 G day is the newest complete one, today has no sample yet
    assert planning_current(series, 10, today=20_019.5) == 4.6
