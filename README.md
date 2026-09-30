# twpa-noise-measurement-fall-2026
Measurement of TWPA noise with mesoscopic noise source and waveguide qubit for power calibration

## Day 1: September 25, 2026

We begin by sweeping the qubit flux bias and looking at the VNA at low power:

![](25-09-2026/1790350926-qubit-sweep-plot.png)

Then zoom in on the sweet spot

![](25-09-2026/1790349780-qubit-sweep-plot.png)
![](25-09-2026/1790355388-qubit-sweep-plot.png)
![](25-09-2026/1790355944-qubit-sweep-plot.png)
take a trace with some averaging
![](25-09-2026/1790364663-vna-average-plot.png)
sweep power
![](25-09-2026/power-sweep-1d.png)

take 1000 averages and look at statistics

![](25-09-2026/integration-statistics-1.png)
![](25-09-2026/integration-statistics-2.png)
![](25-09-2026/integration-statistics-3.png)
![](25-09-2026/integration-statistics-4.png)
![](25-09-2026/integration-statistics-5.png)
![](25-09-2026/integration-statistics-6.png)


# Day 2: September 28, 2026

Took another power sweep, with improved Jupyter  notebook at [vna-power-sweep.ipynb](vna-power-sweep.ipynb) but with very awkward data format of large numbers of raw linear real and imaginary values which were saved to look at statistics of averaging. Did the analysis afterward at home in the notebook [vna-power-sweep-plots.ipynb](vna-power-sweep-plots.ipynb).  There appears to be a lot of unwanted physics in the qubit structure, and a very non-textook shape of the curves.  We will proceed to fit anyway.

![](28-09-2026/qubit-power-sweep.png)

Moved qubit to V=0 bias which should be about 7 GHz, and repeated scan where qubit should have no impact:

![](28-09-2026/no-qubit-power-sweep.png)

looking at averages, seeing how the noise integrates down:

![](28-09-2026/qubit-averages.png)

And here is a data file with the full 100 averages reduced to an averaged set of traces

Now we need to fit the qubit curve(with all its faults) to try to get approximate power calibration from vna and build the zero span power calibration at 5.58 GHz.  

# Day 3: September 29, 2026

Set up both network analyzer and spectrum analyzer to be in zero span mode at frequency of 5.58 GHz, swept the voltage on the noise source and the power from the network analyzer and looked at how the spectrum analzyer responds to the sum of noise and signal, to calibrate noise against signal.

![](29-09-2026/noise-power-scan-plot-1.png)

![](29-09-2026/noise-power-scan-plot-2.png)

![](29-09-2026/noise-power-scan-plot-3.png)

![](29-09-2026/noise-power-scan-plot-4.png)






