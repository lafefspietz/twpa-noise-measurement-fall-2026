# noisecube.py
# get noisecube.json
# open web socket on port 6502
# listen for commands from knobs
# set the values of DS345, HP audio synth, WindFreak A, B, or
# programmable attenuator or sdr parameters
# get noise traces 
# pass noise traces over to the front end at noisecube.js via web socket
# that is all, saving json happens from the front end automatically based on full stacks of spectra 
# if it is in simulation mode it will just compute a trace and pass it back
# it passes back the fghz or fkhz waves
import asyncio
import json
import time
import websockets
import copy
from urllib.request import urlopen
import numpy as np
from pathlib import Path
from datetime import datetime

async def receive_data(websocket):
    try:
        async for message in websocket:
            try:
                incoming_json = json.loads(message)
                print(incoming_json)
            except json.JSONDecodeError as e:
                print(f"Error parsing data: {e}")
    except websockets.exceptions.ConnectionClosed:
        pass

async def main_loop():
    async def connection_handler(ws):
        await receive_data(ws)
        
    async with websockets.serve(connection_handler, "localhost", 6502):
        await asyncio.Future() 

if __name__ == "__main__":
    time.sleep(1)

    try:
        asyncio.run(main_loop())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nServer shutdown complete.")