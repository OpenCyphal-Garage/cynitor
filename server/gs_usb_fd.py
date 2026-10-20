"""Cynitor's driver for candleLight (gs_usb) adapters, Classic CAN and CAN FD.

python-can's gs_usb interface, and the gs_usb package beneath it, speak the
Classic CAN part of the gs_usb protocol. candleLight firmware built with
CAN FD, and other gs_usb devices such as CANnectivity, take CAN FD through
the protocol's extensions, as the Linux kernel's gs_usb driver uses them: a
feature bit, timing limits for the data phase, a request that sets its
timing, a mode flag, and frames with up to 64 data bytes. On Linux that
driver serves these adapters as SocketCAN. Elsewhere GsUsbFdBus stands in for
it, and the CAN hub opens every gs_usb adapter with it, Classic CAN too (no
data bitrate), not with python-can's: like the Linux driver, it keeps at most
TX_WINDOW frames in flight, each with an echo ID of its own, and sends the
next only when the device has echoed one. python-can's sends every frame with
echo ID 0 and no limit: a burst of a few dozen frames, such as a Classic CAN
port list, leaves a CANnectivity adapter refusing frames until it is
unplugged. It also expects one packet size, and fails on the frames a device
still holds from a session in another mode, which this driver reads off when
it opens the device.

Devices are counted as the gs_usb package lists them, as python-can counts
them, so that gs_usb:<n> names the same adapter either way; like python-can,
GsUsbFdBus uses a device's first CAN channel. pyusb comes with the gs_usb
package, which only Windows installs (see requirements.txt).
"""

from __future__ import annotations

import collections
import logging
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import can
from can.util import dlc2len, len2dlc

try:
    import usb.core
    import usb.util
except ImportError:  # see the module docstring
    usb = None

logger = logging.getLogger(__name__)

# Control requests (enum gs_usb_breq in the Linux driver), all of them vendor
# requests to the interface: host to device, or device to host.
_BREQ_HOST_FORMAT = 0
_BREQ_BITTIMING = 1
_BREQ_MODE = 2
_BREQ_BT_CONST = 4
_BREQ_DATA_BITTIMING = 10
_BREQ_BT_CONST_EXT = 11
_TO_DEVICE = 0x41
_TO_HOST = 0xC1
_CONTROL_TIMEOUT_MS = 1000

_MODE_RESET = 0
_MODE_START = 1
_MODE_FLAG_FD = 1 << 8

# Features a device reports with its bit timing limits.
_FEATURE_FD = 1 << 8
_FEATURE_REQ_USB_QUIRK_LPC546XX = 1 << 9  # frames sent to it take one byte more
_FEATURE_BT_CONST_EXT = 1 << 10           # the data phase has limits of its own

# Frame flags.
_FLAG_OVERFLOW = 1 << 0  # the device lost frames before this one
_FLAG_FD = 1 << 1
_FLAG_BRS = 1 << 2
_FLAG_ESI = 1 << 3

# SocketCAN's can_id flags (linux/can.h), which gs_usb frames carry as they are.
_CAN_EFF_FLAG = 0x80000000
_CAN_RTR_FLAG = 0x40000000
_CAN_ERR_FLAG = 0x20000000
_CAN_EFF_MASK = 0x1FFFFFFF
# A receive overflow, as SocketCAN reports one (linux/can/error.h).
_CAN_ERR_CRTL = 0x00000004
_CAN_ERR_CRTL_RX_OVERFLOW = 0x01

_HOST_BYTE_ORDER = 0x0000BEEF  # little endian, all candleLight knows
_ECHO_ID_RX = 0xFFFFFFFF       # a frame off the bus; any other echoes one sent
_CHANNEL = 0

# Frames handed to the device and not echoed yet, at most: GS_MAX_TX_URBS in the
# Linux driver. Each takes an echo ID of its own, 0 to TX_WINDOW - 1.
TX_WINDOW = 10

