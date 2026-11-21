"""Optional GATT connections for predictions, richer status and control.

One connection to any MeatNet node (Gauge, Display, Booster, Engine) is enough:
the node pushes Probe, Gauge and Engine status for the whole network and relays
commands to them. A probe the node does not deliver (no node in reach, or not on
its network) is connected directly instead. Advertisement data keeps flowing
whatever happens here.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, DeviceKind
from .protocol import AlarmStatus, EngineStatus, GaugeStatus, MessageType, ProbeStatus
from .protocol.const import (
    PROBE_STATUS_CHAR_UUID,
    UART_RX_UUID,
    UART_TX_UUID,
    PredictionMode,
)
from .protocol.uart import (
    Frame,
    FrameDecoder,
    Framing,
    encode_node_request,
    encode_probe_request,
    node_set_engine_set_point_payload,
    node_set_gauge_alarms_payload,
    node_set_prediction_payload,
    node_silence_all_alarms_payload,
    set_prediction_payload,
)

if TYPE_CHECKING:
    from .hub import CombustionHub
    from .models import CombustionDevice

_LOGGER = logging.getLogger(__name__)

EVALUATE_INTERVAL = 15.0
MAX_PROBE_SESSIONS = 3
RETRY_BACKOFF = (10.0, 30.0, 60.0, 120.0, 300.0)
RESPONSE_TIMEOUT = 5.0
DISCONNECT_TIMEOUT = 10.0
COMMAND_ATTEMPTS = 3
# After this many failed connection attempts in a row, try another device for a while.
# A connection that drops sooner than STABLE_CONNECTION counts as a failed attempt.
GIVE_UP_FAILURES = 3
STABLE_CONNECTION = 30.0
COOLDOWN = 300.0
# A node session gets this long to start delivering probes; a probe it does not deliver
# within ROUTE_TIMEOUT is connected directly (the official app does the same).
ROUTE_SETTLE = 30.0
ROUTE_TIMEOUT = 30.0
NODE_KINDS = (DeviceKind.GAUGE, DeviceKind.DISPLAY, DeviceKind.BOOSTER, DeviceKind.ENGINE)


class AckTimeout(TimeoutError):
    """The request was written, but no response came back in time."""


@dataclass(frozen=True, slots=True)
class Target:
    address: str
    framing: Framing
    probe_serial: int | None = None  # set for a direct probe connection


def _write_with_response(client: BleakClient, frame: bytes) -> bool:
    """Write without response, as the official apps do, unless only a write fits.

    A frame longer than one unacknowledged packet goes as a write with response,
    but only if the characteristic offers that; otherwise the backend reports the
    problem rather than us using an operation the device does not support.
    """
    try:
        char = client.services.get_characteristic(UART_RX_UUID)
    except BleakError:
        char = None
    if char is None:
        return False
    props = char.properties
    fits = "write-without-response" in props and len(frame) <= char.max_write_without_response_size
    return not fits and "write" in props


class Session:
    """A persistent connection to one device, reconnecting with backoff."""

    def __init__(self, hub: CombustionHub, target: Target) -> None:
        self.hub = hub
        self.target = target
        self.client: BleakClient | None = None
        self._decoder = FrameDecoder(target.framing)
        self._pending: dict[tuple[int, int | None], asyncio.Future[Frame]] = {}
        self._disconnected = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._teardown: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self.failures = 0
        self.created_at = hub.now()
        self.connected_at: float | None = None

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    @property
    def finished(self) -> bool:
        return self._task is not None and self._task.done()

    def start(self) -> None:
        self._task = self.hub.entry.async_create_background_task(
            self.hub.hass, self._run(), f"{DOMAIN} connection {self.target.address}"
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._disconnect()

    async def _run(self) -> None:
        while True:
            try:
                await self._connect_and_wait()
                self.failures = 0
            except (BleakError, TimeoutError, OSError) as err:
                self.failures += 1
                _LOGGER.debug("Connection to %s failed: %s", self.target.address, err)
            except Exception:
                self.failures += 1
                _LOGGER.exception("Unexpected error on connection to %s", self.target.address)
            finally:
                await self._disconnect()  # raises nothing but cancellation
            await asyncio.sleep(RETRY_BACKOFF[min(self.failures, len(RETRY_BACKOFF) - 1)])

    async def _connect_and_wait(self) -> None:
        address = self.target.address
        ble_device = bluetooth.async_ble_device_from_address(
            self.hub.hass, address, connectable=True
        )
        if ble_device is None:
            raise BleakError("no connectable path to the device right now")
        self.client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            ble_device.name or address,
            disconnected_callback=self._on_disconnect,
            max_attempts=2,
            ble_device_callback=lambda: (
                bluetooth.async_ble_device_from_address(self.hub.hass, address, connectable=True)
                or ble_device
            ),
        )
        # BlueZ reports every failed try through the disconnect callback while
        # establish_connection is still at work; only drops from here on count.
        # One that came just before is caught by the is_connected check below.
        self._disconnected.clear()
        await self.client.start_notify(UART_TX_UUID, self._on_uart)
        if self.target.probe_serial is not None:
            await self.client.start_notify(PROBE_STATUS_CHAR_UUID, self._on_probe_status)
        if not self.client.is_connected:
            raise BleakError("the connection was lost while it was being set up")
        _LOGGER.debug("Connected to %s (%s)", address, self.target.framing.value)
        connected_at = self.connected_at = self.hub.now()
        self.hub.set_connected(address, True, self.target.probe_serial)
        await self._disconnected.wait()
        if self.hub.now() - connected_at < STABLE_CONNECTION:
            raise BleakError("the connection dropped right after it was made")

    async def _disconnect(self) -> None:
        """Tear down; bounded, and never raises anything but cancellation.

        The disconnect itself runs as a task the session owns: cancelling whoever
        waits on it does not abandon the client, and stop() waits for it.
        """
        client, self.client = self.client, None
        self.connected_at = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HomeAssistantError("Connection lost"))
        self._pending.clear()
        self._decoder = FrameDecoder(self.target.framing)
        if client is not None:
            self.hub.set_connected(self.target.address, False, self.target.probe_serial)
            self._teardown = self.hub.hass.async_create_background_task(
                self._close(client), f"{DOMAIN} disconnect {self.target.address}"
            )
        if self._teardown is not None:
            await asyncio.shield(self._teardown)

    async def _close(self, client: BleakClient) -> None:
        try:
            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                await client.disconnect()
        except Exception as err:
            _LOGGER.debug("Disconnecting from %s failed: %s", self.target.address, err)

    def _on_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("Disconnected from %s", self.target.address)
        self._disconnected.set()

    @callback
    def _on_probe_status(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        if (status := ProbeStatus.decode_characteristic(bytes(data))) is not None:
            assert self.target.probe_serial is not None
            self.hub.handle_probe_status(status, self.target.probe_serial)

    @callback
    def _on_uart(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        for frame in self._decoder.feed(bytes(data)):
            if frame.is_response:
                key = (frame.message_type, frame.request_id)
                if (future := self._pending.pop(key, None)) is not None and not future.done():
                    future.set_result(frame)
                continue
            try:
                self._dispatch_request(frame)
            except Exception:
                _LOGGER.exception("Failed to handle message 0x%02x", frame.message_type)

    def _dispatch_request(self, frame: Frame) -> None:
        match frame.message_type:
            case MessageType.PROBE_STATUS:
                status = ProbeStatus.decode_node_payload(frame.payload)
                if status is not None and status.serial:
                    self.hub.handle_probe_status(status, status.serial, via_node=True)
            case MessageType.GAUGE_STATUS:
                if (gauge := GaugeStatus.decode_node_payload(frame.payload)) is not None:
                    self.hub.handle_gauge_status(gauge)
            case MessageType.ENGINE_STATUS:
                if (engine := EngineStatus.decode_node_payload(frame.payload)) is not None:
                    self.hub.handle_engine_status(engine)

    async def request(self, message_type: int, payload: bytes, wait: float | None = None) -> Frame:
        """Send one request and wait for its response, all within one deadline."""
        if self.target.framing is Framing.NODE:
            request_id: int | None = random.randint(1, 0xFFFFFFFF)
            frame = encode_node_request(message_type, request_id, payload)
        else:
            request_id = None  # direct probe responses carry no ID: one per type at a time
            frame = encode_probe_request(message_type, payload)
        key = (message_type, request_id)
        if key in self._pending:
            raise HomeAssistantError("Another command of this kind is still running")
        future: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        written = False
        try:
            async with asyncio.timeout(wait or RESPONSE_TIMEOUT):
                async with self._write_lock:
                    client = self.client
                    if client is None or not client.is_connected:
                        raise HomeAssistantError("Not connected")
                    await client.write_gatt_char(
                        UART_RX_UUID, frame, response=_write_with_response(client, frame)
                    )
                written = True
                return await future
        except TimeoutError as err:
            if written:
                raise AckTimeout from err
            raise
        except BleakError as err:
            raise HomeAssistantError(f"Could not send the command: {err}") from err
        finally:
            if self._pending.get(key) is future:
                del self._pending[key]


class ConnectionManager:
    """Picks which devices to hold connections to, and routes commands."""

    def __init__(self, hub: CombustionHub) -> None:
        self.hub = hub
        self.sessions: dict[str, Session] = {}
        self._retiring: set[asyncio.Task[None]] = set()
        self._cooldown: dict[str, float] = {}
        self._unsub: Callable[[], None] | None = None

    @callback
    def async_start(self) -> None:
        self._unsub = async_track_time_interval(
            self.hub.hass, self._async_evaluate, timedelta(seconds=EVALUATE_INTERVAL)
        )
        self._async_evaluate()

    async def async_stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
        sessions, self.sessions = self.sessions, {}
        await asyncio.gather(*(session.stop() for session in sessions.values()), *self._retiring)

    def _connectable(self, address: str | None) -> bool:
        return address is not None and (
            bluetooth.async_ble_device_from_address(self.hub.hass, address, connectable=True)
            is not None
        )

    def _cooling(self, address: str, now: float) -> bool:
        session = self.sessions.get(address)
        if session is not None and (session.finished or session.failures >= GIVE_UP_FAILURES):
            return True
        return self._cooldown.get(address, 0.0) > now

    def _node_target(self, now: float) -> Session | Target | None:
        """The node session to keep, or the best node to try next."""
        for session in self.sessions.values():
            if session.target.framing is Framing.NODE and not self._cooling(
                session.target.address, now
            ):
                return session
        candidates: list[CombustionDevice] = [
            device
            for device in self.hub.devices.values()
            if device.kind in NODE_KINDS
            and device.address is not None
            and not self._cooling(device.address, now)
            and self._connectable(device.address)
        ]
        if not candidates:
            return None
        best = max(candidates, key=lambda d: d.rssi or -127)
        assert best.address is not None
        return Target(best.address, Framing.NODE)

    def _desired_targets(self) -> dict[str, Target]:
        now = self.hub.now()
        targets: dict[str, Target] = {}
        node = self._node_target(now)
        if isinstance(node, Session):
            targets[node.target.address] = node.target
            # A new node session gets time to report its probes before any new direct
            # connection; reconnects within the session do not restart that clock.
            grace_over = now - node.created_at >= ROUTE_SETTLE
        elif node is not None:
            targets[node.address] = node
            grace_over = False
        else:
            grace_over = True
        probe_sessions = 0
        probes = sorted(self.hub.index.probes.values(), key=lambda p: -(p.rssi or -127))
        for probe in probes:
            if probe_sessions >= MAX_PROBE_SESSIONS:
                break
            address = probe.address  # only set once the probe was heard directly
            if address is None or self._cooling(address, now):
                continue
            if probe.last_node_status is not None and now - probe.last_node_status < ROUTE_TIMEOUT:
                continue  # MeatNet delivers this probe
            # Keep a working direct route until a node proves it delivers the probe.
            existing = address in self.sessions
            if existing or (grace_over and self._connectable(address)):
                targets[address] = Target(address, Framing.PROBE, probe.serial_number)
                probe_sessions += 1
        return targets

    @callback
    def _async_evaluate(self, _now: object = None) -> None:
        now = self.hub.now()
        desired = self._desired_targets()
        for address, session in list(self.sessions.items()):
            if address in desired and not session.finished:
                continue
            if session.finished or session.failures >= GIVE_UP_FAILURES:
                self._cooldown[address] = now + COOLDOWN
            del self.sessions[address]
            task = self.hub.entry.async_create_background_task(
                self.hub.hass, session.stop(), f"{DOMAIN} stop {address}"
            )
            self._retiring.add(task)
            task.add_done_callback(self._retiring.discard)
        for address, target in desired.items():
            if address not in self.sessions:
                session = Session(self.hub, target)
                self.sessions[address] = session
                session.start()

    # --- routing ------------------------------------------------------------

    def _node_session(self) -> Session | None:
        return next(
            (s for s in self.sessions.values() if s.target.framing is Framing.NODE and s.connected),
            None,
        )

    def _probe_session(self, serial: int) -> Session | None:
        return next(
            (s for s in self.sessions.values() if s.target.probe_serial == serial and s.connected),
            None,
        )

    async def _send(self, session: Session, message_type: int, payload: bytes) -> Frame:
        last_error: Exception | None = None
        for _ in range(COMMAND_ATTEMPTS):
            try:
                frame = await session.request(message_type, payload)
            except TimeoutError as err:
                last_error = err
                continue
            if not frame.success:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="command_rejected"
                )
            return frame
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="command_timeout"
        ) from last_error

    def _no_connection(self) -> HomeAssistantError:
        return HomeAssistantError(translation_domain=DOMAIN, translation_key="not_connected")

    async def async_set_prediction(
        self, serial: int, mode: PredictionMode, set_point: float
    ) -> None:
        if (session := self._probe_session(serial)) is not None:
            await self._send(
                session, MessageType.SET_PREDICTION, set_prediction_payload(set_point, mode)
            )
        elif (session := self._node_session()) is not None:
            await self._send(
                session,
                MessageType.SET_PREDICTION,
                node_set_prediction_payload(serial, set_point, mode),
            )
        else:
            raise self._no_connection()

    async def async_silence_alarms(self) -> None:
        sessions = [s for s in self.sessions.values() if s.connected]
        if not sessions:
            raise self._no_connection()
        for session in sessions:
            node = session.target.framing is Framing.NODE
            payload = node_silence_all_alarms_payload() if node else b""
            try:
                frame = await session.request(MessageType.SILENCE_ALARMS, payload)
            except TimeoutError as err:
                if node and isinstance(err, AckTimeout):
                    # Nodes do not reliably acknowledge a global silence (the official
                    # app never waits for it), but the request was written.
                    _LOGGER.debug("Silence sent to %s, not acknowledged", session.target.address)
                    continue
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="command_timeout"
                ) from err
            if not frame.success:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="command_rejected"
                )

    async def async_set_gauge_alarms(
        self, serial: str, high: AlarmStatus, low: AlarmStatus
    ) -> None:
        if (session := self._node_session()) is None:
            raise self._no_connection()
        await self._send(
            session,
            MessageType.SET_GAUGE_HIGH_LOW_ALARM,
            node_set_gauge_alarms_payload(serial, high, low),
        )

    async def async_set_engine_set_point(self, serial: str, celsius: float) -> None:
        if (session := self._node_session()) is None:
            raise self._no_connection()
        await self._send(
            session,
            MessageType.SET_ENGINE_TEMPERATURE_SET_POINT,
            node_set_engine_set_point_payload(serial, celsius),
        )

    def diagnostics(self) -> list[dict[str, object]]:
        return [
            {
                "address": s.target.address,
                "framing": s.target.framing.value,
                "probe_serial": s.target.probe_serial,
                "connected": s.connected,
                "failures": s.failures,
            }
            for s in self.sessions.values()
        ]
