"""Tests for gs_usb_fd, Cynitor's driver for candleLight (gs_usb) adapters, Classic CAN and CAN FD.

The adapter is FakeCandleLight: a device that answers at the level pyusb
gives, as candleLight firmware built with CAN FD answers, refusing what that
firmware refuses. pyusb itself is replaced too, so these run on any OS,
without it installed.
"""

import queue
import struct
import threading
import time
from array import array
from dataclasses import astuple
from types import SimpleNamespace

import can
import pytest

import gs_usb_fd
from gs_usb_fd import (TX_WINDOW, Capability, GsUsbFdBus, TimingLimits, classic_bit_timing, fd_bit_timing,
                       read_capability, sample_point)


class FakeUSBError(OSError):
    """pyusb's USBError is an IOError too."""


class FakeUSBTimeoutError(FakeUSBError):
    pass


FD = 1 << 8
BT_CONST_EXT = 1 << 10
LPC546XX_QUIRK = 1 << 9

# What candleLight's FDCAN builds report (src/can/m_can.c).
FDCAN_NOMINAL = TimingLimits(1, 256, 1, 128, 128, 1, 512, 1)
FDCAN_DATA = TimingLimits(1, 32, 1, 16, 16, 1, 32, 1)

HOST_FORMAT, BITTIMING, MODE, BT_CONST, DATA_BITTIMING, BT_CONST_EXT_REQUEST = 0, 1, 2, 4, 10, 11
RESET, START = 0, 1
RX = 0xFFFFFFFF
EFF, RTR, ERR = 0x80000000, 0x40000000, 0x20000000
FLAG_OVERFLOW, FLAG_FD, FLAG_BRS, FLAG_ESI = 1, 2, 4, 8


def endpoint(address, attributes=0x02):
    return SimpleNamespace(bEndpointAddress=address, bmAttributes=attributes)


class FakeInterface(list):
    bInterfaceNumber = 0


class FakeCandleLight:
    """A gs_usb device with CAN FD firmware, as pyusb's Device presents it."""

    def __init__(self, feature=FD | BT_CONST_EXT, clock=40_000_000,
                 nominal=FDCAN_NOMINAL, data=FDCAN_DATA,
                 endpoints=(endpoint(0x81), endpoint(0x02))):
        self.feature = feature
        self.clock = clock
        self.nominal = nominal
        self.data = data
        self.endpoints = list(endpoints)
        self.requests = []        # (bRequest, wValue, wIndex, payload) of each request to the device
        self.written = []         # (endpoint, frame, timeout) of each frame sent to the device
        self.reads = []           # (endpoint, size, timeout) of each read
        self.to_host = queue.Queue()  # frames to read, or exceptions to raise
        self.refused = {}         # bRequest -> the error it fails with
        self.write_error = None
        self.disposed = 0

    def get_active_configuration(self):
        return {(0, 0): FakeInterface(self.endpoints)}

    def ctrl_transfer(self, request_type, request, value=0, index=0, data_or_length=None, timeout=None):
        if request in self.refused:
            raise self.refused[request]
        if request_type == 0xC1:
            if request not in (BT_CONST, BT_CONST_EXT_REQUEST):
                raise FakeUSBError("Pipe error")  # a request it does not know
            answer = struct.pack("<II8I", self.feature, self.clock, *astuple(self.nominal))
            if request == BT_CONST_EXT_REQUEST:
                answer += struct.pack("<8I", *astuple(self.data))
            return array("B", answer[:data_or_length])
        payload = bytes(data_or_length)
        if request == BITTIMING:
            self._check_timing(payload, self.nominal)
        elif request == DATA_BITTIMING:
            self._check_timing(payload, self.data)
        elif request == MODE:
            mode, flags = struct.unpack("<II", payload)
            if mode == START and flags & ~self.feature:
                raise FakeUSBError("Pipe error")  # a mode the device lacks
        self.requests.append((request, value, index, payload))
        return len(payload)

    @staticmethod
    def _check_timing(payload, limits):
        # As candleLight's can_check_bittiming_ok: the request stalls otherwise.
        prop_seg, phase_seg1, phase_seg2, sjw, brp = struct.unpack("<5I", payload)
        if not (limits.tseg1_min <= prop_seg + phase_seg1 <= limits.tseg1_max
                and limits.tseg2_min <= phase_seg2 <= limits.tseg2_max
                and sjw <= limits.sjw_max and limits.brp_min <= brp <= limits.brp_max):
            raise FakeUSBError("Pipe error")

    def write(self, ep, data, timeout=None):
        if self.write_error is not None:
            raise self.write_error
        self.written.append((ep, bytes(data), timeout))
        return len(data)

    def read(self, ep, size, timeout=None):
        self.reads.append((ep, size, timeout))
        try:
            item = self.to_host.get(timeout=timeout / 1000)
        except queue.Empty:
            raise FakeUSBTimeoutError("Operation timed out") from None
        if isinstance(item, Exception):
            raise item
        return array("B", item[:size])

    def sent_requests(self, request):
        return [payload for r, _, _, payload in self.requests if r == request]