_HEADER = struct.Struct("<IIBBBB")  # echo_id, can_id, can_dlc, channel, flags, reserved
_CLASSIC_DATA_SIZE = 8
_FD_DATA_SIZE = 64
# The most a device sends: a CAN FD frame, with a hardware timestamp after it.
_MAX_FRAME_SIZE = _HEADER.size + _FD_DATA_SIZE + 4
# BT_CONST answers with features then limits, BT_CONST_EXT with the data phase's after.
_FEATURES = struct.Struct("<II")    # feature, fclk_can
_LIMITS = struct.Struct("<8I")      # TimingLimits' fields
_TIMING = struct.Struct("<5I")      # prop_seg, phase_seg1, phase_seg2, sjw, brp
_MODE = struct.Struct("<II")        # mode, flags

_ENDPOINT_IN = 0x80
_TRANSFER_TYPE_MASK = 0x03
_TRANSFER_TYPE_BULK = 0x02

# How long send waits by default for the device to take a frame, in seconds.
_SEND_TIMEOUT = 1.0

# Reading off what a device still holds from an earlier session (see GsUsbFdBus):
# how long a read waits for one more, and how many are read at most.
_LEFTOVER_TIMEOUT_MS = 20
_MAX_LEFTOVERS = 256


@dataclass(frozen=True)
class TimingLimits:
    """The bit timings one phase of a device's CAN controller takes, in time quanta."""

    tseg1_min: int
    tseg1_max: int
    tseg2_min: int
    tseg2_max: int
    sjw_max: int
    brp_min: int
    brp_max: int
    brp_inc: int

    def admits(self, brp: int, tseg1: int, tseg2: int) -> bool:
        return (self.brp_min <= brp <= self.brp_max and brp % max(self.brp_inc, 1) == 0
                and self.tseg1_min <= tseg1 <= self.tseg1_max
                and self.tseg2_min <= tseg2 <= self.tseg2_max)


@dataclass(frozen=True)
class Capability:
    """What a gs_usb device's CAN channel can do."""

    feature: int
    clock: int  # of the CAN controller, in Hz
    nominal: TimingLimits
    data: TimingLimits

    @property
    def can_fd(self) -> bool:
        return bool(self.feature & _FEATURE_FD)


def read_capability(device) -> Capability:
    """Ask pyusb ``device`` what its first CAN channel can do.

    A device that gives the data phase no limits of its own gets the nominal
    ones there, as the Linux driver gives it.
    """
    answer = _control_in(device, _BREQ_BT_CONST, _FEATURES.size + _LIMITS.size)
    feature, clock = _FEATURES.unpack_from(answer)
    nominal = data = TimingLimits(*_LIMITS.unpack_from(answer, _FEATURES.size))
    if feature & _FEATURE_FD and feature & _FEATURE_BT_CONST_EXT:
        answer = _control_in(device, _BREQ_BT_CONST_EXT, _FEATURES.size + 2 * _LIMITS.size)
        data = TimingLimits(*_LIMITS.unpack_from(answer, _FEATURES.size + _LIMITS.size))
    return Capability(feature, clock, nominal, data)


def sample_point(bitrate: int) -> float:
    """Where in the bit a phase running at ``bitrate`` is sampled, in percent.

    What the Linux kernel takes for a bitrate given without a sample point
    (CiA's recommendation), so that an adapter runs here as it does as
    SocketCAN. A node switches to the data bitrate at the sample point of
    the BRS bit: sampled at 80 % among nodes sampling at 87.5 %, an adapter
    sends frames at 500 kbit/s and 2 Mbit/s that they take for errors.
    """
    if bitrate > 800_000:
        return 75.0
    if bitrate > 500_000:
        return 80.0
    return 87.5


