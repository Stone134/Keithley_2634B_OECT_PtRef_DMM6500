"""VISA drivers for a two-channel Keithley 2634B and reference DMM6500.

The 2634B uses TSP over a VXI-11 ``inst0::INSTR`` VISA resource.  Drain and
gate currents are requested in one TSP transaction, but the two measurements
are sequential and the DMM reading is separate.  The host schedules samples;
the three readings are not hardware synchronized.  Importing this module does
not contact either instrument.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass


class InstrumentError(RuntimeError):
    """A connection, instrument-state, or measurement error."""


@dataclass(frozen=True)
class SMUSample:
    index: int
    drain_i_a: float
    gate_i_a: float
    nominal_elapsed_s: float
    skipped: int


_FLOAT = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise InstrumentError("Instrument setpoint must be finite")
    return f"{value:.12g}"


def _first_float(response: str, *, label: str) -> float:
    match = _FLOAT.search(response)
    if match is None:
        raise InstrumentError(f"No numeric {label} in instrument response {response!r}")
    value = float(match.group())
    if not math.isfinite(value):
        raise InstrumentError(f"Invalid {label} in instrument response {response!r}")
    return value


def _tsp_fields(response: str, count: int) -> list[str]:
    # TSP print(a, b) separates the values with tabs.  Split on whitespace as
    # well, since VISA backends can normalize the line ending differently.
    fields = response.strip().split()
    if len(fields) != count:
        raise InstrumentError(f"Expected {count} TSP values, received {response!r}")
    return fields


def _tsp_bool(value: str, *, label: str) -> bool:
    if value.lower() not in {"true", "false"}:
        raise InstrumentError(f"Invalid {label} flag from 2634B: {value!r}")
    return value.lower() == "true"


def _tsp_float(value: str, *, label: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise InstrumentError(f"Invalid {label} from 2634B: {value!r}") from exc
    if not math.isfinite(result):
        raise InstrumentError(f"Invalid {label} from 2634B: {value!r}")
    return result


class _VisaTransport:
    """One VXI-11 VISA instrument session, with LF TSP/SCPI termination."""

    def __init__(self, host: str, timeout_s: float) -> None:
        host = host.strip()
        if not host or "::" in host or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise InstrumentError("Invalid instrument IP address or VISA timeout")
        self.host = host
        self.timeout_s = timeout_s
        self.resource_name = f"TCPIP0::{host}::inst0::INSTR"
        self._manager = None
        self._resource = None

    @property
    def connected(self) -> bool:
        return self._resource is not None

    def connect(self) -> None:
        if self.connected:
            raise InstrumentError(f"Already connected to {self.resource_name}")
        try:
            import pyvisa

            self._manager = pyvisa.ResourceManager("@py")
            self._resource = self._manager.open_resource(self.resource_name)
            self._resource.timeout = max(1, math.ceil(self.timeout_s * 1000))
            self._resource.write_termination = "\n"
            self._resource.read_termination = "\n"
        except Exception as exc:
            self.close()
            raise InstrumentError(f"Cannot open {self.resource_name}: {exc}") from exc

    def close(self) -> None:
        resource, self._resource = self._resource, None
        manager, self._manager = self._manager, None
        for item in (resource, manager):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    pass

    def write(self, command: str) -> None:
        if self._resource is None:
            raise InstrumentError(f"Not connected to {self.resource_name}")
        if not command or "\n" in command or "\r" in command:
            raise InstrumentError("Instrument command must be one nonempty line")
        try:
            self._resource.write(command)
        except Exception as exc:
            # A failed write does not leave a pending response.  Keep the
            # session available so shutdown can still try output-off commands.
            raise InstrumentError(f"Failed sending {command!r} to {self.host}: {exc}") from exc

    def query(self, command: str, *, timeout_s: float | None = None) -> str:
        if self._resource is None:
            raise InstrumentError(f"Not connected to {self.resource_name}")
        if not command or "\n" in command or "\r" in command:
            raise InstrumentError("Instrument command must be one nonempty line")
        if timeout_s is not None and (not math.isfinite(timeout_s) or timeout_s <= 0):
            raise InstrumentError("VISA query timeout must be positive and finite")
        resource = self._resource
        previous_timeout = resource.timeout
        try:
            if timeout_s is not None:
                resource.timeout = max(1, math.ceil(timeout_s * 1000))
            return resource.query(command).strip()
        except Exception as exc:
            # A timed-out query can leave its answer pending.  Closing avoids
            # confusing that answer with a later readback or safety check.
            self.close()
            raise InstrumentError(f"Failed reading {command!r} from {self.host}: {exc}") from exc
        finally:
            if self._resource is resource:
                resource.timeout = previous_timeout


class Keithley2634B:
    """smua drains the OECT; smub sources the gate voltage."""

    def __init__(self, host: str, timeout_s: float = 5) -> None:
        self.io = _VisaTransport(host, timeout_s)
        self.identity = ""
        self.interval_s = 0.0
        self._configured = False
        self._started = False
        self._outputs_may_be_on = False
        self._start_monotonic = 0.0
        self._last_returned_index = -1
        self._last_acquired_at: float | None = None

    def connect(self) -> str:
        self.io.connect()
        try:
            identity = self.io.query("*IDN?")
            if "2634B" not in identity.upper():
                raise InstrumentError(f"Expected a Keithley 2634B, received {identity!r}")
            self.identity = identity
            return identity
        except Exception:
            self.io.close()
            raise

    def _verify_source(self, channel: str, voltage_v: float, limit_a: float) -> None:
        fields = _tsp_fields(self.io.query(
            f"print({channel}.source.func == {channel}.OUTPUT_DCVOLTS, "
            f"{channel}.source.output == {channel}.OUTPUT_OFF, "
            f"{channel}.source.levelv, {channel}.source.limiti, "
            f"{channel}.source.offmode == {channel}.OUTPUT_HIGH_Z, "
            f"{channel}.sense == {channel}.SENSE_LOCAL, "
            f"{channel}.source.autorangev == {channel}.AUTORANGE_ON, "
            f"{channel}.measure.autorangei == {channel}.AUTORANGE_ON, "
            f"{channel}.measure.nplc)"
        ), 9)
        if not _tsp_bool(fields[0], label=f"{channel} voltage-source"):
            raise InstrumentError(f"2634B {channel} is not in voltage-source mode")
        if not _tsp_bool(fields[1], label=f"{channel} output"):
            raise InstrumentError(f"2634B {channel} output is on during setup")
        read_voltage = _tsp_float(fields[2], label=f"{channel} source voltage")
        read_limit = _tsp_float(fields[3], label=f"{channel} current limit")
        if not math.isclose(read_voltage, voltage_v, abs_tol=0.00011):
            raise InstrumentError(
                f"2634B {channel} source voltage readback {read_voltage:g} V "
                f"differs from {voltage_v:g} V"
            )
        if not math.isclose(read_limit, limit_a, rel_tol=0.005, abs_tol=1e-8):
            raise InstrumentError(
                f"2634B {channel} current limit readback {read_limit:g} A "
                f"differs from {limit_a:g} A"
            )
        if not _tsp_bool(fields[4], label=f"{channel} high-Z off mode"):
            raise InstrumentError(f"2634B {channel} output-off mode is not high impedance")
        if not _tsp_bool(fields[5], label=f"{channel} sense mode"):
            raise InstrumentError(f"2634B {channel} is not in local (2-wire) sense mode")
        if not _tsp_bool(fields[6], label=f"{channel} source autorange"):
            raise InstrumentError(f"2634B {channel} source voltage autorange is off")
        if not _tsp_bool(fields[7], label=f"{channel} measure autorange"):
            raise InstrumentError(f"2634B {channel} current measure autorange is off")
        nplc = _tsp_float(fields[8], label=f"{channel} NPLC")
        if not math.isclose(nplc, 1.0, abs_tol=1e-6):
            raise InstrumentError(f"2634B {channel} NPLC is {nplc:g}, expected 1")

    def configure(
        self,
        vds_v: float,
        gate_initial_v: float,
        drain_limit_a: float,
        gate_limit_a: float,
        interval_s: float,
    ) -> None:
        if not self.io.connected or not self.identity:
            raise InstrumentError("Connect to 2634B before configuring")
        if self._started:
            raise InstrumentError("2634B is already sampling")
        values = (vds_v, gate_initial_v, drain_limit_a, gate_limit_a, interval_s)
        if not all(math.isfinite(value) for value in values):
            raise InstrumentError("2634B configuration contains a nonfinite value")
        if abs(vds_v) > 3 or abs(gate_initial_v) > 3:
            raise InstrumentError("2634B source voltages must be within ±3 V")
        if not (0 < drain_limit_a <= 0.5 and 0 < gate_limit_a <= 0.5):
            raise InstrumentError("2634B current limits must be above 0 and at most 0.5 A")
        if not 0.1 <= interval_s <= 60:
            raise InstrumentError("2634B sample interval must be 0.1 to 60 s")

        self._configured = False
        self._outputs_may_be_on = True
        # Put both outputs in high-Z before changing their source function or
        # levels.  Do not reset: another program might have left output on.
        for channel in ("smub", "smua"):
            self.io.write(f"{channel}.source.offmode = {channel}.OUTPUT_HIGH_Z")
            self.io.write(f"{channel}.source.output = {channel}.OUTPUT_OFF")
            self.io.write(f"{channel}.source.levelv = 0")
        for channel, voltage_v, limit_a in (
            ("smua", vds_v, drain_limit_a),
            ("smub", gate_initial_v, gate_limit_a),
        ):
            for command in (
                f"{channel}.source.offmode = {channel}.OUTPUT_HIGH_Z",
                f"{channel}.sense = {channel}.SENSE_LOCAL",
                f"{channel}.source.func = {channel}.OUTPUT_DCVOLTS",
                f"{channel}.source.autorangev = {channel}.AUTORANGE_ON",
                f"{channel}.source.limiti = {_number(limit_a)}",
                f"{channel}.source.levelv = {_number(voltage_v)}",
                f"{channel}.measure.autorangei = {channel}.AUTORANGE_ON",
                f"{channel}.measure.nplc = 1",
            ):
                self.io.write(command)
            self._verify_source(channel, voltage_v, limit_a)
        self.interval_s = interval_s
        self._last_returned_index = -1
        self._last_acquired_at = None
        self._configured = True

    def start(self) -> None:
        if not self._configured or self._started:
            raise InstrumentError("Configure 2634B before starting")
        self._outputs_may_be_on = True
        self.io.write("smua.source.output = smua.OUTPUT_ON")
        self.io.write("smub.source.output = smub.OUTPUT_ON")
        fields = _tsp_fields(self.io.query(
            "print(smua.source.output == smua.OUTPUT_ON, "
            "smub.source.output == smub.OUTPUT_ON)"
        ), 2)
        if not all(_tsp_bool(value, label="2634B output") for value in fields):
            raise InstrumentError("2634B outputs did not both enter ON state")
        self._start_monotonic = time.monotonic()
        self._last_returned_index = -1
        self._last_acquired_at = None
        self._started = True

    def read_next(self, timeout_s: float, stop_event=None) -> SMUSample:
        if not self._started:
            raise InstrumentError("2634B sampling has not started")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise InstrumentError("2634B read timeout must be positive and finite")
        deadline = time.monotonic() + timeout_s
        next_tick = self._last_returned_index + 2
        due = self._start_monotonic + next_tick * self.interval_s
        if self._last_acquired_at is not None:
            due = max(due, self._last_acquired_at + self.interval_s)
        while True:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("2634B sample wait was stopped")
            remaining = due - time.monotonic()
            if remaining <= 0:
                break
            if time.monotonic() + remaining > deadline:
                raise InstrumentError("Timed out waiting for the next 2634B sample")
            wait_s = min(0.02, remaining)
            if stop_event is None:
                time.sleep(wait_s)
            elif stop_event.wait(wait_s):
                raise InterruptedError("2634B sample wait was stopped")
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("2634B sample wait was stopped")
        acquired_at = time.monotonic()
        query_timeout = deadline - acquired_at
        if query_timeout <= 0:
            raise InstrumentError("Timed out waiting for the next 2634B sample")
        raw = self.io.query(
            "print(smua.measure.i(), smub.measure.i(), "
            "smua.source.compliance, smub.source.compliance)",
            timeout_s=query_timeout,
        )
        fields = _tsp_fields(raw, 4)
        drain = _tsp_float(fields[0], label="drain current")
        gate = _tsp_float(fields[1], label="gate current")
        if abs(drain) >= 8e30 or abs(gate) >= 8e30 or abs(drain) > 0.5 or abs(gate) > 0.5:
            raise InstrumentError(f"2634B returned invalid current pair {raw!r}")
        if _tsp_bool(fields[2], label="drain compliance") or _tsp_bool(
            fields[3], label="gate compliance"
        ):
            raise InstrumentError("2634B current compliance reached; stop and inspect OECT wiring")
        tick = max(next_tick, int((acquired_at - self._start_monotonic) / self.interval_s + 1e-9))
        index = tick - 1
        sample = SMUSample(
            index=index,
            drain_i_a=drain,
            gate_i_a=gate,
            nominal_elapsed_s=tick * self.interval_s,
            skipped=index - self._last_returned_index - 1,
        )
        self._last_returned_index = index
        self._last_acquired_at = acquired_at
        return sample

    def set_gate(self, voltage_v: float) -> float:
        if not self._configured or not math.isfinite(voltage_v) or abs(voltage_v) > 3:
            raise InstrumentError("2634B gate setpoint must be finite and within ±3 V")
        self.io.write(f"smub.source.levelv = {_number(voltage_v)}")
        actual = _first_float(
            self.io.query("print(smub.source.levelv)"), label="2634B gate voltage"
        )
        if not math.isclose(actual, voltage_v, abs_tol=0.00011):
            raise InstrumentError(
                f"2634B gate readback {actual:g} V differs from request {voltage_v:g} V"
            )
        return actual

    def shutdown(self) -> None:
        """Set both sources to zero, open their relays, verify, then close VISA."""
        errors: list[str] = []
        if self._outputs_may_be_on and not self.io.connected:
            errors.append("VISA connection was lost while output may be on; verify both 2634B outputs")
        if self.io.connected:
            for channel in ("smub", "smua"):
                try:
                    self.io.write(f"{channel}.source.levelv = 0")
                except Exception as exc:
                    errors.append(f"{channel} zero: {exc}")
            for channel in ("smub", "smua"):
                try:
                    self.io.write(f"{channel}.source.offmode = {channel}.OUTPUT_HIGH_Z")
                except Exception as exc:
                    errors.append(f"{channel} high-Z off mode: {exc}")
                try:
                    self.io.write(f"{channel}.source.output = {channel}.OUTPUT_OFF")
                except Exception as exc:
                    errors.append(f"{channel} output off: {exc}")
            if self.io.connected:
                try:
                    fields = _tsp_fields(self.io.query(
                        "print(smua.source.output, smub.source.output)"
                    ), 2)
                    if any(_tsp_float(field, label="output state") != 0 for field in fields):
                        errors.append("2634B output-off readback was not 0 on both channels")
                except Exception as exc:
                    errors.append(f"output-off verification: {exc}")
        self.io.close()
        self._started = False
        self._configured = False
        self._outputs_may_be_on = False
        if errors:
            raise InstrumentError("2634B shutdown incomplete: " + "; ".join(errors))

    def close(self) -> None:
        if self._outputs_may_be_on:
            self.shutdown()
            return
        self.io.close()
        self._started = False
        self._configured = False


class DMM6500:
    """Passive reference-minus-source DC-voltage reader using FRONT terminals."""

    def __init__(self, host: str, timeout_s: float = 5) -> None:
        self.io = _VisaTransport(host, timeout_s)
        self.identity = ""
        self._configured = False

    def connect(self) -> str:
        self.io.connect()
        try:
            identity = self.io.query("*IDN?")
            if "DMM6500" not in identity.upper():
                raise InstrumentError(f"Expected a Keithley DMM6500, received {identity!r}")
            self.identity = identity
            return identity
        except Exception:
            self.io.close()
            raise

    def _require_scpi_front(self) -> None:
        language = self.io.query("*LANG?").strip().upper()
        if language != "SCPI":
            raise InstrumentError(
                f"DMM6500 command set is {language!r}; set SCPI on the meter and reboot"
            )
        terminals = self.io.query(":ROUT:TERM?").strip().upper()
        if terminals != "FRON":
            raise InstrumentError(
                f"DMM6500 terminal selection is {terminals!r}; "
                "use the front-panel TERMINALS button to select FRONT"
            )

    def _verify_config(self) -> None:
        self._require_scpi_front()
        function = self.io.query(":SENS:FUNC?").strip().strip('"\'').upper()
        if function not in {"VOLT", "VOLT:DC", "VOLTAGE", "VOLTAGE:DC"}:
            raise InstrumentError(f"DMM6500 is not in DC-voltage mode: {function!r}")
        auto_range = self.io.query(":SENS:VOLT:DC:RANG:AUTO?").strip().upper()
        if auto_range not in {"0", "OFF"}:
            raise InstrumentError(f"DMM6500 autorange unexpectedly enabled: {auto_range!r}")
        voltage_range = _first_float(
            self.io.query(":SENS:VOLT:DC:RANG?"), label="DMM6500 voltage range"
        )
        if not math.isclose(voltage_range, 10.0, abs_tol=1e-6):
            raise InstrumentError(f"DMM6500 range is {voltage_range:g} V, expected 10 V")
        impedance = self.io.query(":SENS:VOLT:DC:INP?").strip().strip('"').upper()
        if impedance != "AUTO":
            raise InstrumentError(
                f"DMM6500 input impedance is {impedance!r}, expected AUTO (>10 GΩ)"
            )
        data_format = self.io.query(":FORM:DATA?").strip().strip('"').upper()
        if data_format not in {"ASC", "ASCII"}:
            raise InstrumentError(f"DMM6500 output format is not ASCII: {data_format!r}")
        for command, label in (
            (":SENS:VOLT:DC:AVER?", "averaging"),
            (":SENS:VOLT:DC:REL:STAT?", "relative offset"),
            (":CALC:VOLT:DC:MATH:STAT?", "math operation"),
            (":CALC2:VOLT:DC:LIM1:STAT?", "limit test 1"),
            (":CALC2:VOLT:DC:LIM2:STAT?", "limit test 2"),
        ):
            state = self.io.query(command).strip().upper()
            if state not in {"0", "OFF"}:
                raise InstrumentError(f"DMM6500 {label} is unexpectedly on: {state!r}")

    def configure(self) -> None:
        if not self.io.connected or not self.identity:
            raise InstrumentError("Connect to DMM6500 before configuring")
        self._require_scpi_front()
        # A reset can briefly restore a 10 MΩ input and load the electrode.
        # Abort a previous scan and program a single reading explicitly.
        self.io.write(":ABORt")
        self.io.write("*CLS")
        for command in (
            ':SENS:FUNC "VOLT:DC"',
            ":SENS:VOLT:DC:RANG:AUTO OFF",
            ":SENS:VOLT:DC:RANG 10",
            ":SENS:VOLT:DC:INP AUTO",
            ":SENS:VOLT:DC:NPLC 1",
            ":SENS:VOLT:DC:AVER OFF",
            ":SENS:VOLT:DC:REL:STAT OFF",
            ":CALC:VOLT:DC:MATH:STAT OFF",
            ":CALC2:VOLT:DC:LIM1:STAT OFF",
            ":CALC2:VOLT:DC:LIM2:STAT OFF",
            ":FORM:DATA ASC",
            ':TRIG:LOAD "SimpleLoop", 1, 0',
        ):
            self.io.write(command)
        self._verify_config()
        error_reply = self.io.query(":SYST:ERR?")
        if _first_float(error_reply, label="DMM6500 error code") != 0:
            raise InstrumentError(f"DMM6500 rejected a setup command: {error_reply!r}")
        self._configured = True

    def read_vref(self) -> float:
        """Return INPUT HI (reference) minus INPUT LO (OECT source), in volts."""
        if not self._configured:
            raise InstrumentError("DMM6500 is not configured for reference sensing")
        self._verify_config()
        self.io.write(":ABORt")
        self.io.write(':TRIG:LOAD "SimpleLoop", 1, 0')
        raw = self.io.query(":READ?")
        first_field = raw.split(",", 1)[0].strip()
        try:
            value = float(first_field)
        except ValueError as exc:
            raise InstrumentError(f"Invalid DMM6500 reference reading {raw!r}") from exc
        if not math.isfinite(value) or abs(value) >= 1e35 or abs(value) > 10.1:
            raise InstrumentError(f"DMM6500 reference reading is overrange/invalid: {raw!r}")
        return value

    def close(self) -> None:
        self.io.close()
        self._configured = False


def probe_links(smu_host: str, dmm_host: str) -> str:
    """Read identities and present states without changing either instrument."""
    smu = Keithley2634B(smu_host, timeout_s=3)
    dmm = DMM6500(dmm_host, timeout_s=3)
    try:
        smu_id = smu.connect()
        smu_outputs = _tsp_fields(smu.io.query(
            "print(smua.source.output, smub.source.output)"
        ), 2)
        dmm_id = dmm.connect()
        language = dmm.io.query("*LANG?").strip()
        if language.upper() == "SCPI":
            terminals = dmm.io.query(":ROUT:TERM?").strip()
            impedance = dmm.io.query(":SENS:VOLT:DC:INP?").strip()
        else:
            terminals = "unavailable until SCPI is selected"
            impedance = "unavailable until SCPI is selected"
        return (
            f"Keithley 2634B: {smu_id}\n"
            f"SMU outputs (drain/gate): {smu_outputs[0]}/{smu_outputs[1]} "
            "(0=off, 1=on)\n"
            f"DMM6500: {dmm_id}\n"
            f"DMM command set: {language}; terminals: {terminals}; "
            f"DCV input impedance: {impedance}"
        )
    finally:
        dmm.close()
        smu.close()
