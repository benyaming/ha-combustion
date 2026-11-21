"""GATT layer: framing on a live session, command routing and target choice."""

import asyncio
from collections.abc import Callable
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.combustion_inc import connection
from custom_components.combustion_inc.connection import Session, Target
from custom_components.combustion_inc.hub import CombustionHub
from custom_components.combustion_inc.models import GaugeDevice, ProbeDevice
from custom_components.combustion_inc.protocol import ProbeStatus
from custom_components.combustion_inc.protocol.const import UART_RX_UUID, PredictionMode
from custom_components.combustion_inc.protocol.uart import (
    Framing,
    encode_node_request,
    encode_node_response,
    encode_probe_request,
    encode_probe_response,
)

from .conftest import DISPLAY_ADDRESS, GAUGE_ADDRESS, PROBE_ADDRESS, inject, refresh, state
from .protocol.test_status import GOLDEN_0X45_FRAME, GOLDEN_NOTIFICATION
from .test_init import GAUGE, probe_packet
from .test_models import node_status

type Reply = Callable[[bytes], bytes | None]


class FakeCharacteristic:
    def __init__(self, properties: list[str], max_without_response: int) -> None:
        self.properties = properties
        self.max_write_without_response_size = max_without_response


class FakeServices:
    def __init__(self, char: FakeCharacteristic) -> None:
        self._char = char

    def get_characteristic(self, uuid: str) -> FakeCharacteristic | None:
        return self._char if uuid == UART_RX_UUID else None


class FakeClient:
    """Stands in for a connected BleakClient; answers writes through `reply`."""

    def __init__(
        self,
        session: Session,
        reply: Reply,
        properties: tuple[str, ...] = ("write", "write-without-response"),
        max_without_response: int = 244,
    ) -> None:
        self.is_connected = True
        self.services = FakeServices(FakeCharacteristic(list(properties), max_without_response))
        self.writes: list[bytes] = []
        self.write_modes: list[bool | None] = []
        self.hold_writes: asyncio.Event | None = None
        self.disconnect_error: BaseException | None = None
        self._session = session
        self._reply = reply

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool | None = None) -> None:
        assert uuid == UART_RX_UUID
        if self.hold_writes is not None:
            await self.hold_writes.wait()  # a backend write that does not finish
        self.writes.append(bytes(data))
        self.write_modes.append(response)
        if (answer := self._reply(bytes(data))) is not None:
            asyncio.get_running_loop().call_soon(self._session._on_uart, None, bytearray(answer))

    async def disconnect(self) -> None:
        self.is_connected = False
        if self.disconnect_error is not None:
            raise self.disconnect_error


def ack(success: bool = True) -> Reply:
    def reply(frame: bytes) -> bytes:
        request_id = int.from_bytes(frame[5:9], "little")
        return encode_node_response(frame[4], request_id, 0xCAFEBABE, success)

    return reply


def attach(hub: CombustionHub, target: Target, reply: Reply) -> FakeClient:
    assert hub.connections is not None
    session = Session(hub, target)
    client = FakeClient(session, reply)
    session.client = client  # type: ignore[assignment]
    hub.connections.sessions[target.address] = session
    return client


async def gauge(hass: HomeAssistant, hub: CombustionHub) -> GaugeDevice:
    inject(hass, GAUGE_ADDRESS, GAUGE)
    await hass.async_block_till_done()
    await refresh(hass, hub)
    device = hub.devices["gauge_AB12CD34EF"]
    assert isinstance(device, GaugeDevice)
    return device


async def test_node_session_reassembles_status(hass: HomeAssistant, hub: CombustionHub) -> None:
    session = Session(hub, Target(GAUGE_ADDRESS, Framing.NODE))
    for i in range(0, len(GOLDEN_0X45_FRAME), 20):
        session._on_uart(None, bytearray(GOLDEN_0X45_FRAME[i : i + 20]))  # type: ignore[arg-type]
    probe = hub.devices["probe_12345678"]
    assert isinstance(probe, ProbeDevice)
    assert probe.prediction is not None
    assert probe.prediction.seconds == 3600
    assert probe.hop_count == 1

    gauge_status = b"GGG198A54\x00" + bytes(24)
    session._on_uart(None, bytearray(encode_node_request(0x60, 7, gauge_status)))  # type: ignore[arg-type]
    assert "gauge_GGG198A54" in hub.devices


