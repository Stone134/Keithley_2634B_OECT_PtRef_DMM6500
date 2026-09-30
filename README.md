# OECT with reference: Keithley 2634B and DMM6500

This standalone program runs one OECT with a Keithley **2634B two-channel
SourceMeter** and a separate DMM input for the reference electrode. It preserves
the earlier reference-feedback GUI and its six-column raw-data format. The
2634B runs its native **TSP** command language: `smua` biases and measures the
drain, and `smub` biases and measures the gate.

The second instrument is the **Keithley DMM6500** from the earlier project, at
`192.168.1.160`. Its driver uses model-specific commands to verify DC voltage,
input impedance, and terminal selection.

## Connections

| Instrument terminal | OECT connection |
| --- | --- |
| 2634B channel A (`smua`) HI | Drain |
| 2634B channel B (`smub`) HI | Current-carrying gate electrode |
| 2634B channels A and B LO | Common OECT source node |
| DMM6500 front INPUT HI | Separate reference electrode |
| DMM6500 front INPUT LO | Common OECT source node |

The DMM measures `Vref = V(reference) − V(source)`. The feedback target uses the
same sign: `Eref = Vref`. Keep the voltage-sensing reference separate from the
gate electrode carrying current. The program uses the 2634B's local sensing
mode; follow the 2634B manual if you choose to change to remote sense wiring.
Do not assume that the shared LO node is chassis ground; the 2634B manual
describes floating connections and their limits.

The DMM6500 must be physically set to **FRONT** terminals. Its driver checks
SCPI language and front terminals, then selects DC voltage on a fixed 10 V
range with **AUTO input impedance**. On a DMM6500, AUTO gives more than 10 GΩ
input impedance on this range; a 10 MΩ setting can load a reference electrode.
It disables relative, math, averaging, and limit functions, uses a one-reading
trigger model, and verifies the configuration before the SMU output is enabled
and while reading. These details are specific to the DMM6500.

## LAN connection

The default VISA resource names are:

```text
2634B:  TCPIP0::192.168.1.150::inst0::INSTR
DMM6500: TCPIP0::192.168.1.160::inst0::INSTR
```

Both are VISA **TCPIP INSTR** resources (`inst0`, VXI-11), not raw TCP socket
resources. There is no port number to enter in the GUI. The application uses
PyVISA with the `pyvisa-py` backend; both instruments must be reachable from
the computer on the same network. **Test both connections** queries identities
without turning on the 2634B outputs.

## Install and run

### Windows

Copy this project folder to a writable location, such as your Documents folder.
Install 64-bit Python 3.13, then open PowerShell in this folder and run:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe gui.py
```

Create the `.venv` on the Windows PC rather than copying a virtual environment
from another computer. Later, only the last command is needed to start the
GUI. The `.command` launcher below is for macOS.

### macOS

From this folder:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python gui.py
```

On macOS, `launch_gui.command` starts the same GUI after dependencies are
installed. The GUI opens in **Simulation** mode and makes no LAN connection
until you deselect it. Set the two IP addresses in the Connection card if
needed, then use **Test both connections** before a live run.

The controls retain the previous layout: raw-data filename; drain bias;
reference-target initial/final/step; time at each target, delay, final keep,
and read interval; gate bounds, initial gate value, correction factor, feedback
direction, maximum gate change, and tolerance. Three live plots show drain
current, gate voltage command, and measured Vref with its target. **Stop
safely** requests zero source settings and turns both SMU outputs off. Closing
the window during a run also asks to stop. The output file is replaced only
after the GUI's overwrite confirmation.

The default target staircase is 0 to −0.8 V in −0.1 V steps. Default drain
bias is −0.01 V, and both channel current compliance settings are 3 mA. The
program deliberately restricts its voltage controls to ±3 V, even though the
2634B has wider hardware ranges. Check that these settings suit the device
before enabling the outputs.

## Reference feedback and timing

After each reference reading, the software computes

```text
error = target Eref − measured Eref
gate change = direction × correction factor × error
```

It limits that change to the selected maximum step and keeps the command
inside the gate bounds. The default direction means decreasing the gate
voltage is expected to decrease Eref; select the opposite direction only if a
small observed gate change in your cell shows that response. The DMM reading
and target are never sign-inverted. Rows during the delay remain in the raw
file; the last target receives the additional final keep time.

The read interval is a **host-paced** interval. One TSP request obtains the
2634B's drain and gate current readings, and the DMM Vref reading follows
over LAN. The readings are not hardware simultaneous. They are also not
guaranteed to occur exactly at the requested interval; network latency,
measurement time, or a busy computer can make a row late. The plotted and
saved drain/gate voltages are verified source commands, not independent
voltage measurements.

The raw output is a tab-delimited `.txt` file with exactly these columns:

```text
Time  GateI  GateV  DrainI  DrainV  Vref
```

`Time` is elapsed seconds, currents are amperes, and voltages are volts. The
default path is `outputs/oect_reference.txt` inside this project. A DMM or
control error triggers an attempt to set both SMU sources to 0 V and turn both
outputs off. If LAN communication is lost, verify the output indicators at the
2634B itself.

## Offline check

Run the automated tests without instrument connections:

```bash
.venv/bin/python -m unittest discover -v
```

You can also launch the GUI in Simulation mode, choose a new `.txt` output
file, and shorten the period, delay, final keep, and read interval for a quick
trial. Simulation models a reference response to the gate command but cannot
identify the real cell's feedback direction. Before measuring a device, use a
short live run on a suitable dummy load to verify polarity, current limits,
read timing, DMM impedance, and output shutdown. **The program has only been
tested offline in this workspace; no 2634B or DMM was connected for
validation.**

## Manuals and protocol references

- [Local 2634B manual notes](MANUAL_NOTES.md) list the commands and printed manual pages used for this program.
- [Keithley Series 2600B System SourceMeter Reference Manual, Rev. F](https://download.tek.com/manual/2600BS-901-01F_2600B_Reference_Aug2021.pdf) (includes the 2634B and TSP command reference).
- [Keithley DMM6500 Reference Manual](https://download.tek.com/manual/DMM6500-901-01B_Sept_2019_Ref.pdf).
- [PyVISA resource-name syntax](https://pyvisa.readthedocs.io/en/latest/introduction/names.html) and [PyVISA-py TCPIP INSTR support](https://pyvisa.readthedocs.io/projects/pyvisa-py/en/latest/installation.html).
