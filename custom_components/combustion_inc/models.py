"""Live state of each Combustion device, merged from every path it reaches us by.

A probe can be heard directly and through any MeatNet node at the same time, and
one node's advertisements carry several probes. State is therefore kept per
serial number, never per Bluetooth address.
"""

from dataclasses import dataclass, field
from enum import Enum

from .const import (
    DIRECT_PREFERENCE,
    INSTANT_READ_TIMEOUT,
    NODE_TIMEOUT,
    PROBE_TIMEOUT,
    STATUS_TIMEOUT,
    DeviceKind,
)
from .protocol import (
    AlarmStatus,
    EngineAdvertisement,
    EngineStatus,
    GaugeAdvertisement,
    GaugeStatus,
    NodeAdvertisement,
    ProbeAdvertisement,
    ProbeMode,
    ProbeStatus,
    VirtualSensors,
    overheating_sensors,
)
from .protocol.status import FoodSafeStatus, PredictionStatus

# Sessions only change when the device restarts, so a few are plenty to recognize a
# late message of a previous one.
REMEMBERED_SESSIONS = 4
# Statuses that keep being rejected this many sample periods, while nothing is heard
# of the current session, are accepted anyway: a late message is a one-off, a steady
# stream means that session is live.
STUCK_SAMPLE_PERIODS = 3
MIN_STUCK_WINDOW = 15.0  # seconds, when the sample period is unknown or tiny
# Rejection key for statuses without session data (real session IDs are never negative).
SESSIONLESS = -1
# The current cook when its records started over in statuses without session data.
UNNAMED = -2


class Order(Enum):
    NEWER = "newer"  # a later record of the current cook, or a cook that began later
    REPEAT = "repeat"  # the record already applied
    STALE = "stale"  # an older record, or a cook that began before the current one
    RESET = "reset"  # lower records kept coming: the records started over
    UNORDERED = "unordered"  # no session data (old firmware): cannot be ordered


class Outcome(Enum):
    APPLIED = "applied"  # state changed
    CURRENT = "current"  # data of the current cook that changed nothing
    IGNORED = "ignored"  # another cook's data, set aside; says nothing about routes
    REJECTED = "rejected"  # stale


@dataclass
class _Session:
    start: float  # earliest estimate of when it began (monotonic seconds)
    last_status: float  # when a status of it was last heard


@dataclass
class _Rejection:
    first: float
    last: float