async def test_gauge_alarm_command(hass: HomeAssistant, hub: CombustionHub) -> None:
    device = await gauge(hass, hub)
    client = attach(hub, Target(GAUGE_ADDRESS, Framing.NODE), ack())
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.combustion_gauge_ab12cd34ef_low_alarm_temperature", "value": 90},
        blocking=True,
    )
    assert len(client.writes) == 1
    frame = client.writes[0]
    assert frame[4] == 0x61
    payload = frame[10:]
    assert payload[:10] == b"AB12CD34EF"
    # High alarm keeps what the gauge reported; low becomes set at 90 °C.
    assert payload[10:12] == bytes.fromhex("E347")
    assert int.from_bytes(payload[12:14], "little") == (1100 << 3) | 1
    assert device.low_alarm is not None and device.low_alarm.set
    await refresh(hass, hub)
    assert state(hass, "number.combustion_gauge_ab12cd34ef_low_alarm_temperature") == "90.0"
    assert state(hass, "switch.combustion_gauge_ab12cd34ef_low_alarm") == "on"

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.combustion_gauge_ab12cd34ef_high_alarm"},
        blocking=True,
    )
    high = int.from_bytes(client.writes[1][20:22], "little")
    assert high & 1 == 0
    assert (high >> 3) == 2300  # temperature kept: 210 °C


async def test_command_rejected(hass: HomeAssistant, hub: CombustionHub) -> None:
    device = await gauge(hass, hub)
    attach(hub, Target(GAUGE_ADDRESS, Framing.NODE), ack(success=False))
    with pytest.raises(HomeAssistantError) as err:
        await hub.async_set_gauge_alarm(device, high=True, temperature=300)
    assert err.value.translation_key == "command_rejected"
    assert device.high_alarm is not None and device.high_alarm.temperature == 210.0


async def test_command_timeout_retries(hass: HomeAssistant, hub: CombustionHub) -> None:
    device = await gauge(hass, hub)
    client = attach(hub, Target(GAUGE_ADDRESS, Framing.NODE), lambda _frame: None)
    with (
        patch.object(connection, "RESPONSE_TIMEOUT", 0.01),
        pytest.raises(HomeAssistantError) as err,
    ):
        await hub.async_set_gauge_alarm(device, high=True, temperature=300)
    assert err.value.translation_key == "command_timeout"
    assert len(client.writes) == connection.COMMAND_ATTEMPTS
    # Each attempt is a fresh request ID.
    assert len({w[5:9] for w in client.writes}) == connection.COMMAND_ATTEMPTS


async def test_silence_tolerates_missing_ack(hass: HomeAssistant, hub: CombustionHub) -> None:
    await gauge(hass, hub)
    client = attach(hub, Target(GAUGE_ADDRESS, Framing.NODE), lambda _frame: None)
    with patch.object(connection, "RESPONSE_TIMEOUT", 0.01):
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.combustion_gauge_ab12cd34ef_silence_alarms"},
            blocking=True,
        )
    assert client.writes[0][4] == 0x0C
    assert client.writes[0][10:] == bytes([1, 0]) + bytes(14)


async def test_no_connection(hass: HomeAssistant, hub: CombustionHub) -> None:
    device = await gauge(hass, hub)
    with pytest.raises(HomeAssistantError) as err:
        await hub.async_set_gauge_alarm(device, high=True, temperature=300)
    assert err.value.translation_key == "not_connected"


async def test_direct_probe_session(hass: HomeAssistant, hub: CombustionHub) -> None:
    serial = 0x12345678
    target = Target(PROBE_ADDRESS, Framing.PROBE, serial)
    client = attach(hub, target, lambda frame: encode_probe_response(frame[4], True))
    session = hub.connections.sessions[PROBE_ADDRESS]  # type: ignore[union-attr]
    session._on_probe_status(None, bytearray(GOLDEN_NOTIFICATION))  # type: ignore[arg-type]
    probe = hub.devices["probe_12345678"]
    assert isinstance(probe, ProbeDevice)
    assert probe.hop_count == 0
    assert probe.prediction is not None

    await hub.async_set_prediction(probe, PredictionMode.TIME_TO_REMOVAL, 57.0)
    expected = encode_probe_request(0x05, (570 | 1 << 10).to_bytes(2, "little"))
    assert client.writes == [expected]


