"""Tests for BusErrors: errors on the bus said once, and shown while they last."""

import logging

from bus_errors import QUIET_S, BusErrors

# What get_can_link_diagnostics finds on a candleLight can0: counters run from
# when the interface came up, so a healthy bus can show earlier errors.
HEALTHY = {"state": "ERROR-ACTIVE", "berr_tx": 0, "berr_rx": 0, "bus_errors": 0,
           "error_warning": 3024, "error_passive": 3023, "bus_off": 0, "restarts": 0}
PASSIVE = {**HEALTHY, "state": "ERROR-PASSIVE", "berr_rx": 128, "error_warning": 3060, "error_passive": 3059}


class TestBusErrors:
    def test_healthy_bus_has_none_and_says_nothing(self, caplog):
        errors = BusErrors("can0")
        with caplog.at_level(logging.INFO, logger="bus_errors"):
            for t in range(5):
                errors.observe(HEALTHY, now=t * 3.0)
        assert errors.status() is None and caplog.text == ""

    def test_controller_in_error_is_shown_and_said_once(self, caplog):
        errors = BusErrors("can0")
        errors.observe(HEALTHY, now=0.0)
        with caplog.at_level(logging.INFO, logger="bus_errors"):
            errors.observe(PASSIVE, now=3.0)
            errors.observe({**PASSIVE, "state": "ERROR-WARNING", "berr_rx": 126}, now=6.0)  # a burst ends
        assert errors.status() == {"state": "ERROR-WARNING", "tx_errors": 0, "rx_errors": 126, "since_unix": 3.0}
        [record] = caplog.records
        assert record.levelname == "WARNING"
        assert record.message.startswith(
            "CAN bus errors on can0: the controller is ERROR-PASSIVE (error counters TX 0, RX 128). "
            "Check the 120 Ω termination")

    def test_counters_growing_are_errors_until_quiet(self, caplog):
        # An adapter behind the hub reports no controller state: only error frames.
        errors = BusErrors("gs_usb:0")
        errors.observe({"adapter_error_frames": 0, "adapter_send_failures": 0}, now=0.0)
        errors.observe({"adapter_error_frames": 40, "adapter_send_failures": 0}, now=3.0)
        assert errors.status() == {"state": None, "tx_errors": None, "rx_errors": None, "since_unix": 3.0}
        errors.observe({"adapter_error_frames": 40, "adapter_send_failures": 0}, now=3.0 + QUIET_S - 1)
        assert errors.status() is not None, "Over before the counters were quiet long enough"
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="bus_errors"):
            errors.observe({"adapter_error_frames": 40, "adapter_send_failures": 0}, now=3.0 + QUIET_S)
        assert errors.status() is None
        assert [r.message for r in caplog.records] == ["CAN bus errors on gs_usb:0 have stopped"]

    def test_failed_sends_are_errors(self):
        # Nothing acknowledges: no other node, or none at this bitrate.
        errors = BusErrors("pcan:PCAN_USBBUS1")
        errors.observe({"adapter_error_frames": 0, "adapter_send_failures": 3}, now=0.0)
        errors.observe({"adapter_error_frames": 0, "adapter_send_failures": 9}, now=3.0)
        assert errors.status() is not None

    def test_counters_restarting_from_zero_are_no_errors(self):
        errors = BusErrors("can0")
        errors.observe(HEALTHY, now=0.0)
        errors.observe({**HEALTHY, "error_warning": 0, "error_passive": 0}, now=3.0)  # interface brought up again
        assert errors.status() is None

    def test_vcan_has_nothing_to_report(self):
        errors = BusErrors("vcan0")
        for t in (0.0, 3.0):
            errors.observe({"state": None, "berr_tx": None, "berr_rx": None, "bus_errors": None}, now=t)
        assert errors.status() is None


class TestPycyphalErrorFrameLines:
    """pycyphal's line per error-frame state bit is dropped; its other lines stay."""

    LOGGER = "pycyphal.transport.can.media.socketcan._socketcan"

    def test_state_lines_are_dropped(self, caplog):
        log = logging.getLogger(self.LOGGER)
        with caplog.at_level(logging.WARNING, logger=self.LOGGER):
            log.error("Error Tx Passive State on %s", "can0")
            log.error("Error Rx Passive State on %s", "can0")
            log.warning("Error Rx Warning State on %s", "can0")
            log.warning("Error Tx Warning State on %s", "can0")
            log.error("CAN Bus Off on %s", "can0")
            log.error("Error Rx Overflow State on %s", "can0")
            log.error({"not": "a format string"})  # anything can be logged; the filter must not raise
        assert [r.getMessage() for r in caplog.records] == ["Error Rx Overflow State on can0", "{'not': 'a format string'}"]
