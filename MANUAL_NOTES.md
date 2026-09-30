# Keithley 2634B manual notes for this OECT program

These command details come from the official [Series 2600B System SourceMeter Instrument Reference Manual, 2600BS-901-01 Rev. F (August 2021)](https://download.tek.com/manual/2600BS-901-01F_2600B_Reference_Aug2021.pdf), with the [Series 2600B User's Manual, 2600BS-900-01 Rev. A (August 2021)](https://download.tek.com/manual/2600BS-900-01A_2600B_Users_Aug2021.pdf) for connection and sense guidance. Printed section and page numbers below refer to these manuals, rather than PDF viewer page numbers.

## LAN interface

The 2634B supports the VXI-11 instrument interface over LAN. For the assigned IP address, open the PyVISA resource `TCPIP0::192.168.1.150::inst0::INSTR`. Keithley's [Python and 2600B example](https://my.tek.com/_entity/annotation/23d87c77-75e1-ef11-8eea-00224809c434/84b881c0-840c-ed11-82e5-002248097fa7) uses the same `TCPIP0::<IP>::inst0::INSTR` form and `*IDN?` for an initial identity check. The reference manual distinguishes VXI-11 from the **raw socket** interface on TCP port **5025**. It lists VXI-11 on port 1024; the application should use the VISA instrument resource above, with no `::5025::SOCKET` address or direct port-5025 connection. See Reference Manual §8, “Selecting a LAN interface protocol,” p. 8-52, and “Confirming port numbers,” p. 8-53.

The instrument uses Test Script Processor (TSP) commands. TSP values are returned using `print(...)`; `print(value1, value2, ...)` generates one response with values separated by **tabs**. See Reference Manual §9, `print()`, p. 9-170. Do not translate the 2634B control commands to a SCPI `:SOURce` command set.

## Two SMU channels and source setup

Use `smua` for channel A (drain) and `smub` for channel B (gate). In the commands below, replace `smuX` with either `smua` or `smub`; each channel has its own source, measure, and sense attributes. See Reference Manual §2, “Remote source-measure commands,” pp. 2-10–2-11.

| Purpose | TSP command or expression |
| --- | --- |
| Select DC voltage source | `smuX.source.func = smuX.OUTPUT_DCVOLTS` |
| Enable source-voltage autorange | `smuX.source.autorangev = smuX.AUTORANGE_ON` |
| Set source voltage, in V | `smuX.source.levelv = value` |
| Set current compliance, in A | `smuX.source.limiti = value` |
| Enable current-measure autorange | `smuX.measure.autorangei = smuX.AUTORANGE_ON` |
| Set integration time, in line cycles | `smuX.measure.nplc = value` |
| Source on / off | `smuX.source.output = smuX.OUTPUT_ON` / `smuX.source.output = smuX.OUTPUT_OFF` |
| Read source output state | `print(smuX.source.output)`; `0` means off and `1` means on |
| Read compliance state | `print(smuX.source.compliance)`; `true` means a configured limit is controlling the output |

The voltage-source example in Reference Manual §2, p. 2-11 and the power example on p. 2-21 show the source function, level, current limit, current measurement, and output sequence. The `smuX.measure.nplc` range is **0.001 to 25** (§2, “Speed,” p. 2-50). Compliance is a read-only Boolean and may represent a current, voltage, or power limit (§9, `smuX.source.compliance`, p. 9-240). Output-state readback and the `OUTPUT_ON`/`OUTPUT_OFF` constants are documented in §9, `smuX.source.output`, p. 9-249.

For this two-terminal OECT wiring, select `smuX.sense = smuX.SENSE_LOCAL` (local 2-wire sense). `smuX.SENSE_REMOTE` selects 4-wire sensing only when the separate sense leads are wired to the DUT. The 2600B defaults to local sense after reset. See Reference Manual §9, `smuX.sense`, p. 9-237, and User's Manual §4, “Sense mode selection,” p. 4-30.

## Measurements and timing

`print(smua.measure.i(), smub.measure.i(), smua.source.compliance, smub.source.compliance)` is valid TSP syntax for a single tab-separated response. `smuX.measure.i()` makes a current measurement in amperes; `smuX.measure.v()` makes a voltage measurement in volts. `iReading, vReading = smuX.measure.iv()` returns current first, voltage second. A call without a reading buffer makes one measurement even if `smuX.measure.count` is greater than one. See Reference Manual §9, `smuX.measure.Y()`, p. 9-233, and `print()`, p. 9-170.

The 2634B can also collect several readings in a buffer: clear `smuX.nvbuffer1`, set `smuX.measure.count` and `smuX.measure.interval`, start `smuX.measure.overlappedi(smuX.nvbuffer1)` (or `overlappediv` with two buffers), then use `waitcomplete()` and `printbuffer(...)`. See Reference Manual §3, “Reading buffer commands,” pp. 3-9–3-10. The simple measurement interval attribute accepts **0 to 1 s**; it is best effort if the meter cannot finish at the requested rate (§9, `smuX.measure.interval`, pp. 9-226–9-227). The GUI's host-paced loop is appropriate for longer intervals and for sampling the separate DMM6500, but the two instruments' readings are sequential, not hardware-synchronized.

## Output-off behavior and model limits

By default, `OUTPUT_OFF` uses **normal** output-off mode, which may still present a 0 V or 0 A source at the terminals. To open the output relay when turning a channel off, set `smuX.source.offmode = smuX.OUTPUT_HIGH_Z` before the run. Alternatively, `smuX.source.output = smuX.OUTPUT_HIGH_Z` turns that channel off in high-impedance mode immediately; subsequent output-state readback returns `0`. See Reference Manual §9, `smuX.source.offmode`, p. 9-248, and `smuX.source.output`, p. 9-249. The User's Manual cautions that output-off mode and compliance require consideration when connecting devices capable of delivering energy (§4, p. 4-5).

The 2634B is a two-channel model without TSP-Link. Its **100 pA measurement range is unavailable**; the specified current ranges start at 1 nA. The [official 2634B/2635B/2636B specifications](https://www.tek.com/en/documents/specification/models-2634b-2635b-and-2636b-system-sourcemeter-instrument-specifications) also state a 30.3 W continuous maximum per channel and describe the safety interlock required for 200 V operation. The OECT program should use current compliance suited to the device and should not infer accuracy below the instrument's available range.

These notes verify the documented syntax and address format. Communication, wiring, measurement sign, relay behavior, compliance response, and DMM/SMU timing remain **unverified on live hardware**; the project's automated tests are offline simulations.
