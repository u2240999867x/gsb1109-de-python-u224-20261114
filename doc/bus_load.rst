.. _bus_load:

Bus Load Estimation
===================

Historical frame records only contain timestamps, identifiers, DLC and flags,
never the bit-level waveform. The exact number of dynamically inserted bit
stuffing bits is therefore unknown, so :mod:`can.bus_load` reports every
occupied-time estimate as an interval with lower and upper bounds.

Remote frames never carry data bytes regardless of their DLC, and CAN FD
frames using bit-rate switching need a data-phase bit rate; without it the
affected contribution is flagged as undetermined.

.. automodule:: can.bus_load
    :members:
    :show-inheritance:
