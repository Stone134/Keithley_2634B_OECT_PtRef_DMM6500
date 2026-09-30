"""Offline command and failure-path tests; no VISA device is opened."""

from __future__ import annotations

import sys
import threading
import types
import unittest
from unittest.mock import patch

import instruments


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeSMUIO:
    def __init__(self) -> None:
        self.connected = False
        self.writes: list[str] = []
        self.queries: list[str] = []
        self.fail_writes: set[str] = set()
        self.output = {"smua": 0, "smub": 0}
        self.level = {"smua": 0.0, "smub": 0.0}
        self.limit = {"smua": 0.003, "smub": 0.003}
        self.current_response = "1e-6\t-2e-8\tfalse\tfalse"
        self.source_flags = "true\ttrue\ttrue\ttrue\t1"
        self.pair_queries = 0

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def write(self, command: str) -> None:
        self.writes.append(command)
        if command in self.fail_writes:
            raise instruments.InstrumentError(f"fake write failure: {command}")
        for channel in ("smua", "smub"):
            if command.startswith(f"{channel}.source.levelv = "):
                self.level[channel] = float(command.split(" = ", 1)[1])
            elif command.startswith(f"{channel}.source.limiti = "):
                self.limit[channel] = float(command.split(" = ", 1)[1])
            elif command == f"{channel}.source.output = {channel}.OUTPUT_ON":
                self.output[channel] = 1
            elif command == f"{channel}.source.output = {channel}.OUTPUT_OFF":
                self.output[channel] = 0

    def query(self, command: str, *, timeout_s: float | None = None) -> str:
        self.queries.append(command)
        if command == "*IDN?":
            return "KEITHLEY INSTRUMENTS,MODEL 2634B,12345,1.0"
        if command.startswith("print(smua.measure.i()"):
            self.pair_queries += 1
            return self.current_response
        if command.startswith("print(smua.source.func"):
            channel = "smua"
            return (f"true\t{str(self.output[channel] == 0).lower()}\t"
                    f"{self.level[channel]}\t{self.limit[channel]}\t"
                    f"{self.source_flags}")
        if command.startswith("print(smub.source.func"):
            channel = "smub"
            return (f"true\t{str(self.output[channel] == 0).lower()}\t"
                    f"{self.level[channel]}\t{self.limit[channel]}\t"
                    f"{self.source_flags}")
        if command == "print(smua.source.output == smua.OUTPUT_ON, smub.source.output == smub.OUTPUT_ON)":
            return (f"{str(self.output['smua'] == 1).lower()}\t"
                    f"{str(self.output['smub'] == 1).lower()}")
        if command == "print(smua.source.output, smub.source.output)":
            return f"{self.output['smua']}\t{self.output['smub']}"
        if command == "print(smub.source.levelv)":
            return str(self.level["smub"])
        raise AssertionError(f"Unexpected SMU query {command!r}")


class FakeDMMIO:
    def __init__(self) -> None:
        self.connected = False
        self.writes: list[str] = []
        self.queries: list[str] = []
        self.language = "SCPI"
        self.input_impedance = "AUTO"
        self.reading = "0.123456"

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def write(self, command: str) -> None:
        self.writes.append(command)

    def query(self, command: str, *, timeout_s: float | None = None) -> str:
        self.queries.append(command)
        replies = {
            "*IDN?": "KEITHLEY INSTRUMENTS,DMM6500,5678,1.0",
            "*LANG?": self.language,
            ":ROUT:TERM?": "FRON",
            ":SENS:FUNC?": '"VOLT:DC"',
            ":SENS:VOLT:DC:RANG:AUTO?": "0",
            ":SENS:VOLT:DC:RANG?": "10",
            ":SENS:VOLT:DC:INP?": self.input_impedance,
            ":FORM:DATA?": "ASC",
            ":SENS:VOLT:DC:AVER?": "0",
            ":SENS:VOLT:DC:REL:STAT?": "0",
            ":CALC:VOLT:DC:MATH:STAT?": "0",
            ":CALC2:VOLT:DC:LIM1:STAT?": "0",
            ":CALC2:VOLT:DC:LIM2:STAT?": "0",
            ":SYST:ERR?": '0,"No error"',
            ":READ?": self.reading,
        }
        try:
            return replies[command]
        except KeyError as exc:
            raise AssertionError(f"Unexpected DMM query {command!r}") from exc


class DriverTests(unittest.TestCase):
    def make_smu(self) -> tuple[instruments.Keithley2634B, FakeSMUIO]:
        io = FakeSMUIO()
        with patch.object(instruments, "_VisaTransport", return_value=io):
            smu = instruments.Keithley2634B("192.168.1.150")
        self.assertIn("2634B", smu.connect())
        return smu, io

    def prepare_smu(self) -> tuple[instruments.Keithley2634B, FakeSMUIO]:
        smu, io = self.make_smu()
        smu.configure(-0.01, -0.2, 0.003, 0.002, 0.1)
        return smu, io

    def test_vxi11_resource_and_pyvisa_py_backend(self) -> None:
        class Resource:
            def __init__(self) -> None:
                self.timeout = 0
                self.write_termination = ""
                self.read_termination = ""
                self.closed = False

            def query(self, command: str) -> str:
                return "reply\n"

            def write(self, command: str) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        class Manager:
            def __init__(self) -> None:
                self.resources: list[str] = []
                self.resource = Resource()
                self.closed = False

            def open_resource(self, name: str) -> Resource:
                self.resources.append(name)
                return self.resource

            def close(self) -> None:
                self.closed = True

        manager = Manager()
        backends: list[str] = []

        def resource_manager(backend: str) -> Manager:
            backends.append(backend)
            return manager

        pyvisa = types.SimpleNamespace(ResourceManager=resource_manager)
        with patch.dict(sys.modules, {"pyvisa": pyvisa}):
            io = instruments._VisaTransport("192.168.1.150", 2.5)
            io.connect()
            self.assertEqual(io.query("*IDN?", timeout_s=0.25), "reply")
            self.assertEqual(manager.resource.timeout, 2500)
            io.close()
        self.assertEqual(backends, ["@py"])
        self.assertEqual(manager.resources, ["TCPIP0::192.168.1.150::inst0::INSTR"])
        self.assertEqual(manager.resource.write_termination, "\n")
        self.assertEqual(manager.resource.read_termination, "\n")
        self.assertTrue(manager.resource.closed)
        self.assertTrue(manager.closed)

    def test_smu_configure_start_pair_gate_and_shutdown(self) -> None:
        clock = FakeClock()
        with patch.object(instruments, "time", clock):
            smu, io = self.prepare_smu()
            for channel in ("smua", "smub"):
                self.assertIn(f"{channel}.sense = {channel}.SENSE_LOCAL", io.writes)
                self.assertIn(
                    f"{channel}.source.autorangev = {channel}.AUTORANGE_ON", io.writes
                )
                self.assertIn(
                    f"{channel}.source.offmode = {channel}.OUTPUT_HIGH_Z", io.writes
                )
            self.assertEqual(io.output, {"smua": 0, "smub": 0})
            smu.start()
            sample = smu.read_next(timeout_s=1)
            self.assertEqual(sample.index, 0)
            self.assertAlmostEqual(sample.drain_i_a, 1e-6)
            self.assertAlmostEqual(sample.gate_i_a, -2e-8)
            self.assertAlmostEqual(sample.nominal_elapsed_s, 0.1)
            self.assertEqual(sample.skipped, 0)
            self.assertEqual(io.pair_queries, 1)
            self.assertAlmostEqual(smu.set_gate(-0.25), -0.25)
            smu.shutdown()
        self.assertEqual(io.output, {"smua": 0, "smub": 0})
        self.assertEqual(io.level, {"smua": 0.0, "smub": 0.0})
        self.assertFalse(io.connected)

    def test_smu_reports_late_ticks_without_bursting_queries(self) -> None:
        clock = FakeClock()
        with patch.object(instruments, "time", clock):
            smu, io = self.prepare_smu()
            smu.start()
            clock.now = 0.35
            late = smu.read_next(timeout_s=1)
            self.assertEqual((late.index, late.skipped), (2, 2))
            first_started = clock.now
            next_sample = smu.read_next(timeout_s=1)
            self.assertGreaterEqual(clock.now - first_started, 0.1 - 1e-10)
            self.assertEqual((next_sample.index, next_sample.skipped), (3, 0))
            self.assertEqual(io.pair_queries, 2)
            smu.shutdown()

    def test_smu_wait_interrupts_before_query(self) -> None:
        clock = FakeClock()
        with patch.object(instruments, "time", clock):
            smu, io = self.prepare_smu()
            smu.start()
            stop = threading.Event()
            stop.set()
            with self.assertRaises(InterruptedError):
                smu.read_next(timeout_s=1, stop_event=stop)
            self.assertEqual(io.pair_queries, 0)
            smu.shutdown()

    def test_compliance_and_invalid_source_readback_rejected(self) -> None:
        smu, io = self.make_smu()
        io.source_flags = "true\tfalse\ttrue\ttrue\t1"
        with self.assertRaisesRegex(instruments.InstrumentError, "sense mode"):
            smu.configure(-0.01, -0.2, 0.003, 0.002, 0.1)
        smu.shutdown()

        clock = FakeClock()
        with patch.object(instruments, "time", clock):
            smu, io = self.prepare_smu()
            smu.start()
            io.current_response = "1e-6\t-2e-8\ttrue\tfalse"
            with self.assertRaisesRegex(instruments.InstrumentError, "compliance"):
                smu.read_next(timeout_s=1)
            smu.shutdown()

    def test_shutdown_still_turns_off_after_zero_write_failure(self) -> None:
        smu, io = self.prepare_smu()
        smu.start()
        io.fail_writes.add("smub.source.levelv = 0")
        with self.assertRaisesRegex(instruments.InstrumentError, "smub zero"):
            smu.shutdown()
        self.assertEqual(io.output, {"smua": 0, "smub": 0})
        self.assertFalse(io.connected)

    def test_dmm_high_impedance_and_recheck_each_read(self) -> None:
        io = FakeDMMIO()
        with patch.object(instruments, "_VisaTransport", return_value=io):
            dmm = instruments.DMM6500("192.168.1.160")
        self.assertIn("DMM6500", dmm.connect())
        dmm.configure()
        self.assertIn(":SENS:VOLT:DC:RANG 10", io.writes)
        self.assertIn(":SENS:VOLT:DC:INP AUTO", io.writes)
        self.assertNotIn("*RST", io.writes)
        self.assertAlmostEqual(dmm.read_vref(), 0.123456)
        io.input_impedance = "10MOHM"
        with self.assertRaisesRegex(instruments.InstrumentError, "impedance"):
            dmm.read_vref()
        dmm.close()

    def test_probe_is_read_only(self) -> None:
        smu_io, dmm_io = FakeSMUIO(), FakeDMMIO()
        with patch.object(instruments, "_VisaTransport", side_effect=[smu_io, dmm_io]):
            summary = instruments.probe_links("192.168.1.150", "192.168.1.160")
        self.assertIn("2634B", summary)
        self.assertIn("DMM6500", summary)
        self.assertEqual(smu_io.writes, [])
        self.assertEqual(dmm_io.writes, [])
        self.assertFalse(smu_io.connected)
        self.assertFalse(dmm_io.connected)

    def test_probe_reports_non_scpi_meter_without_sending_scpi_queries(self) -> None:
        smu_io, dmm_io = FakeSMUIO(), FakeDMMIO()
        dmm_io.language = "TSP"
        with patch.object(instruments, "_VisaTransport", side_effect=[smu_io, dmm_io]):
            summary = instruments.probe_links("192.168.1.150", "192.168.1.160")
        self.assertIn("command set: TSP", summary)
        self.assertNotIn(":ROUT:TERM?", dmm_io.queries)
        self.assertNotIn(":SENS:VOLT:DC:INP?", dmm_io.queries)


if __name__ == "__main__":
    unittest.main()