def host_frame(can_id, data=b"", *, fd=False, brs=False, esi=False, echo_id=RX, channel=0,
               overflow=False, dlc=None, timestamp=False):
    """A frame as the device sends it to the host."""
    flags = (FLAG_FD if fd else 0) | (FLAG_BRS if brs else 0) | (FLAG_ESI if esi else 0) \
        | (FLAG_OVERFLOW if overflow else 0)
    if dlc is None:
        dlc = can.util.len2dlc(len(data)) if fd else len(data)
    frame = struct.pack("<IIBBBB", echo_id, can_id, dlc, channel, flags, 0) + data.ljust(64 if fd else 8, b"\0")
    return frame + (struct.pack("<I", 123456789) if timestamp else b"")


@pytest.fixture(autouse=True)
def fake_pyusb(monkeypatch):
    def dispose(device):
        device.disposed += 1

    monkeypatch.setattr(gs_usb_fd, "usb", SimpleNamespace(
        core=SimpleNamespace(USBError=FakeUSBError, USBTimeoutError=FakeUSBTimeoutError),
        util=SimpleNamespace(dispose_resources=dispose),
    ))


@pytest.fixture
def device(monkeypatch):
    d = FakeCandleLight()
    monkeypatch.setattr(gs_usb_fd, "_scan", lambda: [d])
    return d


@pytest.fixture
def bus(device):
    b = GsUsbFdBus(0, 500_000, 2_000_000)
    yield b
    b.shutdown()


def timing_of(payload):
    prop_seg, phase_seg1, phase_seg2, sjw, brp = struct.unpack("<5I", payload)
    return {"tseg1": prop_seg + phase_seg1, "tseg2": phase_seg2, "sjw": sjw, "brp": brp}


