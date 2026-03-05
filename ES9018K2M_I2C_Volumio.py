#!/usr/bin/python3
# coding: utf-8

import json
import time
import smbus2
import logging
import signal
import sys
import os
import threading
import RPi.GPIO as GPIO
from websocket import WebSocketApp

# =========================
# CONFIGURATION
# =========================

I2C_BUS = 1
DAC_ADDR = 0x48

# ES9018K2M register addresses
REG_INPUT      = 0x01
REG_MUTE       = 0x07
REG_GPIO       = 0x08
REG_CHMAP      = 0x0B
REG_DPLL       = 0x0C
REG_SOFT       = 0x0E
REG_VOL_L      = 0x0F
REG_VOL_R      = 0x10

# RPi GPIO pins
GPIO_I2S    = 17
GPIO_SPDIF1 = 27
GPIO_SPDIF2 = 4

# Volumio WebSocket settings
WS_URL = "ws://localhost:3000/socket.io/?EIO=3&transport=websocket"

# =========================
# LOGGING SETUP (Journald only)
# =========================

# Настраиваем логи только на вывод в консоль (stdout)
# systemd автоматически добавит их в journalctl
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

# =========================
# DAC CONTROLLER CLASS
# =========================

class ES9018K2M:
    def __init__(self):
        self.bus = None
        self.lock = threading.Lock()
        self.volume = None
        self.mute = None
        self.input_mode = None
        self.last_write = 0
        self._connect_bus()

    def _connect_bus(self):
        try:
            self.bus = smbus2.SMBus(I2C_BUS)
            logging.info(f"Connected to I2C bus {I2C_BUS}")
        except Exception as e:
            logging.error(f"Failed to open I2C bus: {e}")

    def _write(self, reg, val):
        if not self.bus:
            return
        with self.lock:
            now = time.time()
            if now - self.last_write < 0.04:
                time.sleep(0.04)
            try:
                self.bus.write_byte_data(DAC_ADDR, reg, val)
                self.last_write = time.time()
            except Exception as e:
                logging.error(f"I2C Write Error [Reg {hex(reg)}]: {e}")

    def init_dac(self):
        """Initial hardware configuration"""
        self._write(REG_GPIO, 0x88)
        self._write(REG_DPLL, 0xAA)
        self._write(REG_SOFT, 0x8A)
        self._write(REG_INPUT, 0x80)
        self._write(REG_CHMAP, 0x32)
        self.input_mode = "i2s"
        self._write(REG_MUTE, 0x80)
        logging.info("DAC initialized: GPIO1/2 Inputs, DPLL 0xAA, Default=I2S")

    def set_input_mode(self, mode):
        if self.input_mode == mode:
            return
            
        logging.info(f"Input switch triggered: {mode}")
        self.set_mute(True)
        time.sleep(0.1)

        if mode == "i2s":
            self._write(REG_INPUT, 0x80)
        elif mode == "spdif1":
            self._write(REG_CHMAP, 0x32)
            self._write(REG_INPUT, 0x81)
        elif mode == "spdif2":
            self._write(REG_CHMAP, 0x42)
            self._write(REG_INPUT, 0x81)

        time.sleep(0.1)
        self.set_mute(False)
        self.input_mode = mode

    def set_volume(self, vol):
        if vol == self.volume:
            return
        att = 100 - max(0, min(100, vol))
        self._write(REG_VOL_L, att)
        self._write(REG_VOL_R, att)
        self.volume = vol
        logging.info(f"Volume: {vol}% (Reg: {hex(att)})")

    def set_mute(self, mute):
        if mute == self.mute:
            return
        self._write(REG_MUTE, 0x83 if mute else 0x80)
        self.mute = mute
        logging.info(f"Mute: {'ON' if mute else 'OFF'}")

# =========================
# MONITORING THREAD
# =========================

def input_monitor_thread(dac_instance):
    last_detected_mode = None
    logging.info("Input monitor active")
    
    while True:
        try:
            current_mode = None
            if GPIO.input(GPIO_I2S) == GPIO.LOW:
                current_mode = "i2s"
            elif GPIO.input(GPIO_SPDIF1) == GPIO.LOW:
                current_mode = "spdif1"
            elif GPIO.input(GPIO_SPDIF2) == GPIO.LOW:
                current_mode = "spdif2"

            if current_mode and current_mode != last_detected_mode:
                dac_instance.set_input_mode(current_mode)
                last_detected_mode = current_mode
        except Exception as e:
            logging.error(f"GPIO polling error: {e}")
        
        time.sleep(0.25)

# =========================
# WEBSOCKET & MAIN
# =========================

def on_message(ws, message):
    try:
        if not message.startswith("42"): return
        payload = json.loads(message[2:])
        if payload[0] == "pushState":
            state = payload[1]
            volume = state.get("volume")
            mute = state.get("mute")
            if volume is not None:
                dac.set_volume(int(volume))
            if mute is not None:
                dac.set_mute(mute)
    except:
        pass

def start_ws():
    ws = WebSocketApp(WS_URL, on_message=on_message)
    ws.run_forever(ping_interval=10, ping_timeout=5)

dac = ES9018K2M()

def stop_handler(sig, frame):
    logging.info("Shutting down controller...")
    GPIO.cleanup()
    sys.exit(0)

signal.signal(signal.SIGTERM, stop_handler)
signal.signal(signal.SIGINT, stop_handler)

if __name__ == "__main__":
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    for pin in [GPIO_I2S, GPIO_SPDIF1, GPIO_SPDIF2]:
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    dac.init_dac()
    
    monitor = threading.Thread(target=input_monitor_thread, args=(dac,), daemon=True)
    monitor.start()
    
    while True:
        try:
            start_ws()
        except Exception:
            time.sleep(5)