def fd_bit_timing(capability: Capability, bitrate: int, data_bitrate: int) -> can.BitTimingFd:
    """A bit timing for ``bitrate`` with a ``data_bitrate`` data phase that the device takes.

    Of python-can's timings for each phase's sample point (see sample_point),
    those within the device's limits, ranked as python-can ranks them: one
    prescaler for both phases if there is such a timing, then the sample
    points nearest those asked for, then the smallest prescalers. Each
    synchronization jump width is cut to what the device takes. ValueError
    if there is none.
    """
    nominal_point, data_point = sample_point(bitrate), sample_point(data_bitrate)
    fitting = [
        _within_sjw_limits(timing, capability)
        for timing in can.BitTimingFd.iterate_from_sample_point(
            capability.clock, bitrate, nominal_point, data_bitrate, data_point)
        if capability.nominal.admits(timing.nom_brp, timing.nom_tseg1, timing.nom_tseg2)
        and capability.data.admits(timing.data_brp, timing.data_tseg1, timing.data_tseg2)
    ]
    if not fitting:
        raise ValueError(
            f"no CAN FD bit timing for {bitrate} bit/s with a {data_bitrate} bit/s data phase "
            f"at the adapter's {capability.clock / 1e6:g} MHz CAN clock"
        )
    return min(fitting, key=lambda timing: (
        timing.nom_brp != timing.data_brp,
        abs(timing.nom_sample_point - nominal_point)
        + abs(timing.data_sample_point - data_point),
        abs(timing.nom_brp - timing.data_brp),
        timing.nom_brp + timing.data_brp,
    ))


def classic_bit_timing(capability: Capability, bitrate: int) -> can.BitTiming:
    """A Classic CAN bit timing for ``bitrate`` that the device takes.

    Of python-can's timings for the sample point the Linux kernel takes (see
    sample_point), those within the device's limits: the one sampling
    nearest that point, then with the smallest prescaler. The
    synchronization jump width is cut to what the device takes. ValueError
    if there is none.
    """
    point = sample_point(bitrate)
    fitting = [
        timing if timing.sjw <= capability.nominal.sjw_max else can.BitTiming(
            f_clock=timing.f_clock, brp=timing.brp, tseg1=timing.tseg1, tseg2=timing.tseg2,
            sjw=capability.nominal.sjw_max)
        for timing in can.BitTiming.iterate_from_sample_point(capability.clock, bitrate, point)
        if capability.nominal.admits(timing.brp, timing.tseg1, timing.tseg2)
    ]
    if not fitting:
        raise ValueError(f"no bit timing for {bitrate} bit/s at the adapter's {capability.clock / 1e6:g} MHz CAN clock")
    return min(fitting, key=lambda timing: (abs(timing.sample_point - point), timing.brp))


def _within_sjw_limits(timing: can.BitTimingFd, capability: Capability) -> can.BitTimingFd:
    nom_sjw = min(timing.nom_sjw, capability.nominal.sjw_max)
    data_sjw = min(timing.data_sjw, capability.data.sjw_max)
    if (nom_sjw, data_sjw) == (timing.nom_sjw, timing.data_sjw):
        return timing
    return can.BitTimingFd(
        f_clock=timing.f_clock,
        nom_brp=timing.nom_brp, nom_tseg1=timing.nom_tseg1, nom_tseg2=timing.nom_tseg2, nom_sjw=nom_sjw,
        data_brp=timing.data_brp, data_tseg1=timing.data_tseg1, data_tseg2=timing.data_tseg2,
        data_sjw=data_sjw,
    )