class TestOpen:
    def test_sets_both_phases_and_starts_in_can_fd(self, bus, device):
        assert [r for r, *_ in device.requests] == [HOST_FORMAT, MODE, BITTIMING, DATA_BITTIMING, MODE]
        host_format = device.requests[0]
        assert host_format[1:] == (1, 0, struct.pack("<I", 0x0000BEEF))
        reset, start = device.sent_requests(MODE)
        assert struct.unpack("<II", reset) == (RESET, 0)
        assert struct.unpack("<II", start) == (START, FD)
        # 40 MHz: 80 time quanta per bit at 500 kbit/s, 20 at 2 Mbit/s,
        # one prescaler for both, sampled at 87.5 % and 75 % as Linux samples them.
        assert timing_of(device.sent_requests(BITTIMING)[0]) == {"tseg1": 69, "tseg2": 10, "sjw": 10, "brp": 1}
        assert timing_of(device.sent_requests(DATA_BITTIMING)[0]) == {"tseg1": 14, "tseg2": 5, "sjw": 5, "brp": 1}
        assert bus.protocol == can.CanProtocol.CAN_FD

    def test_endpoints_come_from_the_descriptors(self, monkeypatch):
        d = FakeCandleLight(endpoints=[endpoint(0x85, 0x03), endpoint(0x83), endpoint(0x04)])  # interrupt first
        monkeypatch.setattr(gs_usb_fd, "_scan", lambda: [d])
        b = GsUsbFdBus(0, 500_000, 2_000_000)
        try:
            b.send(can.Message(arbitration_id=1, is_fd=True))
            b.recv(timeout=0.01)
            assert d.written[0][0] == 0x04 and d.reads[0][0] == 0x83
        finally:
            b.shutdown()

    def test_frames_go_to_the_last_bulk_out_endpoint(self, monkeypatch):
        # CANnectivity: a dummy 0x01 before the 0x02 that takes the frames.
        d = FakeCandleLight(endpoints=[endpoint(0x81), endpoint(0x01), endpoint(0x02)])
        monkeypatch.setattr(gs_usb_fd, "_scan", lambda: [d])
        b = GsUsbFdBus(0, 500_000, 2_000_000)
        try:
            b.send(can.Message(arbitration_id=1, is_fd=True))
            assert d.written[0][0] == 0x02
        finally:
            b.shutdown()

    def test_index_picks_among_the_adapters(self, monkeypatch):
        first, second = FakeCandleLight(), FakeCandleLight()
        monkeypatch.setattr(gs_usb_fd, "_scan", lambda: [first, second])
        GsUsbFdBus(1, 500_000, 2_000_000).shutdown()
        assert first.requests == [] and second.requests

    def test_missing_adapter(self, monkeypatch):
        # Worded as python-can's gs_usb words it, which the Windows smoke test looks for.
        monkeypatch.setattr(gs_usb_fd, "_scan", lambda: [])
        with pytest.raises(can.CanInitializationError, match="Cannot find device 0. Devices found: 0"):
            GsUsbFdBus(0, 500_000, 2_000_000)

    def test_classic_only_firmware_is_refused(self, monkeypatch):
        d = FakeCandleLight(feature=0)
        monkeypatch.setattr(gs_usb_fd, "_scan", lambda: [d])
        with pytest.raises(can.CanInitializationError, match="cannot run CAN FD.*Leave the data bitrate out"):
            GsUsbFdBus(0, 500_000, 2_000_000)
        assert MODE not in [r for r, *_ in d.requests]  # never joined the bus
        assert d.disposed == 1  # nor left the device held

    def test_rates_the_clock_cannot_make_are_refused(self, monkeypatch):
        d = FakeCandleLight(clock=170_000_000)
        monkeypatch.setattr(gs_usb_fd, "_scan", lambda: [d])
        with pytest.raises(can.CanInitializationError, match="gs_usb:0: no CAN FD bit timing.*170 MHz"):
            GsUsbFdBus(0, 500_000, 4_000_000)
        assert d.disposed == 1

    def test_usb_errors_become_initialization_errors(self, monkeypatch):
        # Another program holding the adapter, on Windows.
        d = FakeCandleLight()
        d.refused[HOST_FORMAT] = FakeUSBError("[Errno 13] Access denied (insufficient permissions)")
        monkeypatch.setattr(gs_usb_fd, "_scan", lambda: [d])
        with pytest.raises(can.CanInitializationError, match="gs_usb:0: .*Access denied"):
            GsUsbFdBus(0, 500_000, 2_000_000)
        assert d.disposed == 1

    def test_without_pyusb(self, monkeypatch):
        monkeypatch.setattr(gs_usb_fd, "usb", None)
        with pytest.raises(can.CanInterfaceNotImplementedError, match="pyusb"):
            GsUsbFdBus(0, 500_000, 2_000_000)