async def test_target_prefers_a_node(hass: HomeAssistant, hub: CombustionHub) -> None:
    inject(hass, GAUGE_ADDRESS, GAUGE, rssi=-90)
    inject(hass, PROBE_ADDRESS, probe_packet(1), rssi=-40)
    await hass.async_block_till_done()
    manager = hub.connections
    assert manager is not None
    with patch.object(connection.bluetooth, "async_ble_device_from_address", return_value=object()):
        targets = manager._desired_targets()
    assert list(targets) == [GAUGE_ADDRESS]
    assert targets[GAUGE_ADDRESS].framing is Framing.NODE

    hub.devices.pop("gauge_AB12CD34EF")
    with patch.object(connection.bluetooth, "async_ble_device_from_address", return_value=object()):
        targets = manager._desired_targets()
    assert targets == {PROBE_ADDRESS: Target(PROBE_ADDRESS, Framing.PROBE, 0x12345678)}


DISPLAY = bytes([4]) + b"DSP1234567" + bytes([1]) + bytes(11)
CONNECTABLE = patch.object(
    connection.bluetooth, "async_ble_device_from_address", return_value=object()
)


async def test_write_and_wait_share_one_deadline(hass: HomeAssistant, hub: CombustionHub) -> None:
    await gauge(hass, hub)
    client = attach(hub, Target(GAUGE_ADDRESS, Framing.NODE), ack())
    client.hold_writes = asyncio.Event()  # the backend never finishes the write
    session = hub.connections.sessions[GAUGE_ADDRESS]  # type: ignore[union-attr]
    with pytest.raises(TimeoutError):
        await session.request(0x61, b"x", wait=0.02)
    assert not session._pending