@dataclass
class StatusOrder:
    """Orders GATT statuses that reach us over several MeatNet paths.

    A port of the official SDK's SessionStarts. Session IDs are random, but records
    advance once per sample period, so a session began about record * period before
    a status of it arrived. Relay delay only makes that later, so the earliest
    estimate is kept. A session that began clearly (one sample period) before the
    current one is a late message of an older cook, unless its statuses keep coming
    for STUCK_SAMPLE_PERIODS while the current session is silent: then the estimate
    was wrong and that session is live.

    classify() only records rejections; commit() is called once a status was used,
    so a status set aside for another reason does not consume a record number or
    switch the session.
    """

    session: int | None = None
    sequence: int | None = None
    period: float = 0.0  # current session's sample period, seconds
    last_accepted: float | None = None  # when a status was last committed
    sessions: dict[int, _Session] = field(default_factory=dict)
    rejections: dict[int, _Rejection] = field(default_factory=dict)
    fall: _Rejection | None = None  # records below the current one, arriving since

    def classify(
        self, session: int | None, sequence: int, period_ms: int | None, now: float
    ) -> Order:
        if session is None:
            return self._classify_sessionless(sequence, now)
        if session == self.session and self.sequence is not None:
            # Only a status naming the current session shows that cook is still live.
            self.sessions[session].last_status = now
            return self._by_record(sequence, now)
        period = max((period_ms or 0) / 1000, self.period)
        if self._began_clearly_before(
            session, sequence, period_ms, now, period
        ) and not self._stuck(session, period, now):
            self._reject(session, period, now)
            return Order.STALE
        return Order.NEWER

    def _by_record(self, sequence: int, now: float) -> Order:
        """Order within the current cook; recover when its records start over.

        A probe that restarts while a node relays it without session data starts
        again from record 0. One lower record is a late packet; lower records that
        keep coming for the stuck window, with nothing accepted meanwhile, mean the
        records started over (the SDK re-reads session info for this).
        """
        assert self.sequence is not None
        if sequence > self.sequence:
            return Order.NEWER
        if sequence == self.sequence:
            return Order.REPEAT
        window = _window(self.period)
        fall = self.fall
        if fall is not None and now - fall.last <= window:
            fall.last = now
            quiet = self.last_accepted is None or now - self.last_accepted >= window
            if now - fall.first >= window and quiet:
                return Order.RESET
        else:
            self.fall = _Rejection(now, now)
        return Order.STALE

    def _classify_sessionless(self, sequence: int, now: float) -> Order:
        """A status without session data: the probe's first notification after a
        connect, advertisement-derived relays, or older firmware anywhere on the way.

        It is taken as part of the current cook unless its record makes it clearly
        older, judged with the current session's sample period (SDK:
        isStatusWithoutSessionInfoOfOlderSession).
        """
        current = self.sessions.get(self.session) if self.session is not None else None
        if current is None or not self.period or self.sequence is None:
            return Order.UNORDERED  # nothing known to compare with
        start = now - sequence * self.period
        # Both estimates drift with the device clock: two sample periods plus 1 % of
        # how long the current session has run.
        tolerance = 2 * self.period + (now - current.start) / 100
        if start + tolerance < current.start and not self._stuck(SESSIONLESS, self.period, now):
            self._reject(SESSIONLESS, self.period, now)
            return Order.STALE
        self.rejections.pop(SESSIONLESS, None)
        # It cannot prove which cook it belongs to, so it does not count as the
        # current cook being heard.
        return self._by_record(sequence, now)

    def commit(self, session: int | None, sequence: int, period_ms: int | None, now: float) -> None:
        self.last_accepted = now
        self.fall = None
        if session is None:
            if self.session is None or self.sequence is None:
                return  # nothing known to attach it to
            if sequence < self.sequence:
                # The records started over (RESET): this is a new cook whose ID we
                # have not seen. It is not the old one, whose late packets must now
                # compare as older by their start.
                self.sessions[UNNAMED] = _Session(now - sequence * self.period, now)
                _trim(self.sessions, keep={UNNAMED, self.session})
                self.session = UNNAMED
            else:
                current = self.sessions[self.session]
                if self.period:
                    current.start = min(current.start, now - sequence * self.period)
                if self.session == UNNAMED:
                    current.last_status = now  # its own unnamed stream is all we have
            self.sequence = sequence
            return
        self.rejections.pop(session, None)
        estimate = _estimate_start(now, sequence, period_ms)
        if (known := self.sessions.get(session)) is None:
            self.sessions[session] = _Session(estimate, now)
            _trim(self.sessions, keep={session, self.session})
        else:
            known.start = min(known.start, estimate)
            known.last_status = now
        self.session = session
        self.sequence = sequence
        if period_ms:
            self.period = period_ms / 1000

    def _began_clearly_before(
        self, session: int, sequence: int, period_ms: int | None, now: float, period: float
    ) -> bool:
        current = self.sessions.get(self.session) if self.session is not None else None
        if current is None or not period_ms:
            return False
        start = _estimate_start(now, sequence, period_ms)
        if (known := self.sessions.get(session)) is not None:
            start = min(start, known.start)
        return start + period < current.start

    def _stuck(self, session: int, period: float, now: float) -> bool:
        window = _window(period)
        rejection = self.rejections.get(session)
        if rejection is None or now - rejection.last > window:
            return False
        current = self.sessions.get(self.session) if self.session is not None else None
        return now - rejection.first >= window and (
            current is None or now - current.last_status >= window
        )

    def _reject(self, session: int, period: float, now: float) -> None:
        rejection = self.rejections.get(session)
        if rejection is not None and now - rejection.last <= _window(period):
            rejection.last = now
        else:
            self.rejections[session] = _Rejection(now, now)
            _trim(self.rejections, keep={session})


def _window(period: float) -> float:
    """How long a contradicting stream must last before it wins (see STUCK_SAMPLE_PERIODS)."""
    return max(STUCK_SAMPLE_PERIODS * period, MIN_STUCK_WINDOW)


def _fresh(at: float | None, now: float) -> bool:
    """Direct data this recent wins over relayed data."""
    return at is not None and now - at < DIRECT_PREFERENCE


def _estimate_start(now: float, sequence: int, period_ms: int | None) -> float:
    return now - sequence * (period_ms or 0) / 1000


def _trim(entries: dict[int, _Session] | dict[int, _Rejection], keep: set[int | None]) -> None:
    """Forget the oldest entries beyond REMEMBERED_SESSIONS, never those in keep."""
    for key in list(entries):
        if len(entries) <= REMEMBERED_SESSIONS:
            break
        if key not in keep:
            del entries[key]