class GsUsbFdBus(can.BusABC):
    """The first CAN channel of the ``index``th gs_usb device: CAN FD with a
    ``data_bitrate``, Classic CAN without one.

    CanInitializationError if there is no such device, it cannot be opened
    (another program has it), or it cannot run CAN FD or these bitrates.

    One thread may receive while another sends, as the CAN hub's do. The
    device echoes every frame sent back; ``recv`` drops those, and returns
    the frames off the bus, error frames among them. A USB error other than
    a timeout, as when the adapter is unplugged, fails ``recv``. ``send``
    waits while TX_WINDOW frames are in flight.
    """

    def __init__(self, index: int, bitrate: int, data_bitrate: Optional[int] = None, **kwargs: Any) -> None:
        if usb is None:
            raise can.CanInterfaceNotImplementedError("gs_usb adapters need pyusb, which is not installed")
        self.channel_info = f"gs_usb:{index}"
        self._fd = data_bitrate is not None
        devices = _scan()
        if not 0 <= index < len(devices):
            raise can.CanInitializationError(f"Cannot find device {index}. Devices found: {len(devices)}")
        self.device = devices[index]
        timing: can.BitTiming | can.BitTimingFd
        try:
            self._ep_in, self._ep_out, interface = _bulk_endpoints(self.device)
            _control_out(self.device, _BREQ_HOST_FORMAT, struct.pack("<I", _HOST_BYTE_ORDER),
                         value=1, index=interface)
            capability = read_capability(self.device)
            if self._fd and not capability.can_fd:
                raise can.CanInitializationError(
                    f"{self.channel_info} cannot run CAN FD: its firmware does Classic CAN only. "
                    f"Leave the data bitrate out for Classic CAN"
                )
            try:
                timing = (fd_bit_timing(capability, bitrate, data_bitrate) if data_bitrate is not None
                          else classic_bit_timing(capability, bitrate))
            except ValueError as exc:
                raise can.CanInitializationError(f"{self.channel_info}: {exc}") from None
            self._quirk = b"\0" if capability.feature & _FEATURE_REQ_USB_QUIRK_LPC546XX else b""
            # The channel takes timings while stopped; a program may have left it running.
            self._set_mode(_MODE_RESET)
            leftovers = self._drop_leftovers()
            if isinstance(timing, can.BitTimingFd):
                _control_out(self.device, _BREQ_BITTIMING,
                             _phase_timing(timing.nom_brp, timing.nom_tseg1, timing.nom_tseg2, timing.nom_sjw))
                _control_out(self.device, _BREQ_DATA_BITTIMING,
                             _phase_timing(timing.data_brp, timing.data_tseg1, timing.data_tseg2, timing.data_sjw))
            else:
                _control_out(self.device, _BREQ_BITTIMING,
                             _phase_timing(timing.brp, timing.tseg1, timing.tseg2, timing.sjw))
            self._set_mode(_MODE_START, _MODE_FLAG_FD if self._fd else 0)
        except BaseException as exc:
            usb.util.dispose_resources(self.device)
            if isinstance(exc, usb.core.USBError):
                raise can.CanInitializationError(f"{self.channel_info}: {exc}") from exc
            raise
        logger.info("%s: %s, %s%s", self.channel_info, "CAN FD" if self._fd else "Classic CAN", timing,
                    f"; {leftovers} frames left from an earlier session dropped" if leftovers else "")
        # Messages read but not returned yet: a frame and its overflow notice.
        self._pending: collections.deque[can.Message] = collections.deque()
        # Echo IDs not in flight. A frame sent takes one; its echo gives it back.
        self._free_echo_ids = list(range(TX_WINDOW))
        self._echo_ids_freed = threading.Condition()
        self._can_protocol = can.CanProtocol.CAN_FD if self._fd else can.CanProtocol.CAN_20
        super().__init__(channel=index, **kwargs)

    def _drop_leftovers(self) -> int:
        """Read off, with the channel stopped, what the device still holds, and say how many.

        A device hands its next session the frames it received, and the
        echoes it made, just before the last one stopped (a CANnectivity
        adapter, two frames after a busy CAN FD session): frames from before
        this session, in that session's format, and echoes that would give
        back echo IDs this session has not used.
        """
        dropped = 0
        while dropped < _MAX_LEFTOVERS:
            try:
                self.device.read(self._ep_in, _MAX_FRAME_SIZE, _LEFTOVER_TIMEOUT_MS)
            except usb.core.USBTimeoutError:
                break
            dropped += 1
        return dropped

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        """Hand ``msg`` to the device; CanOperationError if it does not take it within ``timeout``.

        It waits for an echo ID while TX_WINDOW frames are in flight. None
        waits a second, as python-can's gs_usb does, rather than for ever: a
        device with nobody on the bus to acknowledge its frames sends none
        of them, and echoes none.
        """
        frame = self._frame(msg)
        deadline = time.monotonic() + (_SEND_TIMEOUT if timeout is None else timeout)
        with self._echo_ids_freed:
            while not self._free_echo_ids:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise can.CanOperationError(
                        f"{self.channel_info}: send failed: the adapter has sent none of the {TX_WINDOW} frames "
                        f"it was given last; is another node on the bus to acknowledge them?")
                self._echo_ids_freed.wait(left)
            echo_id = self._free_echo_ids.pop(0)
        try:
            self.device.write(self._ep_out, struct.pack("<I", echo_id) + frame[4:],
                              _milliseconds(max(deadline - time.monotonic(), 0.0)))
        except usb.core.USBError as exc:
            self._echoed(echo_id)  # not taken, so never echoed
            raise can.CanOperationError(f"{self.channel_info}: send failed: {exc}") from exc

    def _echoed(self, echo_id: int) -> None:
        """Give ``echo_id`` back for the next frame sent."""
        with self._echo_ids_freed:
            if 0 <= echo_id < TX_WINDOW and echo_id not in self._free_echo_ids:
                self._free_echo_ids.append(echo_id)
                self._echo_ids_freed.notify()

    def _recv_internal(self, timeout: Optional[float]) -> tuple[Optional[can.Message], bool]:
        if not self._pending:
            try:
                # To libusb a timeout of 0 means none.
                frame = self.device.read(self._ep_in, _MAX_FRAME_SIZE,
                                         0 if timeout is None else _milliseconds(timeout))
            except usb.core.USBTimeoutError:
                return None, False
            except usb.core.USBError as exc:
                raise can.CanOperationError(f"{self.channel_info}: receive failed: {exc}") from exc
            self._pending.extend(self._messages(bytes(frame)))
        return (self._pending.popleft() if self._pending else None), False

    def shutdown(self) -> None:
        if self._is_shutdown:
            return
        super().shutdown()
        try:
            self._set_mode(_MODE_RESET)  # off the bus
        except usb.core.USBError as exc:  # unplugged, most likely
            logger.debug("%s: could not stop the channel: %s", self.channel_info, exc)
        finally:
            # Now rather than once garbage-collected: WinUSB lets one handle
            # at a time open the device, and connecting again needs one.
            usb.util.dispose_resources(self.device)

    def _set_mode(self, mode: int, flags: int = 0) -> None:
        _control_out(self.device, _BREQ_MODE, _MODE.pack(mode, flags))

    def _frame(self, msg: can.Message) -> bytes:
        """``msg`` as the device takes it, echo ID 0 (send sets it): each frame sized for the
        channel's mode, CAN FD or Classic, as the Linux driver sends them."""
        data = bytes(msg.data)
        if msg.is_fd and not self._fd:
            raise ValueError(f"a CAN FD frame on {self.channel_info}, which runs Classic CAN")
        if len(data) > (_FD_DATA_SIZE if msg.is_fd else _CLASSIC_DATA_SIZE):
            raise ValueError(f"{len(data)} data bytes do not fit a {'CAN FD' if msg.is_fd else 'Classic CAN'} frame")
        can_id = msg.arbitration_id | (_CAN_EFF_FLAG if msg.is_extended_id else 0)
        flags = 0
        if msg.is_fd:
            dlc = len2dlc(len(data))  # the device pads the data up to the DLC's length
            flags = (_FLAG_FD | (_FLAG_BRS if msg.bitrate_switch else 0)
                     | (_FLAG_ESI if msg.error_state_indicator else 0))
        elif msg.is_remote_frame:
            can_id |= _CAN_RTR_FLAG
            dlc = msg.dlc
        else:
            dlc = len(data)
        header = _HEADER.pack(0, can_id, dlc, _CHANNEL, flags, 0)
        return header + data.ljust(_FD_DATA_SIZE if self._fd else _CLASSIC_DATA_SIZE, b"\0") + self._quirk

    def _messages(self, frame: bytes) -> list[can.Message]:
        """What a frame from the device reports: a frame off the bus, and an overflow."""
        if len(frame) < _HEADER.size:
            logger.debug("%s: %d-byte frame from the device dropped", self.channel_info, len(frame))
            return []
        echo_id, can_id, dlc, channel, flags, _ = _HEADER.unpack_from(frame)
        if channel != _CHANNEL:
            return []
        messages = []
        if echo_id == _ECHO_ID_RX:
            message = self._message(can_id, dlc, flags, frame[_HEADER.size:])
            if message is not None:
                messages.append(message)
        else:  # a frame sent is on the bus: its echo ID is free again
            self._echoed(echo_id)
        if flags & _FLAG_OVERFLOW:
            messages.append(can.Message(
                timestamp=time.time(), arbitration_id=_CAN_ERR_CRTL, is_extended_id=False,
                is_error_frame=True, data=bytes([0, _CAN_ERR_CRTL_RX_OVERFLOW, 0, 0, 0, 0, 0, 0]),
                channel=self.channel_info,
            ))
        return messages

    def _message(self, can_id: int, dlc: int, flags: int, payload: bytes) -> Optional[can.Message]:
        fd = bool(flags & _FLAG_FD)
        remote = not fd and bool(can_id & _CAN_RTR_FLAG)
        if fd:
            length = dlc2len(dlc & 0x0F)
        else:
            length = 0 if remote else min(dlc, 8)
        if len(payload) < length:
            logger.debug("%s: frame with %d of its %d data bytes dropped", self.channel_info, len(payload), length)
            return None
        return can.Message(
            timestamp=time.time(),
            arbitration_id=can_id & _CAN_EFF_MASK,
            is_extended_id=bool(can_id & _CAN_EFF_FLAG),
            is_remote_frame=remote,
            is_error_frame=bool(can_id & _CAN_ERR_FLAG),
            is_fd=fd,
            bitrate_switch=bool(flags & _FLAG_BRS),
            error_state_indicator=bool(flags & _FLAG_ESI),
            dlc=min(dlc, 8) if remote else None,
            data=payload[:length],
            channel=self.channel_info,
        )