class TestCapability:
    def test_reads_both_phases_limits(self):
        capability = read_capability(FakeCandleLight(clock=80_000_000))
        assert capability == Capability(FD | BT_CONST_EXT, 80_000_000, FDCAN_NOMINAL, FDCAN_DATA)
        assert capability.can_fd

    def test_data_phase_shares_the_nominal_limits_unless_given_its_own(self):
        # As the Linux driver has it, for devices without BT_CONST_EXT.
        d = FakeCandleLight(feature=FD)
        d.refused[BT_CONST_EXT_REQUEST] = FakeUSBError("Pipe error")
        assert read_capability(d).data == FDCAN_NOMINAL

    def test_classic_firmware(self):
        assert not read_capability(FakeCandleLight(feature=0)).can_fd

    def test_short_answer(self):
        class Terse(FakeCandleLight):
            def ctrl_transfer(self, *args, **kwargs):
                return super().ctrl_transfer(*args, **kwargs)[:12]

        with pytest.raises(can.CanInitializationError, match="12 bytes, not 40"):
            read_capability(Terse())


class TestBitTiming:
    def test_stays_within_the_device_limits(self):
        # A controller with few time quanta per data bit and an even prescaler.
        data = TimingLimits(1, 8, 1, 4, 2, 2, 16, 2)
        timing = fd_bit_timing(Capability(FD, 80_000_000, FDCAN_NOMINAL, data), 1_000_000, 4_000_000)
        assert (timing.nom_bitrate, timing.data_bitrate) == (1_000_000, 4_000_000)
        assert timing.data_brp % 2 == 0 and timing.data_tseg1 <= 8 and timing.data_tseg2 <= 4
        assert timing.data_sjw <= 2  # cut to the device's limit

    def test_one_prescaler_for_both_phases_where_possible(self):
        timing = fd_bit_timing(Capability(FD, 80_000_000, FDCAN_NOMINAL, FDCAN_DATA), 500_000, 2_000_000)
        assert timing.nom_brp == timing.data_brp
        assert (timing.nom_sample_point, timing.data_sample_point) == (87.5, 75.0)

    @pytest.mark.parametrize("bitrate, percent", [
        (125_000, 87.5), (500_000, 87.5), (800_000, 80.0), (1_000_000, 75.0), (2_000_000, 75.0),
    ])
    def test_sample_points_are_the_linux_kernels(self, bitrate, percent):
        assert sample_point(bitrate) == percent

    def test_none_possible(self):
        with pytest.raises(ValueError, match="no CAN FD bit timing for 1000000 bit/s with a 8000000"):
            fd_bit_timing(Capability(FD, 170_000_000, FDCAN_NOMINAL, FDCAN_DATA), 1_000_000, 8_000_000)


def sent_frame(device, index=-1):
    _, frame, _ = device.written[index]
    echo_id, can_id, dlc, channel, flags, _ = struct.unpack_from("<IIBBBB", frame)
    return SimpleNamespace(echo_id=echo_id, can_id=can_id, dlc=dlc, channel=channel, flags=flags,
                           data=frame[12:76], size=len(frame))


