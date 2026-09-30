"""Offline feedback and acquisition checks; no instrument connections are opened."""

from __future__ import annotations

import csv
import math
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import runner
from instruments import SMUSample
from models import RunConfig


class FakeSMU:
    def __init__(self, *, shutdown_error: bool = False) -> None:
        self.connected = False
        self.configured = False
        self.started = False
        self.shutdown_calls = 0
        self.shutdown_error = shutdown_error
        self.configure_args: tuple[object, ...] = ()

    def connect(self) -> str:
        self.connected = True
        return "Keithley 2634B fake"

    def configure(self, *args: object) -> None:
        self.configured = True
        self.configure_args = args

    def start(self) -> None:
        self.started = True

    def read_next(self, timeout_s: float, stop_event: threading.Event | None = None) -> SMUSample:
        return SMUSample(1, -1e-6, 1e-9, 0.1, 0)

    def set_gate(self, voltage_v: float) -> float:
        return voltage_v

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.shutdown_error:
            raise RuntimeError("outputs off readback failed")


class FakeDMM:
    def __init__(self, *, configure_error: bool = False, read_error: bool = False) -> None:
        self.configure_error = configure_error
        self.read_error = read_error
        self.closed = False
        self.read_calls = 0

    def connect(self) -> str:
        return "Keithley DMM6500 fake"

    def configure(self) -> None:
        if self.configure_error:
            raise RuntimeError("DMM impedance incorrect")

    def read_vref(self) -> float:
        self.read_calls += 1
        if self.read_error:
            raise RuntimeError("DMM read failed")
        return -0.1

    def close(self) -> None:
        self.closed = True


class RunnerTests(unittest.TestCase):
    def test_config_defaults_and_feedback_bounds(self) -> None:
        config = RunConfig()
        self.assertEqual((config.smu_ip, config.dmm_ip), ("192.168.1.150", "192.168.1.160"))
        self.assertFalse(hasattr(config, "port"))
        self.assertEqual((config.drain_limit_a, config.gate_limit_a), (0.003, 0.003))
        self.assertEqual(runner.next_feedback_gate(0, -0.5, 0, config), -0.1)
        with self.assertRaisesRegex(ValueError, r"\.txt extension"):
            RunConfig(output_file="data.csv").validate()
        for polarity in (1, -1):
            with self.subTest(polarity=polarity):
                cfg = RunConfig(feedback_polarity=polarity)
                gate = 0.0
                for _ in range(80):
                    measured = polarity * 0.82 * gate
                    next_gate = runner.next_feedback_gate(gate, -0.5, measured, cfg)
                    self.assertLessEqual(abs(next_gate - gate), cfg.feedback_max_step_v + 1e-12)
                    self.assertLessEqual(cfg.gate_min_v, next_gate)
                    self.assertLessEqual(next_gate, cfg.gate_max_v)
                    gate = next_gate
                self.assertLessEqual(abs(-0.5 - polarity * 0.82 * gate), cfg.feedback_tolerance_v)

    def test_simulated_run_writes_six_columns_and_converges(self) -> None:
        for polarity in (1, -1):
            with self.subTest(polarity=polarity), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "data.txt"
                config = RunConfig(
                    feedback_polarity=polarity,
                    sweep_start_v=-0.05, sweep_stop_v=-0.05,
                    hold_s=0.8, delay_s=0, final_keep_s=0,
                    sample_interval_s=0.1, output_file=str(path),
                )
                samples = []
                result = runner.run_experiment(config, on_sample=samples.append)
                with path.open(newline="", encoding="utf-8") as stream:
                    data = csv.DictReader(stream, delimiter="\t")
                    self.assertEqual(data.fieldnames, ["Time", "GateI", "GateV", "DrainI", "DrainV", "Vref"])
                    rows = list(data)
                self.assertEqual(len(rows), result.samples)
                self.assertGreaterEqual(result.samples, 4)
                self.assertTrue(samples[-1].within_tolerance)
                self.assertTrue(all(math.isfinite(float(row["Vref"])) for row in rows))
                self.assertAlmostEqual(float(rows[0]["GateV"]), 0)
                self.assertLess(float(rows[-1]["GateV"]) * polarity, 0)
                self.assertTrue(all(sample.dmm_read_utc for sample in samples))

    def test_dmm_validation_precedes_smu_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            smu, dmm = FakeSMU(), FakeDMM(configure_error=True)
            config = RunConfig(simulation=False, output_file=str(Path(directory) / "data.txt"))
            with patch.object(runner, "Keithley2634B", return_value=smu), patch.object(
                runner, "DMM6500", return_value=dmm
            ):
                with self.assertRaisesRegex(RuntimeError, "impedance incorrect"):
                    runner.run_experiment(config)
            self.assertFalse(smu.connected)
            self.assertFalse(smu.started)
            self.assertTrue(dmm.closed)
            self.assertFalse(config.output_path.exists())

    def test_live_path_passes_current_limits_and_stops_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            smu, dmm = FakeSMU(), FakeDMM()
            stop = threading.Event()
            config = RunConfig(simulation=False, output_file=str(Path(directory) / "data.txt"))
            with patch.object(runner, "Keithley2634B", return_value=smu), patch.object(
                runner, "DMM6500", return_value=dmm
            ):
                result = runner.run_experiment(config, on_sample=lambda _: stop.set(), stop_event=stop)
            self.assertEqual(smu.configure_args, (
                config.vds_v, config.gate_initial_v, 0.003, 0.003, config.sample_interval_s
            ))
            self.assertTrue(result.stopped)
            self.assertEqual(result.samples, 1)
            self.assertEqual(smu.shutdown_calls, 1)
            self.assertTrue(dmm.closed)

    def test_dmm_read_error_or_unverified_shutdown_is_reported(self) -> None:
        for read_error, shutdown_error, expected in (
            (True, False, "DMM read failed"),
            (False, True, "2634B shutdown could not be verified"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                smu = FakeSMU(shutdown_error=shutdown_error)
                dmm = FakeDMM(read_error=read_error)
                stop = threading.Event()
                config = RunConfig(simulation=False, output_file=str(Path(directory) / "data.txt"))
                with patch.object(runner, "Keithley2634B", return_value=smu), patch.object(
                    runner, "DMM6500", return_value=dmm
                ):
                    with self.assertRaisesRegex(RuntimeError, expected):
                        runner.run_experiment(config, on_sample=lambda _: stop.set(), stop_event=stop)
                self.assertTrue(smu.started)
                self.assertEqual(smu.shutdown_calls, 1)
                self.assertTrue(dmm.closed)


if __name__ == "__main__":
    unittest.main()
