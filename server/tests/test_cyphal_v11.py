"""Telling Cyphal v1.1 transfers apart from v1.0 and DroneCAN frames."""

import can
import pytest

from cyphal_v11 import SOCKETCAN_FILTER, V11Traffic, is_v11_transfer_start

SINGLE_V1 = 0x80 | 0x40 | 0x20  # start, end, toggle: a whole Cyphal transfer in one frame
SINGLE_V0 = 0x80 | 0x40         # start, end, no toggle: DroneCAN


def v11(subject, source, tail=SINGLE_V1, priority=4):
    return can.Message(arbitration_id=(priority << 26) | (subject << 8) | (1 << 7) | source,
                       data=b"\x01\x02" + bytes([tail]))


def v10(subject, source):
    return can.Message(arbitration_id=(4 << 26) | (3 << 21) | (subject << 8) | source, data=bytes([SINGLE_V1]))


class TestClassification:
    def test_v11_message_starts(self):
        assert is_v11_transfer_start(v11(subject=0xABCD, source=42))
        first_of_several = can.Message(arbitration_id=(1620 << 8) | (1 << 7) | 1, data=bytes(7) + bytes([0x80 | 0x20]))
        assert is_v11_transfer_start(first_of_several)

    @pytest.mark.parametrize("msg", [
        v10(7509, 42),                                                   # a v1.0 heartbeat
        can.Message(arbitration_id=(1 << 25) | (1 << 24) | (430 << 14) | (5 << 7) | 42,
                    data=bytes([SINGLE_V1])),                            # a v1.0 service request
        v11(1620, 42, tail=SINGLE_V0),                                   # a DroneCAN service frame
        v11(1620, 42, tail=0x40),                                        # a v1.1 transfer's last frame
        v11(1620, 42, tail=0x80 | 0x20),                                 # a first frame, but not full
        can.Message(arbitration_id=0x80, is_extended_id=False, data=bytes([SINGLE_V1])),
        can.Message(arbitration_id=(1 << 7), data=b""),
        can.Message(arbitration_id=(1 << 7) | 42, is_error_frame=True, data=bytes([SINGLE_V1])),
    ])
    def test_others_are_not(self, msg):
        assert not is_v11_transfer_start(msg)

    def test_the_kernel_filter_passes_what_may_be_v11(self):
        [f] = SOCKETCAN_FILTER
        passes = lambda msg: msg.arbitration_id & f["can_mask"] == f["can_id"]  # noqa: E731
        assert passes(v11(1620, 42)) and passes(v11(1620, 42, tail=SINGLE_V0))
        assert not passes(v10(7509, 42))


class TestTraffic:
    def test_nothing_seen(self):
        assert V11Traffic().status() is None

    def test_counts_transfers_nodes_and_subjects(self, caplog):
        traffic = V11Traffic()
        for msg in (v11(100, 42), v11(100, 42, tail=0x40), v11(200, 43), v10(7509, 44)):
            traffic.observe(msg)
        status = traffic.status()
        assert status["transfers"] == 2
        assert status["nodes"] == [42, 43] and status["subject_ids"] == [100, 200]
        assert status["subject_count"] == 2 and status["last_seen_unix"] > 0
        assert sum("Cyphal v1.1 traffic" in r.message for r in caplog.records) == 1  # said once

    def test_subject_list_is_capped(self):
        traffic = V11Traffic()
        for subject in range(40):
            traffic.observe(v11(subject, 42))
        status = traffic.status()
        assert status["subject_count"] == 40 and len(status["subject_ids"]) == 16