class TestSend:
    def test_can_fd_frame(self, bus, device):
        bus.send(can.Message(arbitration_id=0x107D552A, is_extended_id=True, is_fd=True,
                             bitrate_switch=True, data=bytes(range(12))))
        frame = sent_frame(device)
        assert frame.can_id == 0x107D552A | EFF
        assert (frame.dlc, frame.flags, frame.channel) == (9, FLAG_FD | FLAG_BRS, 0)
        assert frame.data == bytes(range(12)) + bytes(52)
        assert frame.echo_id != RX
        assert frame.size == 76  # every frame sized for CAN FD, as the Linux driver sends

    def test_payload_between_lengths_takes_the_next_dlc(self, bus, device):
        bus.send(can.Message(arbitration_id=0x1, is_fd=True, data=bytes(9)))
        assert sent_frame(device).dlc == 9  # 12 bytes; the device pads

    def test_error_state_indicator(self, bus, device):
        bus.send(can.Message(arbitration_id=0x1, is_fd=True, error_state_indicator=True, data=b"\x01"))
        assert sent_frame(device).flags == FLAG_FD | FLAG_ESI

    def test_classic_frame(self, bus, device):
        bus.send(can.Message(arbitration_id=0x123, is_extended_id=False, data=b"\x01\x02\x03"))
        frame = sent_frame(device)
        assert (frame.can_id, frame.dlc, frame.flags, frame.size) == (0x123, 3, 0, 76)

    def test_remote_frame(self, bus, device):
        bus.send(can.Message(arbitration_id=0x123, is_extended_id=False, is_remote_frame=True, dlc=4))
        frame = sent_frame(device)
        assert (frame.can_id, frame.dlc) == (0x123 | RTR, 4)

    def test_classic_frame_cannot_carry_more_than_eight_bytes(self, bus, device):
        with pytest.raises(ValueError):
            bus.send(can.Message(arbitration_id=0x1, data=bytes(12)))
        assert device.written == []

    def test_waits_a_second_unless_told(self, bus, device):
        bus.send(can.Message(arbitration_id=0x1, is_fd=True))
        bus.send(can.Message(arbitration_id=0x1, is_fd=True), timeout=0.25)
        first, second = [timeout for *_, timeout in device.written]
        assert 900 <= first <= 1000 and 150 <= second <= 250  # what is left of it

    def test_failure_is_an_operation_error(self, bus, device):
        device.write_error = FakeUSBTimeoutError("Operation timed out")
        with pytest.raises(can.CanOperationError, match="send failed"):
            bus.send(can.Message(arbitration_id=0x1, is_fd=True))

    def test_lpc546xx_quirk_takes_a_byte_more(self, monkeypatch):
        d = FakeCandleLight(feature=FD | BT_CONST_EXT | LPC546XX_QUIRK)
        monkeypatch.setattr(gs_usb_fd, "_scan", lambda: [d])
        b = GsUsbFdBus(0, 500_000, 2_000_000)
        try:
            b.send(can.Message(arbitration_id=0x1, is_fd=True))
            assert sent_frame(d).size == 77
        finally:
            b.shutdown()


class TestReceive:
    def test_can_fd_frame(self, bus, device):
        device.to_host.put(host_frame(0x107D552A | EFF, bytes(range(64)), fd=True, brs=True))
        msg = bus.recv(timeout=1)
        assert (msg.arbitration_id, msg.is_extended_id, msg.is_fd, msg.bitrate_switch) == (0x107D552A, True, True, True)
        assert bytes(msg.data) == bytes(range(64))
        assert msg.is_rx and not msg.error_state_indicator
        assert abs(msg.timestamp - time.time()) < 5  # wall-clock, as candump logs want

    def test_length_follows_the_dlc(self, bus, device):
        device.to_host.put(host_frame(0x1, bytes(range(20)), fd=True, esi=True))
        msg = bus.recv(timeout=1)
        assert len(msg.data) == 20 and msg.error_state_indicator

    def test_classic_frame(self, bus, device):
        device.to_host.put(host_frame(0x123, b"\xAA\xBB"))
        msg = bus.recv(timeout=1)
        assert (msg.arbitration_id, msg.is_extended_id, msg.is_fd, bytes(msg.data)) == (0x123, False, False, b"\xAA\xBB")

    def test_remote_frame(self, bus, device):
        device.to_host.put(host_frame(0x123 | RTR, dlc=4))
        msg = bus.recv(timeout=1)
        assert msg.is_remote_frame and msg.dlc == 4 and len(msg.data) == 0

    def test_a_hardware_timestamp_after_the_data_is_no_matter(self, bus, device):
        device.to_host.put(host_frame(0x5, b"\x01", fd=True, timestamp=True))
        assert bytes(bus.recv(timeout=1).data) == b"\x01"

    def test_echoes_of_frames_sent_are_dropped(self, bus, device):
        device.to_host.put(host_frame(0x1, b"\x01", fd=True, echo_id=0))
        device.to_host.put(host_frame(0x2, b"\x02", fd=True))
        assert bus.recv(timeout=1).arbitration_id == 0x2

    def test_error_frame_as_socketcan_reports_it(self, bus, device):
        # Bus-off, as candleLight reports it.
        device.to_host.put(host_frame(ERR | 0x40, bytes(8)))
        msg = bus.recv(timeout=1)
        assert msg.is_error_frame and msg.arbitration_id == 0x40

    def test_overflow_is_reported_after_the_frame(self, bus, device):
        device.to_host.put(host_frame(0x7, b"\x07", fd=True, overflow=True))
        frame, overflow = bus.recv(timeout=1), bus.recv(timeout=1)
        assert frame.arbitration_id == 0x7
        assert overflow.is_error_frame and overflow.arbitration_id == 0x4  # CAN_ERR_CRTL
        assert overflow.data[1] == 0x01  # CAN_ERR_CRTL_RX_OVERFLOW

    def test_frames_cut_short_are_dropped(self, bus, device):
        device.to_host.put(host_frame(0x1, bytes(64), fd=True)[:40])
        device.to_host.put(b"\x00" * 6)
        assert bus.recv(timeout=0.2) is None

    def test_other_channels_are_not_ours(self, bus, device):
        device.to_host.put(host_frame(0x1, b"\x01", channel=1))
        assert bus.recv(timeout=0.2) is None

    def test_nothing_within_the_timeout(self, bus, device):
        assert bus.recv(timeout=0) is None
        assert device.reads[-1][2] == 1  # not 0, which libusb takes for no timeout
        assert device.reads[-1][1] == 80  # room for the largest frame a device sends

    def test_usb_failure_fails_the_receive(self, bus, device):
        # Unplugged: unlike python-can's gs_usb, which hides it as a quiet bus.
        device.to_host.put(FakeUSBError("[Errno 19] No such device (it may have been disconnected)"))
        with pytest.raises(can.CanOperationError, match="receive failed.*No such device"):
            bus.recv(timeout=1)


