"""Errors on the CAN bus, said once: when they start, and when they stop.

A bus without its 120 Ω termination, or a node at another bitrate, makes the
CAN controller see errors: it leaves ERROR-ACTIVE, or its error counters grow.
pycyphal logs every error frame it reads, a line per state bit for each of
Cynitor's nodes: hundreds a second on such a bus, and nothing about why.
``BusErrors`` follows the controller's state and counters as the session's
health check samples them, logs a line when errors start and one when they
stop, and gives ``/api/status`` what the dashboard shows meanwhile.
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# A controller that sees errors (BUS-OFF ends the session, see main._check_can_health).
ERROR_STATES = ("ERROR-WARNING", "ERROR-PASSIVE", "BUS-OFF")
# Counters that grow with errors: SocketCAN's, from iproute2, and the CAN hub's.
ERROR_COUNTERS = ("bus_errors", "error_warning", "error_passive", "bus_off", "restarts",
                  "adapter_error_frames", "adapter_send_failures")
# Errors are over once the counters have not grown for this long.
QUIET_S = 10.0
HINT = ("Check the 120 Ω termination at each end of the bus, that every node runs "
        "the same bitrate, and the wiring.")

# pycyphal's SocketCAN media: a line for each of these in every error frame.
# BusErrors says it once instead; its other lines (overflows, timeouts) stay.
_ERROR_FRAME_LINES = frozenset((
    "Error Rx Warning State on %s", "Error Tx Warning State on %s",
    "Error Rx Passive State on %s", "Error Tx Passive State on %s", "CAN Bus Off on %s",
))
logging.getLogger("pycyphal.transport.can.media.socketcan._socketcan").addFilter(
    lambda record: not (isinstance(record.msg, str) and record.msg in _ERROR_FRAME_LINES))


def _grew(now: Optional[int], before: Optional[int]) -> bool:
    return now is not None and before is not None and now > before


class BusErrors:
    """The bus's errors, from samples of its link diagnostics."""

    def __init__(self, interface: str) -> None:
        self.interface = interface
        self._link: dict = {}                  # the last sample
        self._grew_at: Optional[float] = None  # when an error counter last grew
        self._since: Optional[float] = None    # when the errors began; None while there are none

    def observe(self, link: dict, now: Optional[float] = None) -> None:
        """One sample: what ``main.get_can_link_diagnostics`` or ``CANHub.link_diagnostics`` returns."""
        now = time.time() if now is None else now
        if any(_grew(link.get(name), self._link.get(name)) for name in ERROR_COUNTERS):
            self._grew_at = now
        self._link = link
        erroring = link.get("state") in ERROR_STATES or (
            self._grew_at is not None and now - self._grew_at < QUIET_S)
        if erroring and self._since is None:
            self._since = now
            logger.warning("CAN bus errors on %s: %s. %s", self.interface, self._said(), HINT)
        elif not erroring and self._since is not None:
            self._since = None
            logger.info("CAN bus errors on %s have stopped", self.interface)

    def status(self) -> Optional[dict]:
        """What ``/api/status`` reports: None while the bus has no errors."""
        if self._since is None:
            return None
        return {"state": self._link.get("state"), "tx_errors": self._link.get("berr_tx"),
                "rx_errors": self._link.get("berr_rx"), "since_unix": self._since}

    def _said(self) -> str:
        state, tx, rx = (self._link.get(name) for name in ("state", "berr_tx", "berr_rx"))
        counters = f" (error counters TX {tx}, RX {rx})" if tx is not None and rx is not None else ""
        return f"the controller is {state}{counters}" if state in ERROR_STATES else f"errors are reported{counters}"