def _scan() -> list:
    """The pyusb devices of every gs_usb adapter, in the order the gs_usb package lists them."""
    from gs_usb.gs_usb import GsUsb
    return [found.gs_usb for found in GsUsb.scan()]


def _bulk_endpoints(device) -> tuple[int, int, int]:
    """The device's bulk IN and OUT endpoint addresses, and the number of their interface.

    From its descriptors, as the Linux driver finds them: candleLight's are
    0x81 and 0x02, other firmware's need not be. Of several bulk OUT
    endpoints the last takes the frames: CANnectivity puts a dummy 0x01
    before its 0x02, which older Linux drivers expect there, and a frame
    written to the dummy is never taken.
    """
    interface = device.get_active_configuration()[(0, 0)]
    bulk = [endpoint.bEndpointAddress for endpoint in interface
            if endpoint.bmAttributes & _TRANSFER_TYPE_MASK == _TRANSFER_TYPE_BULK]
    ep_in = next((address for address in bulk if address & _ENDPOINT_IN), None)
    ep_out = next((address for address in reversed(bulk) if not address & _ENDPOINT_IN), None)
    if ep_in is None or ep_out is None:
        raise can.CanInitializationError("The gs_usb device has no bulk endpoints for CAN frames")
    return ep_in, ep_out, interface.bInterfaceNumber


def _phase_timing(brp: int, tseg1: int, tseg2: int, sjw: int) -> bytes:
    # The device adds prop_seg and phase_seg1 up again; split as the Linux driver splits them.
    prop_seg = tseg1 // 2
    return _TIMING.pack(prop_seg, tseg1 - prop_seg, tseg2, sjw, brp)


def _control_out(device, request: int, payload: bytes, value: int = _CHANNEL, index: int = 0) -> None:
    device.ctrl_transfer(_TO_DEVICE, request, value, index, payload, _CONTROL_TIMEOUT_MS)


def _control_in(device, request: int, length: int, value: int = _CHANNEL, index: int = 0) -> bytes:
    answer = bytes(device.ctrl_transfer(_TO_HOST, request, value, index, length, _CONTROL_TIMEOUT_MS))
    if len(answer) < length:
        raise can.CanInitializationError(
            f"The gs_usb device answered request {request} with {len(answer)} bytes, not {length}"
        )
    return answer


def _milliseconds(seconds: float) -> int:
    # At least 1: to libusb, 0 means wait for ever.
    return max(1, round(seconds * 1000))
