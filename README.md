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


# Day 4: September 30

Spent time on administrative tasks. Filled LN2 trap. 

Measured more sweeps of vna power in zero span on both vna and spa with zero bias on noise source for various settings:

![](30-09-2026/1790801324-zero-bias-power-scan-plot.png)

![](30-09-2026/1790802201-zero-bias-power-scan-plot.png)

![](30-09-2026/1790803157-zero-bias-power-scan-plot.png)

![](30-09-2026/1790803475-zero-bias-power-scan-plot.png)


# Day 5: October 1

Attempting to make sense of spectrum analyzer scaling, which appears to be very much not what it claims to be

![](01-10-2026/noise-plot-stack-1.png)

![](01-10-2026/noise-plot-stack-2.png)

# Day 6: October 2

Spent a bunch of time re-learning how frustrating the HP E7405A is to use, how the resolution bandwidth pretends to be controllable but actually only has a few set allowed values, and how both the bandwidth and reference level totally change response and how frustrating the hidden variables are in regards to mode.  In particular, I am now using it in the following mode all the time:


```
spa.write(':DET SAMP')  
```
which gets it out of the default peak detect mode which breaks things.

So doing Y factor over and over is the correct approach. And so we now build python and json required to do that with the noise diode.


Here is the noise diode:

![](noise-diode-1.png)
![](noise-diode-2.png)


And here are the Y plots of the cal data provided by the manufacturer:

![](02-10-2026/noise-diode-cal-1.png)
![](02-10-2026/noise-diode-cal-2.png)
![](02-10-2026/noise-diode-cal-3.png)

Here is the code to make the json we will use for cal:

```

import json
import matplotlib.pyplot as plt
import numpy as np
import matplotlib.ticker as ticker
noise_diode = {}
noise_diode['jupyter_url'] = "https://github.com/lafefspietz/twpa-noise-measurement-fall-2026/blob/main/noise-diode.ipynb"
noise_diode['serial_number'] = "MY61410135"
noise_diode['model'] = "346B"
noise_diode['manufacturer'] = "Keysight"
noise_diode['voltage'] = "28"
noise_diode['fghz_cal'] = [0.01,0.1,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18]
noise_diode['ENR_cal'] = [
    15.24,
    15.22,
    15.13,
    15.13,
    14.94,
    14.85,
    14.81,
    14.78,
    14.80,
    14.78,
    14.86,
    14.83,
    14.76,
    14.90,
    15.00,
    15.08,
    15.28,
    15.35,
    15.16,
    14.58
]
noise_diode['diode_noise_temperature_hot_cal'] = []
for index in np.arange(len(noise_diode['ENR_cal'])):
    noise_temperature_hot = 290.0*(10**(noise_diode['ENR_cal'][index]/10))
    noise_diode['diode_noise_temperature_hot_cal'].append(noise_temperature_hot)

noise_diode['fghz'] = np.linspace(3, 12,1001)
noise_diode['diode_noise_temperature_hot'] = np.interp(noise_diode['fghz'], noise_diode['fghz_cal'], noise_diode['diode_noise_temperature_hot_cal'])
noise_diode['diode_noise_temperature_cold'] = noise_diode['fghz']*0 + 290
k_boltzmann = 1.380649e-23
planck_constant = 6.62607015e-34
noise_diode['k_boltzmann'] = k_boltzmann
noise_diode['planck_constant'] = planck_constant
noise_diode['diode_noise_number_hot'] = k_boltzmann*noise_diode['diode_noise_temperature_hot']/(planck_constant*1e9*noise_diode['fghz'])
noise_diode['diode_noise_number_cold'] = k_boltzmann*noise_diode['diode_noise_temperature_cold']/(planck_constant*1e9*noise_diode['fghz'])

noise_diode['fghz'] = np.round(noise_diode['fghz'], 3).tolist() # 3 decimals for clean 0.009 steps
noise_diode['diode_noise_temperature_hot'] = np.round(noise_diode['diode_noise_temperature_hot'], 2).tolist()
noise_diode['diode_noise_temperature_cold'] = np.round(noise_diode['diode_noise_temperature_cold'], 2).tolist()
noise_diode['diode_noise_number_hot'] = np.round(noise_diode['diode_noise_number_hot'], 4).tolist()
noise_diode['diode_noise_number_cold'] = np.round(noise_diode['diode_noise_number_cold'], 4).tolist()

with open("noise-diode.json", "w", encoding="utf-8") as f:
    json.dump(noise_diode, f, indent=4)
    
```
And here is the jupyter notebook:

[noise-diode.ipynb](noise-diode.ipynb)

And here is the json:

[noise-diode.json](noise-diode.json)

# Day 7: October 5, 2026

Looking at fits from [qubit-power-fits.ipynb](qubit-power-fits.ipynb) to get the attenuation factor A:

![](28-09-2026/qubit-power-fit-1.png)
![](28-09-2026/qubit-power-fit-2.png)
![](28-09-2026/qubit-power-fit-3.png)

And here is a cartoon of the model we are using for the Y factor:


![](quantum-y-factor-cartoon.png)


![](08-10-2026/noise-diode-hot.png)

![](08-10-2026/noise-diode-cold.png)

$$
n_{out diode on} = G_{warm RX}(n_{warm RX} + n_{diode hot})
$$

$$
n_{out diode off} = G_{warm RX}(n_{warm RX} + n_{diode cold})
$$

$$
G_{warm RX} = \frac{n_{out diode on} - n_{out diode off}}{n_{diode hot} - n_{diode cold}}
$$

$$
n_{warm RX} = \frac{ n_{out diode on} }{ G_{warm RX} } - n_{diode hot}
$$


