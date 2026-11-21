"""Merging device state from several paths: ordering, sessions, direct preference."""

from dataclasses import replace

from custom_components.combustion_inc.models import (
    EngineDevice,
    GaugeDevice,
    Outcome,
    ProbeDevice,
)
from custom_components.combustion_inc.protocol import (
    EngineStatus,
    GaugeStatus,
    ProbeMode,
    ProbeStatus,
    parse_advertisement,
)
from custom_components.combustion_inc.protocol.const import (
    EngineControllerState,
    EngineFanState,
    PredictionState,
)
from custom_components.combustion_inc.protocol.fields import ModeIdColor
from custom_components.combustion_inc.sensor import SENSORS

from .protocol.test_status import ENGINE_STATUS, GOLDEN_0X45_FRAME, GOLDEN_NOTIFICATION
from .test_init import probe_packet

NORMAL = ModeIdColor(ProbeMode.NORMAL, 0, 0)
APPLIED, CURRENT, REJECTED = Outcome.APPLIED, Outcome.CURRENT, Outcome.REJECTED


def node_status(session: int | None, sequence: int, t1: float, hops: int | None = 1) -> ProbeStatus:
    """A Normal-mode status with a 5 s sample period; hops=None means direct."""
    status = ProbeStatus.decode_node_payload(GOLDEN_0X45_FRAME[10:])
    assert status is not None
    return replace(
        status,
        mode_id_color=NORMAL,
        session_id=session,
        max_sequence=sequence,
        temperatures=(t1, *status.temperatures[1:]),
        hop_count=hops,
    )


def t1(probe: ProbeDevice) -> float | None:
    return probe.temperatures[0] if probe.temperatures else None


def test_late_status_cannot_bring_back_an_old_cook() -> None:
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    assert probe.apply_status(node_status(111, 100, 80.0), 1000.0) is APPLIED
    assert probe.apply_status(node_status(222, 1, 20.0), 1001.0) is APPLIED
    assert probe.apply_status(node_status(111, 101, 81.0), 1002.0) is REJECTED
    assert t1(probe) == 20.0
    # A repeat of the current record changes nothing; an older one is stale.
    assert probe.apply_status(node_status(222, 1, 21.0), 1003.0) is CURRENT
    assert probe.apply_status(node_status(222, 0, 21.0), 1003.0) is REJECTED
    # A status without session data counts as part of the current cook: a newer
    # record applies, an older one is stale, and the cook is not forgotten.
    assert probe.apply_status(node_status(None, 2, 22.0), 1004.0) is APPLIED
    assert probe.apply_status(node_status(None, 1, 22.0), 1004.5) is REJECTED
    assert probe.status_order.session == 222
    assert probe.apply_status(node_status(111, 500, 81.0), 1006.0) is REJECTED


def test_sessionless_status_of_an_older_cook_is_refused() -> None:
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    status = node_status(222, 1, 70.0, hops=None)
    assert probe.apply_status(status, 1000.0) is APPLIED
    # No session data, but record 100 at a 5 s period began ~8 minutes before 222.
    old = replace(node_status(None, 100, 30.0), sample_period_ms=None)
    assert probe.apply_status(old, 1000.1) is REJECTED
    assert probe.apply_status(old, 1006.0) is REJECTED
    assert t1(probe) == 70.0
    assert probe.connection_record == (222, 1)


def test_records_starting_over_without_session_data_recover() -> None:
    # The probe restarts while its node relays it without session data: the new
    # cook's records start again from 1. A lower record is a late packet at first,
    # but a steady stream of them for three sample periods is a new start.
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    assert probe.apply_status(node_status(111, 720, 80.0), 4000.0) is APPLIED
    outcomes = []
    for i in range(1, 8):
        status = replace(node_status(None, i, 20.0), sample_period_ms=None)
        status = replace(status, prediction=replace(status.prediction, seconds=1000 + i))
        outcomes.append(probe.apply_status(status, 4000.0 + 5 * i))
    assert outcomes[0] is REJECTED
    assert APPLIED in outcomes
    assert t1(probe) == 20.0
    assert probe.prediction is not None and probe.prediction.seconds == 1007
    assert probe.status_order.sequence == 7
    # A delayed packet of the old cook, which it still names, is now older.
    assert probe.apply_status(node_status(111, 721, 61.0), 4036.0) is REJECTED
    assert t1(probe) == 20.0
    nxt = replace(node_status(None, 8, 21.0), sample_period_ms=None)
    assert probe.apply_status(nxt, 4040.0) is APPLIED
    # When the new cook's ID shows up, it simply takes over.
    assert probe.apply_status(node_status(222, 9, 22.0), 4045.0) is APPLIED
    assert probe.status_order.session == 222


def test_one_late_low_record_does_not_restart_the_cook() -> None:
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    probe.apply_status(node_status(111, 720, 80.0), 4000.0)
    late = replace(node_status(None, 3, 20.0), sample_period_ms=None)
    assert probe.apply_status(late, 4001.0) is REJECTED
    for i in range(1, 6):
        assert probe.apply_status(node_status(111, 720 + i, 81.0), 4000.0 + 5 * i) is APPLIED
        assert probe.apply_status(late, 4001.0 + 5 * i) is REJECTED
    assert probe.status_order.sequence == 725


def test_sessionless_noise_does_not_keep_an_old_cook_alive() -> None:
    # Unattributable sessionless packets must not count as the cached old cook being
    # heard, so the live named stream still takes over.
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    probe.apply_status(node_status(111, 10, 30.0), 1000.0)
    noise = replace(node_status(None, 0, 20.0), sample_period_ms=None)
    for i in range(8):
        probe.apply_status(noise, 1000.9 + 5 * i)
        probe.apply_status(node_status(222, 50 + i, 80.0, hops=None), 1001.0 + 5 * i)
    assert probe.status_order.session == 222
    assert t1(probe) == 80.0


def test_direct_advertisements_do_not_block_the_node_naming_the_cook() -> None:
    # The usual setup: HA hears the probe's advertisements and holds a node link.
    # Advertisements carry no cook, so they keep the readings but cannot stop the
    # node from establishing the cook and its predictions.
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    for i in range(3):
        adv = parse_advertisement(probe_packet(1, t1_raw=1400 + i))
        assert adv is not None
        probe.apply_advertisement(adv, 1000.0 + 5 * i, "AA:BB:CC:00:00:01", -50)  # type: ignore[arg-type]
        outcome = probe.apply_status(node_status(222, 1 + i, 30.0), 1000.1 + 5 * i)
        assert outcome is APPLIED
    assert probe.status_order.session == 222
    assert probe.prediction is not None
    assert t1(probe) == round(1402 * 0.05 - 20, 2)  # readings stay with the advertisements
    assert probe.hop_count == 0
    # A later cook announced by the node takes over just the same.
    assert probe.apply_status(node_status(333, 1, 30.0), 4000.0) is APPLIED
    assert probe.status_order.session == 333


def test_first_seen_relay_of_an_earlier_cook_is_stale() -> None:
    # At startup HA sees the current cook first, then a delayed relay of an older
    # cook it never saw. That cook began ~75 minutes earlier, so it is refused and
    # the current cook is not retired.
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    assert probe.apply_status(node_status(222, 1, 70.0, hops=None), 1000.0) is APPLIED
    assert probe.apply_status(node_status(111, 900, 40.0), 1000.1) is REJECTED
    assert probe.apply_status(node_status(111, 900, 40.0), 1100.0) is REJECTED
    assert probe.apply_status(node_status(222, 2, 71.0, hops=None), 1005.0) is APPLIED
    assert probe.status_order.session == 222
    # A cook that really began later takes over.
    assert probe.apply_status(node_status(333, 1, 20.0), 4000.0) is APPLIED
    assert probe.status_order.session == 333


def test_a_live_cook_wins_over_a_misleading_first_estimate() -> None:
    # After HA starts, a node's cached status of a short earlier cook (111) arrives
    # first. The live cook (222) then looks older by its estimate, but its statuses
    # keep coming while 111 is silent: after three sample periods it is accepted.
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    assert probe.apply_status(node_status(111, 10, 30.0), 1000.0) is APPLIED
    outcomes = [
        probe.apply_status(node_status(222, 50 + i, 80.0, hops=None), 1001.0 + 5 * i)
        for i in range(5)
    ]
    assert outcomes[:3] == [REJECTED] * 3
    assert APPLIED in outcomes
    assert probe.status_order.session == 222
    assert t1(probe) == 80.0
    # An isolated late status is still refused.
    assert probe.apply_status(node_status(333, 5000, 1.0), 1030.0) is REJECTED


def test_session_start_keeps_the_earliest_estimate() -> None:
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    probe.apply_status(node_status(111, 10, 30.0), 1000.0)  # began ~950
    probe.apply_status(node_status(111, 70, 31.0), 1000.1)  # really began ~650
    # A cook that began at ~700 began after 111, so it is newer.
    assert probe.apply_status(node_status(222, 61, 80.0, hops=None), 1005.0) is APPLIED
    assert probe.status_order.session == 222


def test_another_cooks_relay_is_ignored_while_direct_data_is_fresh() -> None:
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    assert probe.apply_status(node_status(222, 1, 70.0, hops=None), 1000.0) is APPLIED
    # Unseen 111 began within one sample period of 222: not provably older, but set
    # aside while the direct path is fresh, and no evidence of a route either.
    assert probe.apply_status(node_status(111, 2, 30.0), 1000.1) is Outcome.IGNORED
    assert t1(probe) == 70.0