@dataclass(kw_only=True)
class CombustionDevice:
    kind: DeviceKind
    serial: str
    address: str | None = None  # the device's own BLE address, once known
    rssi: int | None = None
    high_radio_power: bool | None = None
    last_seen: float | None = None  # monotonic time of the last data, any path
    last_raw: str | None = None  # hex of the last own advertisement, for diagnostics
    connected: bool = False  # Home Assistant holds a GATT connection to it
    available: bool = False

    @property
    def key(self) -> str:
        return f"{self.kind}_{self.serial}"

    def is_fresh(self, now: float, timeout: float) -> bool:
        return self.last_seen is not None and now - self.last_seen < timeout

    def compute_available(self, now: float, last_heard: float | None) -> bool:
        """last_heard: when Home Assistant last heard the device's own address."""
        if self.connected or self.is_fresh(now, NODE_TIMEOUT):
            return True
        return last_heard is not None and now - last_heard < NODE_TIMEOUT

    def note_own_advertisement(self, now: float, address: str, rssi: int, raw: bytes) -> None:
        self.address = address
        self.rssi = rssi
        self.last_seen = now
        self.last_raw = raw.hex()


@dataclass(kw_only=True)
class ProbeDevice(CombustionDevice):
    kind: DeviceKind = DeviceKind.PROBE
    serial_number: int = 0
    temperatures: tuple[float, ...] | None = None  # last Normal-mode reading, T1-T8
    virtual_sensors: VirtualSensors | None = None
    mode: ProbeMode | None = None
    probe_id: int | None = None  # 0-7
    color: int | None = None
    battery_low: bool | None = None
    overheat_raw: int | None = None
    power_mode: int | None = None
    instant_read: float | None = None
    instant_read_at: float | None = None
    hop_count: int | None = None  # 0 = heard directly
    last_direct: float | None = None  # direct data of any kind
    last_direct_status: float | None = None  # direct GATT status, which names the cook
    last_relayed: float | None = None
    # Connection-only data
    prediction: PredictionStatus | None = None
    food_safe: FoodSafeStatus | None = None
    high_alarms: tuple[AlarmStatus, ...] | None = None
    low_alarms: tuple[AlarmStatus, ...] | None = None
    status_at: float | None = None
    status_order: StatusOrder = field(default_factory=StatusOrder)
    last_node_status: float | None = None  # last status a MeatNet node delivered
    connection_record: tuple[int | None, int] = (None, -1)  # (session, record) applied

    def compute_available(self, now: float, last_heard: float | None) -> bool:
        # A node keeps advertising after the probe went back into its charger, so a
        # node's presence says nothing. The probe's own advertisements do: Home
        # Assistant skips callbacks for unchanged ones but still notes when it heard them.
        if self.is_fresh(now, PROBE_TIMEOUT):
            return True
        return (
            self.last_direct is not None
            and last_heard is not None
            and now - last_heard < PROBE_TIMEOUT
        )

    def has_status(self, now: float) -> bool:
        return self.status_at is not None and now - self.status_at < STATUS_TIMEOUT

    def instant_read_value(self, now: float) -> float | None:
        if self.instant_read_at is None or now - self.instant_read_at >= INSTANT_READ_TIMEOUT:
            return None
        return self.instant_read

    def virtual_temperature(self, which: str) -> float | None:
        if self.temperatures is None or self.virtual_sensors is None:
            return None
        index = getattr(self.virtual_sensors, which)
        return None if index is None else self.temperatures[index]

    @property
    def overheating(self) -> tuple[int, ...] | None:
        if self.temperatures is None:
            return None
        return overheating_sensors(self.temperatures, self.overheat_raw)

    def apply_advertisement(
        self, adv: ProbeAdvertisement, now: float, address: str, rssi: int
    ) -> bool:
        """Merge an advertisement; returns False when it lost arbitration."""
        if adv.is_repeated:
            hops = adv.hop_count or 1
            if _fresh(self.last_direct, now):
                self.last_seen = now
                return False
            if (
                self.hop_count is not None
                and hops > self.hop_count
                and self.last_relayed is not None
                and now - self.last_relayed < DIRECT_PREFERENCE
            ):
                self.last_seen = now
                return False
            self.last_relayed = now
        else:
            hops = 0
            self.last_direct = now
            self.address = address
            if adv.power_mode is not None:
                self.power_mode = adv.power_mode
        self.hop_count = hops
        self.rssi = rssi
        self.last_seen = now
        if adv.high_radio_power is not None:
            self.high_radio_power = adv.high_radio_power
        if adv.battery_low is not None:
            self.battery_low = adv.battery_low
        self.overheat_raw = adv.overheat_raw
        if adv.mode_id_color is not None:
            self._apply_mode(
                adv.mode_id_color.mode, adv.mode_id_color.probe_id, adv.mode_id_color.color
            )
        self._apply_reading(
            adv.mode_id_color.mode if adv.mode_id_color else ProbeMode.NORMAL,
            adv.temperatures,
            adv.virtual_sensors,
            now,
        )
        return True

    def apply_status(self, status: ProbeStatus, now: float) -> Outcome:
        """Merge a GATT status from a direct connection or relayed by a node."""
        order = self.status_order.classify(
            status.session_id, status.max_sequence, status.sample_period_ms, now
        )
        if order is Order.STALE:
            return Outcome.REJECTED
        if order is Order.RESET:
            self.connection_record = (None, -1)  # the records started over
        direct = status.hop_count is None
        if direct:
            self.last_direct = self.last_direct_status = now
        self.last_seen = now
        # Instant Read statuses repeat the record number, so only Normal repeats are
        # duplicates (while the data they would refresh is still current).
        if (
            order is Order.REPEAT
            and status.mode_id_color.mode is ProbeMode.NORMAL
            and self.has_status(now)
        ):
            return Outcome.CURRENT
        if not direct and _fresh(self.last_direct_status, now):
            # A direct GATT link owns the cook and the record order. A relay may only
            # fill in connection-only data for the cook that link is on.
            if order is Order.NEWER and status.session_id not in (None, self.status_order.session):
                return Outcome.IGNORED
            if self._apply_connection_data(status, now):
                return Outcome.APPLIED
            return Outcome.CURRENT
        # Otherwise the status sets the cook and record order. Direct advertisements
        # carry no cook, but while fresh they still own the readings.
        self.status_order.commit(
            status.session_id, status.max_sequence, status.sample_period_ms, now
        )
        if direct or not _fresh(self.last_direct, now):
            self.hop_count = 0 if direct else status.hop_count
            self.overheat_raw = status.overheat_raw
            if status.power_mode is not None:
                self.power_mode = status.power_mode
            if status.high_radio_power is not None:
                self.high_radio_power = status.high_radio_power
            mic = status.mode_id_color
            self._apply_mode(mic.mode, mic.probe_id, mic.color)
            self._apply_reading(mic.mode, status.temperatures, status.virtual_sensors, now)
        self._apply_connection_data(status, now)
        return Outcome.APPLIED

    def _apply_connection_data(self, status: ProbeStatus, now: float) -> bool:
        """Prediction, food safety and alarms, in their own record order.

        They can arrive by relay ahead of the direct reading of the same record, so
        they keep their own order: an older record never rolls them back.
        """
        # A status without session data was accepted as part of the current cook.
        session = status.session_id if status.session_id is not None else self.status_order.session
        sequence = status.max_sequence
        if (
            session is not None
            and session == self.connection_record[0]
            and sequence < self.connection_record[1]
        ):
            return False
        if session is not None:
            self.connection_record = (session, sequence)
        self.status_at = now
        self.battery_low = status.battery_low
        self.prediction = status.prediction
        self.food_safe = status.food_safe_status
        if status.high_alarms is not None:
            self.high_alarms = status.high_alarms
            self.low_alarms = status.low_alarms
        return True

    def _apply_mode(self, mode: ProbeMode, probe_id: int, color: int) -> None:
        self.mode = mode
        self.probe_id = probe_id
        self.color = color

    def _apply_reading(
        self,
        mode: ProbeMode,
        temperatures: tuple[float, ...],
        virtual: VirtualSensors | None,
        now: float,
    ) -> None:
        if mode is ProbeMode.NORMAL:
            self.temperatures = temperatures
            if virtual is not None:
                self.virtual_sensors = virtual
        elif mode is ProbeMode.INSTANT_READ:
            # Only T1 holds a value (the instant reading); the rest are zero filler.
            self.instant_read = temperatures[0]
            self.instant_read_at = now