class TestShutdown:
    def test_stops_the_channel_and_releases_the_device(self, device):
        b = GsUsbFdBus(0, 500_000, 2_000_000)
        b.shutdown()
        assert struct.unpack("<II", device.sent_requests(MODE)[-1]) == (RESET, 0)
        assert device.disposed == 1
        b.shutdown()
        assert device.disposed == 1

    def test_releases_an_unplugged_device_without_complaint(self, device):
        b = GsUsbFdBus(0, 500_000, 2_000_000)
        device.refused[MODE] = FakeUSBError("[Errno 19] No such device")
        b.shutdown()
        assert device.disposed == 1


def echo_of(device, index=-1):
    """The device's echo of the frame sent ``index``th: the frame as sent, echo ID and all."""
    _, frame, _ = device.written[index]
    return frame


class TestFlowControl:
    """At most TX_WINDOW frames in flight, each with an echo ID of its own, as the Linux driver keeps them:
    a CANnectivity adapter given a few dozen back-to-back refuses frames until it is unplugged."""

    def test_frames_in_flight_take_echo_ids_of_their_own(self, bus, device):
        for _ in range(TX_WINDOW):
            bus.send(can.Message(arbitration_id=0x1, is_fd=True), timeout=0.1)
        assert sorted(sent_frame(device, i).echo_id for i in range(TX_WINDOW)) == list(range(TX_WINDOW))

    def test_a_full_window_waits_and_then_says_why(self, bus, device):
        for _ in range(TX_WINDOW):
            bus.send(can.Message(arbitration_id=0x1, is_fd=True), timeout=0.1)
        t0 = time.monotonic()
        with pytest.raises(can.CanOperationError, match="sent none of the 10 frames.*acknowledge"):
            bus.send(can.Message(arbitration_id=0x1, is_fd=True), timeout=0.2)
        assert time.monotonic() - t0 >= 0.15
        assert len(device.written) == TX_WINDOW  # nothing more handed to the device

    def test_an_echo_frees_its_echo_id(self, bus, device):
        for _ in range(TX_WINDOW):
            bus.send(can.Message(arbitration_id=0x1, is_fd=True), timeout=0.1)
        third = sent_frame(device, 2).echo_id
        device.to_host.put(echo_of(device, 2))
        assert bus.recv(timeout=0.2) is None  # echoes are not frames off the bus
        bus.send(can.Message(arbitration_id=0x2, is_fd=True), timeout=0.1)
        assert sent_frame(device).echo_id == third

    def test_a_send_waiting_goes_on_when_an_echo_comes(self, bus, device):
        for _ in range(TX_WINDOW):
            bus.send(can.Message(arbitration_id=0x1, is_fd=True), timeout=0.1)
        device.to_host.put(echo_of(device, 0))
        reader = threading.Timer(0.2, lambda: bus.recv(timeout=0.5))
        reader.start()
        try:
            bus.send(can.Message(arbitration_id=0x3, is_extended_id=False, is_fd=True), timeout=2)
        finally:
            reader.join()
        assert sent_frame(device).can_id == 0x3

    def test_a_frame_the_device_did_not_take_frees_its_echo_id(self, bus, device):
        device.write_error = FakeUSBTimeoutError("Operation timed out")
        for _ in range(TX_WINDOW + 2):
            with pytest.raises(can.CanOperationError, match="Operation timed out"):
                bus.send(can.Message(arbitration_id=0x1, is_fd=True), timeout=0.05)
        device.write_error = None
        for _ in range(TX_WINDOW):  # all ten are free still
            bus.send(can.Message(arbitration_id=0x1, is_fd=True), timeout=0.05)

    def test_a_burst_goes_through_as_the_device_echoes(self, monkeypatch):
        """200 frames back-to-back through the hub, the device echoing each one as it takes it."""
        from can_hub import CANHub

        class Echoing(FakeCandleLight):
            def write(self, ep, data, timeout=None):
                n = super().write(ep, data, timeout)
                self.to_host.put(bytes(data))
                return n

        d = Echoing()
        monkeypatch.setattr(gs_usb_fd, "_scan", lambda: [d])
        hub = CANHub("gs_usb:0", 500_000, 2_000_000,
                     open_bus=lambda spec, bitrate, data_bitrate: GsUsbFdBus(0, bitrate, data_bitrate))
        hub.start()
        component = can.Bus(interface="virtual", channel=hub.channel)
        try:
            for i in range(200):
                component.send(can.Message(arbitration_id=i, is_extended_id=True, is_fd=True, data=bytes([i % 256])))
            deadline = time.monotonic() + 5
            while len(d.written) < 200 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(d.written) == 200 and hub.send_failures == 0
        finally:
            component.shutdown()
            hub.stop()