def test_connection_data_never_rolls_back() -> None:
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    probe.apply_status(node_status(1, 10, 70.0, hops=None), 1000.0)

    def with_seconds(sequence: int, seconds: int, hops: int | None = 1) -> ProbeStatus:
        status = node_status(1, sequence, 70.0, hops=hops)
        return replace(status, prediction=replace(status.prediction, seconds=seconds))

    assert probe.apply_status(with_seconds(12, 600), 1000.2) is APPLIED
    assert probe.apply_status(with_seconds(11, 1200), 1000.3) is CURRENT  # older: kept 600
    assert probe.prediction is not None and probe.prediction.seconds == 600
    # The direct reading of record 11 still applies, without rolling the prediction back.
    assert probe.apply_status(with_seconds(11, 1200, hops=None), 1001.0) is APPLIED
    assert probe.status_order.sequence == 11
    assert probe.prediction.seconds == 600


def test_relay_ignored_for_readings_does_not_consume_the_record() -> None:
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    assert probe.apply_status(node_status(1, 10, 70.0, hops=None), 1000.0) is APPLIED
    # The node relays record 11 before the direct link delivers it: connection-only
    # data is taken, the reading and the record number are left to the direct path.
    assert probe.apply_status(node_status(1, 11, 80.0), 1000.5) is APPLIED
    assert t1(probe) == 70.0
    assert probe.status_order.sequence == 10
    assert probe.apply_status(node_status(1, 11, 80.0, hops=None), 1001.0) is APPLIED
    assert t1(probe) == 80.0
    assert probe.hop_count == 0


def test_direct_gatt_status_beats_older_relayed_data() -> None:
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    direct = ProbeStatus.decode_characteristic(GOLDEN_NOTIFICATION)  # Instant Read, T1 -5.45
    assert direct is not None
    assert probe.apply_status(direct, 1000.0) is APPLIED
    assert probe.hop_count == 0
    relayed = parse_advertisement(probe_packet(2, t1_raw=1200, mode=1, network=0xC0))
    assert relayed is not None
    assert not probe.apply_advertisement(relayed, 1000.1, "AA:BB:CC:00:00:04", -70)  # type: ignore[arg-type]
    assert probe.instant_read == -5.45
    relayed_status = node_status(0xDEADBEEF, 0x05060709, 50.0, hops=4)
    probe.apply_status(relayed_status, 1000.2)
    assert probe.temperatures is None  # readings stayed with the direct path
    assert probe.hop_count == 0
    # Once the direct path is quiet, relayed readings are used again.
    probe.apply_status(replace(relayed_status, max_sequence=0x0506070A), 1010.0)
    assert t1(probe) == 50.0
    assert probe.hop_count == 4


def gauge_status(session: int, sequence: int, raw_temp: int) -> GaugeStatus:
    payload = bytearray(b"GGG198A54\x00" + bytes(25))
    payload[10:14] = session.to_bytes(4, "little")
    payload[16:18] = raw_temp.to_bytes(2, "little")
    payload[18] = 1  # sensor present
    payload[23:27] = sequence.to_bytes(4, "little")
    status = GaugeStatus.decode_node_payload(bytes(payload))
    assert status is not None
    return status


def test_gauge_status_ignores_older_records() -> None:
    gauge = GaugeDevice(serial="GGG198A54")
    assert gauge.apply_status(gauge_status(7, 100, 1700), 1000.0) is APPLIED
    assert gauge.apply_status(gauge_status(7, 99, 1200), 1001.0) is REJECTED
    assert gauge.temperature == 150.0
    # A gauge without a sensor stays at record 0; equal records still apply.
    assert gauge.apply_status(gauge_status(7, 100, 1710), 1002.0) is APPLIED
    assert gauge.temperature == 151.0
    assert gauge.last_seen == 1002.0


def test_engine_status_ignores_older_records() -> None:
    engine = EngineDevice(serial="ABCDEF1234")
    newer = EngineStatus.decode_node_payload(b"ABCDEF1234" + ENGINE_STATUS)
    assert newer is not None
    older = replace(newer, max_sequence=newer.max_sequence - 1, set_point=50.0)
    assert engine.apply_status(newer, 1000.0) is APPLIED
    assert engine.apply_status(older, 1001.0) is REJECTED
    assert engine.set_point == 100.0


def sensor(key: str):
    return next(d for d in SENSORS if d.key == key).value_fn


def test_zero_valued_states_are_reported() -> None:
    probe = ProbeDevice(serial="12345678", serial_number=0x12345678)
    status = node_status(1, 1, 20.0)
    probe.apply_status(
        replace(status, prediction=replace(status.prediction, state=PredictionState(0))), 1.0
    )
    assert sensor("prediction_state")(probe, 1.0) == "probe_not_inserted"

    engine = EngineDevice(serial="ABCDEF1234")
    decoded = EngineStatus.decode_node_payload(b"ABCDEF1234" + ENGINE_STATUS)
    assert decoded is not None
    engine.apply_status(
        replace(
            decoded,
            fan=replace(decoded.fan, state=EngineFanState.POWER_DOWN),
            controller=replace(decoded.controller, state=EngineControllerState.IDLE),
        ),
        1.0,
    )
    assert sensor("fan_state")(engine, 1.0) == "power_down"
    assert sensor("controller_state")(engine, 1.0) == "idle"
