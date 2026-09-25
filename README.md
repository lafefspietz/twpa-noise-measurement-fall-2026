# twpa-noise-measurement-fall-2026
Measurement of TWPA noise with mesoscopic noise source and waveguide qubit for power calibration

 - [noisecube.html](noisecube.html)


Plan

1. make sure noise source works
2. make sure qubit works
3. make sure qubit and noise source work together
4. do a calibration with the qubit at sweet spot to convert volts at source to W at qubit
5. use linearity of receiver to build up a calibrated curve that maps voltage to power
6. build a routine to automatically set power to known high power and zero and do a y factor, extract both gain and noise temperature
7. rebuild noise cube to be only spectrum analyzer and not vna, with an initial calibration routine that saves numbers to json
8. get TWPA working
9. Map noise modes with a chopped noise source at input for y factor
10. build massive noise cube which pushes twpa up to edge of chaos
11. build real time knob interface to build up 