@dataclass(kw_only=True)
class GaugeDevice(CombustionDevice):
    kind: DeviceKind = DeviceKind.GAUGE
    temperature: float | None = None
    sensor_present: bool | None = None
    overheating: bool | None = None
    low_battery: bool | None = None
    high_alarm: AlarmStatus | None = None
    low_alarm: AlarmStatus | None = None
    gauge_id: int | None = None
    hop_count: int | None = None
    status_at: float | None = None
    status_order: StatusOrder = field(default_factory=StatusOrder)

    def apply_advertisement(self, adv: GaugeAdvertisement) -> None:
        self._apply(
            adv.temperature,
            adv.sensor_present,
            adv.overheating,
            adv.low_battery,
            adv.high_alarm,
            adv.low_alarm,
        )
        if adv.high_radio_power is not None:
            self.high_radio_power = adv.high_radio_power
        if adv.gauge_id is not None:
            self.gauge_id = adv.gauge_id

    def apply_status(self, status: GaugeStatus, now: float) -> Outcome:
        order = self.status_order.classify(
            status.session_id, status.max_sequence, status.sample_period_ms, now
        )
        if order is Order.STALE:
            return Outcome.REJECTED
        # A gauge without a sensor logs nothing, so a repeated record still applies.
        self.status_order.commit(
            status.session_id, status.max_sequence, status.sample_period_ms, now
        )
        self.last_seen = now
        self._apply(
            status.temperature,
            status.sensor_present,
            status.overheating,
            status.low_battery,
            status.high_alarm,
            status.low_alarm,
        )
        self.hop_count = status.hop_count
        if status.gauge_id is not None:
            self.gauge_id = status.gauge_id
        self.status_at = now
        return Outcome.APPLIED

    def _apply(
        self,
        temperature: float | None,
        present: bool,
        overheating: bool,
        low_battery: bool,
        high: AlarmStatus,
        low: AlarmStatus,
    ) -> None:
        self.temperature = temperature
        self.sensor_present = present
        self.overheating = overheating
        self.low_battery = low_battery
        self.high_alarm = high
        self.low_alarm = low