class TestLeftovers:
    """What a device holds from an earlier session is read off before this one starts."""

    def test_frames_from_before_are_not_delivered(self, monkeypatch):
        d = FakeCandleLight()
        d.to_host.put(host_frame(0x111, b"\x01"))                    # a frame from the last session
        d.to_host.put(host_frame(0x222, b"\x02", fd=True, echo_id=4))  # an echo of one it sent
        monkeypatch.setattr(gs_usb_fd, "_scan", lambda: [d])
        b = GsUsbFdBus(0, 500_000)
        try:
            assert b.recv(timeout=0.1) is None
            assert d.to_host.empty()
        finally:
            b.shutdown()

    def test_read_off_between_the_reset_and_the_start(self, device):
        b = GsUsbFdBus(0, 500_000, 2_000_000)
        try:
            assert device.reads[0][2] == 20  # a short wait: nothing new comes while the channel is stopped
            assert [struct.unpack("<II", p)[0] for p in device.sent_requests(MODE)] == [RESET, START]
        finally:
            b.shutdown()


class TestClassic:
    """Without a data bitrate: Classic CAN, through the same driver."""

    @pytest.fixture
    def classic(self, device):
        b = GsUsbFdBus(0, 500_000)
        yield b
        b.shutdown()

    def test_sets_the_nominal_phase_only_and_starts_without_the_fd_flag(self, classic, device):
        assert [r for r, *_ in device.requests] == [HOST_FORMAT, MODE, BITTIMING, MODE]
        assert struct.unpack("<II", device.sent_requests(MODE)[-1]) == (START, 0)
        # 40 MHz, 500 kbit/s: 16 time quanta per bit, sampled at 87.5 %, as Linux samples it.
        t = timing_of(device.sent_requests(BITTIMING)[0])
        assert (t["brp"], t["tseg1"], t["tseg2"]) == (5, 13, 2)
        assert classic.protocol == can.CanProtocol.CAN_20

    def test_classic_only_firmware(self, monkeypatch):
        d = FakeCandleLight(feature=0)
        monkeypatch.setattr(gs_usb_fd, "_scan", lambda: [d])
        GsUsbFdBus(0, 250_000).shutdown()
        assert struct.unpack("<II", d.sent_requests(MODE)[1]) == (START, 0)

    def test_frames_are_sized_for_classic_can(self, classic, device):
        classic.send(can.Message(arbitration_id=0x123, is_extended_id=False, data=b"\x01\x02"))
        frame = sent_frame(device)
        assert (frame.can_id, frame.dlc, frame.size) == (0x123, 2, 20)  # as the Linux driver sends them

    def test_a_can_fd_frame_is_refused(self, classic, device):
        with pytest.raises(ValueError, match="runs Classic CAN"):
            classic.send(can.Message(arbitration_id=0x1, is_fd=True))
        assert device.written == []

    def test_receives_classic_frames(self, classic, device):
        device.to_host.put(host_frame(0x107D552A | EFF, b"\x01\x02\x03"))
        msg = classic.recv(timeout=1)
        assert (msg.arbitration_id, msg.is_fd, bytes(msg.data)) == (0x107D552A, False, b"\x01\x02\x03")

    def test_bit_timing_within_the_device_limits(self):
        # candleLight's bxCAN: at most 16 quanta before the sample point, 8 after.
        bxcan = TimingLimits(1, 16, 1, 8, 4, 1, 1024, 1)
        timing = classic_bit_timing(Capability(0, 48_000_000, bxcan, bxcan), 1_000_000)
        assert timing.bitrate == 1_000_000 and timing.tseg1 <= 16 and timing.tseg2 <= 8
        assert timing.sample_point == 75.0  # Linux's for 1 Mbit/s

    def test_rates_the_clock_cannot_make_are_refused(self, monkeypatch):
        d = FakeCandleLight(clock=7_000_000)
        monkeypatch.setattr(gs_usb_fd, "_scan", lambda: [d])
        with pytest.raises(can.CanInitializationError, match="no bit timing for 1000000 bit/s at the adapter's 7 MHz"):
            GsUsbFdBus(0, 1_000_000)
        assert d.disposed == 1


class TestThroughTheHub:
    """The CAN hub runs the bus as it runs any adapter: frames pass both ways, as CAN FD."""

    def test_frames_pass_both_ways(self, device):
        from can_hub import CANHub

        hub = CANHub("gs_usb:0", 500_000, 2_000_000,
                     open_bus=lambda spec, bitrate, data_bitrate: GsUsbFdBus(0, bitrate, data_bitrate))
        hub.start()
        component = can.Bus(interface="virtual", channel=hub.channel)
        try:
            device.to_host.put(host_frame(0x10 | EFF, bytes(range(32)), fd=True, brs=True))
            got = component.recv(timeout=2)
            assert got is not None and got.is_fd and bytes(got.data) == bytes(range(32))

            component.send(can.Message(arbitration_id=0x20, is_extended_id=True, is_fd=True,
                                       bitrate_switch=True, data=bytes(64)))
            deadline = time.monotonic() + 2
            while not device.written and time.monotonic() < deadline:
                time.sleep(0.01)
            frame = sent_frame(device)
            assert frame.can_id == 0x20 | EFF and frame.flags == FLAG_FD | FLAG_BRS and frame.dlc == 15
        finally:
            component.shutdown()
            hub.stop()
        assert device.disposed == 1