async def test_cancelled_direct_command_frees_its_slot(
    hass: HomeAssistant, hub: CombustionHub
) -> None:
    target = Target(PROBE_ADDRESS, Framing.PROBE, 0x12345678)
    client = attach(hub, target, lambda frame: encode_probe_response(frame[4], True))
    client.hold_writes = asyncio.Event()
    session = hub.connections.sessions[PROBE_ADDRESS]  # type: ignore[union-attr]
    task = asyncio.create_task(session.request(0x05, b"\x00\x00"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not session._pending
    client.hold_writes = None
    assert (await session.request(0x05, b"\x00\x00")).success


async def test_failed_teardown_does_not_end_reconnecting(
    hass: HomeAssistant, hub: CombustionHub
) -> None:
    session = Session(hub, Target(GAUGE_ADDRESS, Framing.NODE))
    attempts = 0

    async def connect_then_drop() -> None:
        nonlocal attempts
        attempts += 1
        client = FakeClient(session, ack())
        client.disconnect_error = TimeoutError()  # BlueZ gave up on the disconnect
        session.client = client  # type: ignore[assignment]

    with (
        patch.object(session, "_connect_and_wait", connect_then_drop),
        patch.object(connection, "RETRY_BACKOFF", (0.001,)),
    ):
        session._task = asyncio.create_task(session._run())
        await asyncio.sleep(0.05)
        assert not session.finished
        await session.stop()
    assert attempts > 1


async def test_failing_node_gives_way_and_probes_fall_back(
    hass: HomeAssistant, hub: CombustionHub
) -> None:
    inject(hass, GAUGE_ADDRESS, GAUGE, rssi=-30)
    inject(hass, DISPLAY_ADDRESS, DISPLAY, rssi=-60)
    inject(hass, PROBE_ADDRESS, probe_packet(1), rssi=-20)
    await hass.async_block_till_done()
    manager = hub.connections
    assert manager is not None

    failing = Session(hub, Target(GAUGE_ADDRESS, Framing.NODE))
    failing.failures = connection.GIVE_UP_FAILURES
    manager.sessions = {GAUGE_ADDRESS: failing}
    with CONNECTABLE, patch.object(Session, "start"):
        assert list(manager._desired_targets()) == [DISPLAY_ADDRESS]
        manager._async_evaluate()
    assert list(manager.sessions) == [DISPLAY_ADDRESS]
    assert manager._cooldown[GAUGE_ADDRESS] > hub.now()

    # A fresh node connection gets time to report its probes...
    working = manager.sessions[DISPLAY_ADDRESS]
    working.client = FakeClient(working, ack())  # type: ignore[assignment]
    working.connected_at = hub.now()
    with CONNECTABLE:
        assert list(manager._desired_targets()) == [DISPLAY_ADDRESS]
    # ...then a probe it does not deliver is connected directly...
    hub.now.now += connection.ROUTE_SETTLE + 1  # type: ignore[attr-defined]
    with CONNECTABLE:
        assert PROBE_ADDRESS in manager._desired_targets()
    # ...until MeatNet delivers it.
    status = ProbeStatus.decode_node_payload(GOLDEN_0X45_FRAME[10:])
    assert status is not None
    hub.handle_probe_status(status, 0x12345678, via_node=True)
    with CONNECTABLE:
        assert list(manager._desired_targets()) == [DISPLAY_ADDRESS]


async def test_parallel_gauge_alarm_edits_both_stick(
    hass: HomeAssistant, hub: CombustionHub
) -> None:
    device = await gauge(hass, hub)
    client = attach(hub, Target(GAUGE_ADDRESS, Framing.NODE), ack())
    await asyncio.gather(
        hub.async_set_gauge_alarm(device, high=True, temperature=300),
        hub.async_set_gauge_alarm(device, high=False, enabled=False),
    )
    assert device.high_alarm is not None and device.low_alarm is not None
    assert device.high_alarm.set and device.high_alarm.temperature == 300
    assert not device.low_alarm.set
    last_high = int.from_bytes(client.writes[-1][20:22], "little")
    assert last_high >> 3 == 3200 and last_high & 1


async def test_silence_raises_on_explicit_rejection(
    hass: HomeAssistant, hub: CombustionHub
) -> None:
    await gauge(hass, hub)
    attach(hub, Target(GAUGE_ADDRESS, Framing.NODE), ack(success=False))
    with pytest.raises(HomeAssistantError) as err:
        await hub.async_silence_alarms()
    assert err.value.translation_key == "command_rejected"


async def test_write_mode_follows_the_characteristic(
    hass: HomeAssistant, hub: CombustionHub
) -> None:
    device = await gauge(hass, hub)
    client = attach(hub, Target(GAUGE_ADDRESS, Framing.NODE), ack())
    await hub.async_set_gauge_alarm(device, high=True, temperature=250)
    client.services = FakeServices(
        FakeCharacteristic(["write", "write-without-response"], max_without_response=20)
    )
    await hub.async_set_gauge_alarm(device, high=True, temperature=260)
    client.services = FakeServices(FakeCharacteristic(["write"], max_without_response=244))
    await hub.async_set_gauge_alarm(device, high=True, temperature=270)
    # Fits the link: without response, like the official apps. Too long, or
    # without-response not offered: with response.
    assert client.write_modes == [False, True, True]
    # Too long, but a plain write is not offered: never pick an unsupported operation.
    client.services = FakeServices(
        FakeCharacteristic(["write-without-response"], max_without_response=20)
    )
    await hub.async_set_gauge_alarm(device, high=True, temperature=280)
    assert client.write_modes[-1] is False


async def test_stale_node_status_is_not_a_route(hass: HomeAssistant, hub: CombustionHub) -> None:
    hub.handle_probe_status(node_status(111, 100, 80.0, hops=None), 0x12345678)
    hub.handle_probe_status(node_status(222, 1, 20.0, hops=None), 0x12345678)
    probe = hub.devices["probe_12345678"]
    assert isinstance(probe, ProbeDevice)
    hub.handle_probe_status(node_status(111, 101, 81.0), 0x12345678, via_node=True)
    assert probe.last_node_status is None
    hub.handle_probe_status(node_status(222, 2, 21.0), 0x12345678, via_node=True)
    assert probe.last_node_status == hub.now()


async def test_short_connections_count_as_failures(hass: HomeAssistant, hub: CombustionHub) -> None:
    session = Session(hub, Target(GAUGE_ADDRESS, Framing.NODE))

    class Device:
        name = "Gauge"

    class FlakyClient(FakeClient):
        async def start_notify(self, *_args: object) -> None:
            # The node drops the link right after it was made.
            asyncio.get_running_loop().call_soon(session._on_disconnect, self)

    async def connect(*_args: object, **_kwargs: object) -> FakeClient:
        return FlakyClient(session, ack())

    with (
        patch.object(connection.bluetooth, "async_ble_device_from_address", return_value=Device()),
        patch.object(connection, "establish_connection", connect),
        patch.object(connection, "RETRY_BACKOFF", (0.001,)),
    ):
        session._task = asyncio.create_task(session._run())
        await asyncio.sleep(0.05)
        await session.stop()
    assert session.failures >= connection.GIVE_UP_FAILURES


class Device:
    name = "Gauge"


class NotifyingClient(FakeClient):
    async def start_notify(self, *_args: object) -> None:
        pass


async def run_session(session: Session, client: FakeClient) -> None:
    """Run the session for a moment against a backend that reports failed tries."""

    async def connect(
        *_args: object, disconnected_callback: Callable[[object], None], **_kwargs: object
    ) -> FakeClient:
        for _ in range(3):  # BlueZ, on every failed try before the link holds
            disconnected_callback(client)
        return client

    with (
        patch.object(connection.bluetooth, "async_ble_device_from_address", return_value=Device()),
        patch.object(connection, "establish_connection", connect),
        patch.object(connection, "RETRY_BACKOFF", (0.001,)),
    ):
        session._task = asyncio.create_task(session._run())
        await asyncio.sleep(0.05)


async def test_disconnects_reported_while_connecting_are_ignored(
    hass: HomeAssistant, hub: CombustionHub
) -> None:
    # Seen on a real gauge: the session gave up 1 ms after every successful connect.
    session = Session(hub, Target(GAUGE_ADDRESS, Framing.NODE))
    await run_session(session, NotifyingClient(session, ack()))
    assert session.connected
    assert session.failures == 0
    await session.stop()


async def test_link_lost_during_setup_is_a_failure(hass: HomeAssistant, hub: CombustionHub) -> None:
    session = Session(hub, Target(GAUGE_ADDRESS, Framing.NODE))
    client = NotifyingClient(session, ack())
    client.is_connected = False  # dropped before the clear, so no callback is left
    await run_session(session, client)
    assert not session.connected
    assert session.failures > 0
    await session.stop()


async def test_node_grace_runs_from_session_start_and_keeps_direct_routes(
    hass: HomeAssistant, hub: CombustionHub
) -> None:
    inject(hass, DISPLAY_ADDRESS, DISPLAY, rssi=-60)
    inject(hass, PROBE_ADDRESS, probe_packet(1), rssi=-20)
    await hass.async_block_till_done()
    manager = hub.connections
    assert manager is not None
    node = Session(hub, Target(DISPLAY_ADDRESS, Framing.NODE))  # created now, not connected
    direct = Session(hub, Target(PROBE_ADDRESS, Framing.PROBE, 0x12345678))
    manager.sessions = {DISPLAY_ADDRESS: node, PROBE_ADDRESS: direct}
    with CONNECTABLE:
        # Inside the grace period a working direct route is kept...
        assert set(manager._desired_targets()) == {DISPLAY_ADDRESS, PROBE_ADDRESS}
        manager.sessions = {DISPLAY_ADDRESS: node}
        # ...but no new one is opened.
        assert set(manager._desired_targets()) == {DISPLAY_ADDRESS}
        # The grace runs from when the session began, however often it reconnects.
        hub.now.now += connection.ROUTE_SETTLE + 1  # type: ignore[attr-defined]
        assert set(manager._desired_targets()) == {DISPLAY_ADDRESS, PROBE_ADDRESS}


async def test_silence_fails_when_nothing_was_written(
    hass: HomeAssistant, hub: CombustionHub
) -> None:
    await gauge(hass, hub)
    client = attach(hub, Target(GAUGE_ADDRESS, Framing.NODE), lambda _frame: None)
    client.hold_writes = asyncio.Event()  # the write never completes
    with patch.object(connection, "RESPONSE_TIMEOUT", 0.01), pytest.raises(HomeAssistantError):
        await hub.async_silence_alarms()
    assert client.writes == []


async def test_stop_waits_for_a_teardown_in_progress(
    hass: HomeAssistant, hub: CombustionHub
) -> None:
    session = Session(hub, Target(GAUGE_ADDRESS, Framing.NODE))
    release = asyncio.Event()
    closed = False

    class SlowClient(FakeClient):
        async def disconnect(self) -> None:
            nonlocal closed
            await release.wait()
            closed = True

    session.client = SlowClient(session, ack())  # type: ignore[assignment]
    first = asyncio.create_task(session._disconnect())  # e.g. _run's own cleanup
    await asyncio.sleep(0)
    first.cancel()  # whoever was waiting goes away mid-teardown
    stopping = asyncio.create_task(session.stop())
    await asyncio.sleep(0.01)
    assert not stopping.done()  # still owns the client until it is closed
    release.set()
    await stopping
    assert closed