@dataclass(kw_only=True)
class EngineDevice(CombustionDevice):
    kind: DeviceKind = DeviceKind.ENGINE
    set_point: float | None = None
    app_mode: bool | None = None
    control_device_connected: bool | None = None
    lid_open: bool | None = None
    fixed_speed: bool | None = None
    status: EngineStatus | None = None
    status_at: float | None = None
    status_order: StatusOrder = field(default_factory=StatusOrder)

    def has_status(self, now: float) -> bool:
        return self.status_at is not None and now - self.status_at < STATUS_TIMEOUT

    def apply_advertisement(self, adv: EngineAdvertisement) -> None:
        self.set_point = adv.set_point
        self.app_mode = adv.app_mode
        self.control_device_connected = adv.control_device_connected
        self.lid_open = adv.lid_open
        self.fixed_speed = adv.fixed_speed
        if adv.high_radio_power is not None:
            self.high_radio_power = adv.high_radio_power

    def apply_status(self, status: EngineStatus, now: float) -> Outcome:
        order = self.status_order.classify(
            status.session_id, status.max_sequence, status.sample_period_ms, now
        )
        if order is Order.STALE:
            return Outcome.REJECTED
        self.status_order.commit(
            status.session_id, status.max_sequence, status.sample_period_ms, now
        )
        self.last_seen = now
        self.status = status
        self.set_point = status.set_point
        self.app_mode = status.app_mode
        self.control_device_connected = status.control_device_connected
        self.lid_open = status.lid_open
        self.fixed_speed = status.fixed_speed
        self.status_at = now
        return Outcome.APPLIED


@dataclass(kw_only=True)
class NodeDevice(CombustionDevice):
    """Display (Timer) or Booster."""

    def apply_advertisement(self, adv: NodeAdvertisement) -> None:
        if adv.high_radio_power is not None:
            self.high_radio_power = adv.high_radio_power


@dataclass
class DeviceIndex:
    devices: dict[str, CombustionDevice] = field(default_factory=dict)
    probes: dict[int, ProbeDevice] = field(default_factory=dict)
    # Own BLE address of each node we have identified -> device key
    node_addresses: dict[str, str] = field(default_factory=dict)

    def add(self, device: CombustionDevice) -> None:
        self.devices[device.key] = device
        if isinstance(device, ProbeDevice):
            self.probes[device.serial_number] = device


def make_device(kind: DeviceKind, serial: str) -> CombustionDevice:
    match kind:
        case DeviceKind.PROBE:
            return ProbeDevice(serial=serial, serial_number=int(serial, 16))
        case DeviceKind.GAUGE:
            return GaugeDevice(serial=serial)
        case DeviceKind.ENGINE:
            return EngineDevice(serial=serial)
        case _:
            return NodeDevice(kind=kind, serial=serial)
