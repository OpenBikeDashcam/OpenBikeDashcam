#!/usr/bin/env python3

#License: GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007

import sys
import time
import threading
import queue
import multiprocessing
from multiprocessing import Process, Value, Manager
from ctypes import c_int
from datetime import datetime
import os
import shutil
import csv
import cv2
import numpy as np
from picamera2 import Picamera2
import smbus2
import lgpio
import logging
import logging.handlers
import signal
#für GPS empfang
from gps_receiver import gps_generator
#für WebApp Anzeige auf dem iPhone
from flask import Flask, jsonify, render_template_string, request
app = Flask(__name__)
import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)  # nur Fehler anzeigen, keine normalen Requests
#für die Variable shared_event_state
from ctypes import c_bool      
from ctypes import c_int      

shared_event_state = Value(c_bool, False)
shared_distance = Value('i', 300)  # Anfangswert
shared_abstand = Value('i', 300)   # gefilterter Abstand
shared_bike_width = Value(c_int, 0)    # Lenkerbreite (Standardwert)
shared_sensor_offset = Value(c_int, 0) # Sensoroffset (Standardwert)
shared_overlay_update_flag = Value(c_bool, True)  # updateanweisung für das statische Overlay. Init auf True damit Overlay beim Start gezeichnet wird
######## ASYNCHRONES LOGGING SETUP ########
log_queue = queue.Queue(-1)
queue_handler = logging.handlers.QueueHandler(log_queue)
console_handler = logging.StreamHandler()
formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
console_handler.setFormatter(formatter)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(queue_handler)
queue_listener = logging.handlers.QueueListener(log_queue, console_handler)
queue_listener.start()

######## GLOBAL SETTINGS ########
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
FPS = 30
shared_dist_zul = Value(c_int, 150)             # Anfangswert für dist_zul (Abstandsschwelle)
shared_partial_video_seconds = Value(c_int, 180) # Anfangswert für PARTIAL_VIDEO_SECONDS
SD_PATH = "/dev/shm"
EVENT_DIR_NAME = "Events"
USB_BASE_PATH = "/media/pi"
RED_PIN = 6      # Board Pin 31
GREEN_PIN = 16   # Board Pin 36
BLUE_PIN = 26    # Board Pin 37
RUECKLICHT_PIN = 5
XSHUT_PIN = 23
h = lgpio.gpiochip_open(0)
lgpio.gpio_claim_output(h, XSHUT_PIN)

shutdown_flag = threading.Event()

######## LED CONTROL ########
class LedController(threading.Thread):
    def __init__(self, red_pin, green_pin, blue_pin):
        super().__init__()
        self.red_pin = red_pin
        self.green_pin = green_pin
        self.blue_pin = blue_pin
        self._stop_event = threading.Event()
        self.blink_interval = 0.6
        self.chip = lgpio.gpiochip_open(0)
        for pin in [self.red_pin, self.green_pin, self.blue_pin]:
            lgpio.gpio_claim_output(self.chip, pin)
            lgpio.gpio_write(self.chip, pin, 0)
        # Farbzustand
        #self.red_state = 0
        self.green_state = 0
        self.blue_state = 0
        self._blink = threading.Event()
        self._running = threading.Event()

    def set_color(self, red, green, blue):
        self.red_state = red
        self.green_state = green
        self.blue_state = blue


    def set_blink_interval(self, interval):
        self.blink_interval = interval

    def set_on(self):
        self._blink.clear()
        self._running.set()

    def set_blink(self):
        self._running.clear()
        self._blink.set()

    def set_off(self):
        self._running.clear()
        self._blink.clear()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            if self._blink.is_set():
                lgpio.gpio_write(self.chip, self.red_pin, self.red_state)
                lgpio.gpio_write(self.chip, self.green_pin, self.green_state)
                lgpio.gpio_write(self.chip, self.blue_pin, self.blue_state)
                time.sleep(self.blink_interval / 2)
                lgpio.gpio_write(self.chip, self.red_pin, 0)
                lgpio.gpio_write(self.chip, self.green_pin, 0)
                lgpio.gpio_write(self.chip, self.blue_pin, 0)
                time.sleep(self.blink_interval / 2)
            elif self._running.is_set():
                lgpio.gpio_write(self.chip, self.red_pin, self.red_state)
                lgpio.gpio_write(self.chip, self.green_pin, self.green_state)
                lgpio.gpio_write(self.chip, self.blue_pin, self.blue_state)
                time.sleep(0.05)
            else:
                lgpio.gpio_write(self.chip, self.red_pin, 0)
                lgpio.gpio_write(self.chip, self.green_pin, 0)
                lgpio.gpio_write(self.chip, self.blue_pin, 0)
                time.sleep(0.1)
        # LEDs ausschalten beim Stoppen
        lgpio.gpio_write(self.chip, self.red_pin, 0)
        lgpio.gpio_write(self.chip, self.green_pin, 0)
        lgpio.gpio_write(self.chip, self.blue_pin, 0)
        lgpio.gpiochip_close(self.chip)


######### Sensor & I2C etc. ##########
# === XM125 Sensor Register & Funktionen ===
I2C_ADDR = 0x52
REG_DETECTOR_STATUS = 0x0003
REG_DISTANCE_RESULT = 0x0010
REG_PEAK0_DISTANCE = 0x0011
REG_PEAK0_STRENGTH = 0x001B
REG_START = 0x0040
REG_END = 0x0041
REG_MAX_PROFILE = 0x0045
REG_THRESHOLD_METHOD = 0x0046
REG_FIXED_THRESHOLD_VALUE = 0x0049
REG_THRESHOLD_SENSITIVITY = 0x004A
REG_COMMAND = 0x0100
CMD_APPLY_CONFIG_AND_CALIBRATE = 1
CMD_MEASURE_DISTANCE = 2
CMD_RECALIBRATE = 5

def get_usb_label():
    try:
        entries = os.listdir(USB_BASE_PATH)
        for entry in entries:
            full_path = os.path.join(USB_BASE_PATH, entry)
            if os.path.ismount(full_path):
                return entry
    except Exception as e:
        logger.error(f"USB-Laufwerk-Fehler: {e}")
    return None

def set_leds(red=None, green=None, blue=None, ruecklicht=None):
    # Wird nicht mehr extern zum direkten GPIO-Zugriff genutzt,
    # LedController steuert GPIO exklusiv.
    pass

def write_reg(bus, addr: int, value: int):
    data = [
        (addr >> 8) & 0xFF,
        addr & 0xFF,
        (value >> 24) & 0xFF,
        (value >> 16) & 0xFF,
        (value >> 8) & 0xFF,
        value & 0xFF,
    ]
    msg = smbus2.i2c_msg.write(I2C_ADDR, data)
    bus.i2c_rdwr(msg)

def read_reg(bus, addr: int) -> int:
    addr_bytes = [(addr >> 8) & 0xFF, addr & 0xFF]
    write = smbus2.i2c_msg.write(I2C_ADDR, addr_bytes)
    read = smbus2.i2c_msg.read(I2C_ADDR, 4)
    bus.i2c_rdwr(write, read)
    result = list(read)
    return (result[0] << 24) | (result[1] << 16) | (result[2] << 8) | result[3]

def wait_until_not_busy(bus):
    while True:
        status = read_reg(bus, REG_DETECTOR_STATUS)
        if (status & 0x80000000) == 0:
            break
        time.sleep(0.01)

def configure_sensor(bus):
    write_reg(bus, REG_START, 300)
    write_reg(bus, REG_END, 4000)
    write_reg(bus, REG_MAX_PROFILE, 3)
    write_reg(bus, REG_THRESHOLD_METHOD, 3)
    write_reg(bus, REG_THRESHOLD_SENSITIVITY, 500)
    write_reg(bus, 0x0049, 25000)
    write_reg(bus, 0x0048, 50)
    write_reg(bus, 0x004C, 30000)
    write_reg(bus, 0x0043, 1)
    write_reg(bus, 0x0044, 15000)
    write_reg(bus, 0x0047, 2)
    write_reg(bus, 0x004B, 2)
    write_reg(bus, 0x0080, 0)
    write_reg(bus, REG_COMMAND, CMD_APPLY_CONFIG_AND_CALIBRATE)
    wait_until_not_busy(bus)

def measure_distance(bus):
    write_reg(bus, REG_COMMAND, CMD_MEASURE_DISTANCE)
    wait_until_not_busy(bus)
    result = read_reg(bus, REG_DISTANCE_RESULT)
    if result & 0x00000400:
        return None, None
    if result & 0x00000200:
        write_reg(bus, REG_COMMAND, CMD_RECALIBRATE)
        wait_until_not_busy(bus)
        return None, None
    num_peaks = result & 0xF
    if num_peaks == 0:
        return None, None
    distance = int(round(read_reg(bus, REG_PEAK0_DISTANCE) / 10))
    strength = read_reg(bus, REG_PEAK0_STRENGTH)
    return distance, strength

def sensor_worker(shared_distance, shared_bike_width, shared_sensor_offset):
    bus = smbus2.SMBus(1)
    try:
        configure_sensor(bus)
        logger.info("Sensor konfiguriert.")
        while True:
            dist, _ = measure_distance(bus)
            if dist is not None:
                shared_distance.value = int(round(dist - ((shared_bike_width.value / 2) - shared_sensor_offset.value)))
            else:
                shared_distance.value = 300
            time.sleep(1/FPS)
    except Exception as e:
        logger.error(f"Sensorfehler: {e}")
        shared_distance.value = 300


def create_dirs(base_path, event_path):
    os.makedirs(base_path, exist_ok=True)
    os.makedirs(event_path, exist_ok=True)
    logger.info(f"Speicherordner: {base_path}, {event_path}")

def get_bike_width_cm(filename):
    try:
        with open(filename, 'r') as file:
            val = file.read().strip()
            return int(val) if val.isdigit() else 0
    except:
        return 0

def get_sensor_offset_cm(filename):
    try:
        with open(filename, 'r') as file:
            val = file.read().strip()
            return int(val) if val.isdigit() else 0
    except:
        return 0

def get_dist_zul(path):
    try:
        with open(path, 'r') as f:
            val = f.read().strip()
            return int(val) if val.isdigit() else 150
    except Exception:
        return 150

def get_partial_video_seconds(path):
    try:
        with open(path, 'r') as f:
            val = f.read().strip()
            return int(val) if val.isdigit() else 60
    except Exception:
        return 60

######## ASYNCHRONE SPEICHERUNG (SAVE-WORKER) ########

save_queue = queue.Queue()

def get_free_space_bytes(path):
    """Gibt freien Speicherplatz (Bytes) im angegebenen Pfad zurück."""
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize

def delete_oldest_files(folder, ext_video=".mp4", ext_csv=".csv"):
    """Löscht das älteste Video + entsprechende CSV basierend auf Dateiendungen."""
    files = [f for f in os.listdir(folder) if f.endswith(ext_video)]
    if not files:
        return False
    files_full = [(f, os.path.getctime(os.path.join(folder, f))) for f in files]
    files_full.sort(key=lambda x: x[1])
    oldest_video = files_full[0][0]
    base_name = oldest_video[:-len(ext_video)]
    video_path = os.path.join(folder, oldest_video)
    csv_path = os.path.join(folder, base_name + ext_csv)
    try:
        if os.path.exists(video_path):
            os.remove(video_path)
        if os.path.exists(csv_path):
            os.remove(csv_path)
        logger.info(f"Lösche älteste Dateien: {oldest_video} und {base_name + ext_csv}")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Löschen ältester Dateien: {e}")
        return False

def save_worker(led_ctrl):
    led_state_blue = False
    while True:
        task = save_queue.get()
        if task is None:
            break
        writer_path, csv_path, shared_event_state, event_path = task
        try:
            free_bytes = get_free_space_bytes(event_path)
            if free_bytes < 100 * 1024 * 1024:
                if not led_state_blue:
                    led_ctrl.set_color(0, 0, 1)  # blau
                    led_state_blue = True
                deleted = delete_oldest_files(event_path)
                if not deleted:
                    logger.warning("Kein altes Video zum Löschen gefunden bei wenig Speicher.")
            else:
                if led_state_blue:
                    led_ctrl.set_color(1, 0, 0)  # rot
                    led_state_blue = False

            if shared_event_state:
                shutil.move(writer_path, os.path.join(event_path, os.path.basename(writer_path)))
                shutil.move(csv_path, os.path.join(event_path, os.path.basename(csv_path)))
                logger.info(f"Event-Video + CSV gesichert: {os.path.basename(writer_path)}")
            else:
                if os.path.exists(writer_path):
                    os.remove(writer_path)
                if os.path.exists(csv_path):
                    os.remove(csv_path)
                logger.info("Kein Event: Video + CSV gelöscht.")
        except Exception as e:
            logger.error(f"Fehler im Save-Worker: {e}")
        save_queue.task_done()

######## VIDEO WRITER THREAD ########
class VideoWriterThread(threading.Thread):
    def __init__(self, filename, frame_size, fps, fourcc='mp4v'):
        super().__init__()
        self.filename = filename
        self.queue = queue.Queue(maxsize=120)
        self.stopped = threading.Event()
        self.writer = cv2.VideoWriter(
            filename,
            cv2.VideoWriter_fourcc(*fourcc),
            fps,
            frame_size
        )

    def run(self):
        while not self.stopped.is_set() or not self.queue.empty():
            try:
                frame = self.queue.get(timeout=0.05)
                self.writer.write(frame)
                self.queue.task_done()
            except queue.Empty:
                continue
        self.writer.release()

    def write(self, frame):
        try:
            self.queue.put(frame, timeout=0.2)
        except queue.Full:
            logger.warning("[WARNING] VideoWriter-Queue voll. Frame wurde verworfen!")

    def stop(self):
        self.stopped.set()
        self.queue.join()

def make_writer_thread():
    fname = f"{SD_PATH}/{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.mp4"
    writer_thread = VideoWriterThread(fname, (FRAME_WIDTH, FRAME_HEIGHT), FPS)
    writer_thread.start()
    return writer_thread, fname


def stop_and_queue_save(writer_thread, csv_file, shared_event_state, writer_path, csv_path, event_path):
    # Schließen der CSV-Datei sofort
    csv_file.close()
    # Writer im Hintergrund stoppen lassen (nicht blockieren)
    writer_thread.stop()
    # Übergibt direkt die Dateien in die Save-Queue -> Save-Worker erledigt den Rest
    save_queue.put((writer_path, csv_path, shared_event_state.value, event_path))


def gps_worker_process(gps_data_dict, interval=1): #Messinterval = 1s
    """
    Startet den GPS-Generator aus gps_receiver.py und schreibt
    die gemessenen GPS-Daten zyklisch in gps_data_dict.
    Interval in Sekunden bestimmt die Aktualisierungshäufigkeit.
    """
    gen = gps_generator()
    try:
        for data in gen:
            gps_data_dict['timestamp'] = int(time.time() * 1000)
            gps_data_dict['lat'] = data.get('latitude', 0.0)
            gps_data_dict['lon'] = data.get('longitude', 0.0)
            speed_knots = data.get('speed_knots', 0.0)
            gps_data_dict['speed'] = speed_knots * 1.852  # Knoten -> km/h
            gps_data_dict['heading'] = data.get('course', 0.0)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
        
def make_webapp(shared_distance, shared_abstand, shared_event_state, shared_bike_width, shared_sensor_offset, gps_data):
    HTML = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Live-Abstand</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link rel="icon" href="{{ url_for('static', filename='icon32.png') }}" type="image/png">
        <style>
            body {
                background-color: #000;
                color: #fff;
                font-family: Arial, sans-serif;
                text-align: center;
                margin: 0;
                padding: 20px;
            }
            .logo-container {
                width: 100%;
                margin-bottom: 10px;
                display: flex;
                justify-content: center;
                align-items: center;
            }
            .logo-container img {
                width: 100px;
                height: 60px;
                background-color: #fff;
                object-fit: contain;
            }
            .tile {
                border: 2px solid #fff;
                border-radius: 12px;
                padding: 10px;
                margin: 10px auto;
                max-width: 320px;
                position: relative;
            }
            .label {
                font-size: 0.8em;
                color: #aaa;
                margin-bottom: 4px;
            }
            .value-medium {
                font-size: 2.5em;
                font-weight: bold;
                transition: color 0.2s;
            }
            .value-large {
                font-size: 4em;
                font-weight: bold;
                transition: color 0.2s;
            }
            .small-status {
                position: absolute;
                top: 8px;
                right: 12px;
                font-size: 0.8em;
                font-weight: bold;
            }
            .rot { color: #ff4c4c; }
            .gruen { color: #2aff6a; }
            input {
                font-size: 1.2em;
                text-align: center;
                width: 80px;
                border-radius: 6px;
                border: none;
                padding: 4px;
            }
            button {
                margin-top: 15px;
                font-size: 1.2em;
                padding: 10px 20px;
                border-radius: 5px;
                background: #2aff6a;
                border: none;
                cursor: pointer;
            }
            #save_status {
                font-size: 0.9em;
                margin-top: 5px;
                min-height: 16px;
            }
            .input-row {
                display: flex;
                justify-content: center;
                gap: 20px;
                margin-top: 20px;
                flex-wrap: wrap;
            }
            .input-column {
                display: flex;
                flex-direction: column;
                align-items: center;
                width: 120px;
            }
            .input-column label {
                font-size: 1em;
                color: #aaa;
                margin-bottom: 6px;
            }
            .input-column input {
                font-size: 1.8em;
                text-align: center;
                width: 100%;
                border-radius: 8px;
                border: none;
                padding: 10px;
                box-sizing: border-box;
            }
        </style>
        <script>
            function poll() {
                fetch('/data').then(r => r.json()).then(data => {

                    var letzterElem = document.getElementById('letzter');
                    letzterElem.innerText = data.last_overtake + " cm";
                    if (data.last_overtake < data.dist_zul) {
                        letzterElem.className = "value-large rot";
                    } else {
                        letzterElem.className = "value-large gruen";
                    }

                    var statusElem = document.getElementById('status-text');
                    if (data.event_state) {
                        statusElem.textContent = "Status: Event";
                        statusElem.style.color = "#ff4c4c";
                    } else {
                        statusElem.textContent = "Status: OK";
                        statusElem.style.color = "#2aff6a";
                    }

                    var geschwElem = document.getElementById('geschwindigkeit');
                    geschwElem.innerText = data.speed.toFixed(1) + " km/h";
                    geschwElem.className = "value-medium";

                });
            }

            function saveSettings() {
                const bw = parseInt(document.getElementById('input_bike_width').value);
                const so = parseInt(document.getElementById('input_sensor_offset').value);
                const dist_zul = parseInt(document.getElementById('input_dist_zul').value);
                const video_sekunden = parseInt(document.getElementById('input_video_sekunden').value);

                fetch('/save-settings', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        shared_bike_width: bw,
                        shared_sensor_offset: so,
                        dist_zul: dist_zul,
                        partial_video_seconds: video_sekunden
                    })
                }).then(resp => resp.json())
                  .then(result => {
                      document.getElementById('save_status').innerText = result.message;
                  })
                  .catch(err => {
                      document.getElementById('save_status').innerText = "Fehler beim Speichern";
                  });
            }

            window.onload = function() {
                fetch('/data').then(r => r.json()).then(data => {
                    document.getElementById('input_bike_width').value = data.bike_width;
                    document.getElementById('input_sensor_offset').value = data.sensor_offset;
                    document.getElementById('input_dist_zul').value = data.dist_zul;
                    document.getElementById('input_video_sekunden').value = data.partial_video_seconds;
                });
                setInterval(poll, 1000);
            };
        </script>
    </head>
    <body>
        <div class="logo-container">
            <link rel="icon" type="image/png" sizes="32x32" href="{{ url_for('static', filename='icon32.png') }}">
        </div>
        <div 
            class="logo-container" 
            style="background-color: white; width: 90px;
            padding: 3px; 
            margin-left: auto; 
            margin-right: auto; 
            display: block;
            border-radius: 12px; 
            "
        >
            <img src="{{ url_for('static', filename='logo200.png') }}" alt="Logo" style="display: block; width: 100%;">
        </div>
        <!--
        <div class="tile" style="margin-top:0;">
            <div class="label">Aktueller Sensorwert</div>
            <div id="sensorwert" class="value-large"></div>
        </div>
        -->
        
        <div class="tile">
            <div class="label">Letzter Überholabstand</div>
            <div id="letzter" class="value-large"></div>
            <div id="status-text" class="small-status">Status: OK</div>
        </div>

        <div class="tile">
            <div class="label">Geschwindigkeit</div>
            <div id="geschwindigkeit" class="value-medium"></div>
        </div>

        <button onclick="shutdownSystem()" style="margin-top: 20px; font-size: 1.2em; padding: 10px 20px;">Power OFF</button>

        <!-- Alle Eingabefelder in einer gemeinsamen Reihe -->
        <div class="input-row">
            <div class="input-column">
                <label for="input_dist_zul">zul. Abstand (cm)</label>
                <input type="number" id="input_dist_zul" min="0" max="500">
            </div>
            <div class="input-column">
                <label for="input_video_sekunden">Video in Sekunden</label>
                <input type="number" id="input_video_sekunden" min="1" max="300">
            </div>
            <div class="input-column">
                <label for="input_bike_width">Lenkerbreite (cm)</label>
                <input type="number" id="input_bike_width" min="0" max="200">
            </div>
            <div class="input-column">
                <label for="input_sensor_offset">Sensoroffset (cm)</label>
                <input type="number" id="input_sensor_offset" min="0" max="50">
            </div>
        </div>

        <button onclick="saveSettings()">Speichern</button>
        <div id="save_status"></div>

    <script>
    function shutdownSystem() {
        if(confirm("Möchtest du den Raspberry Pi wirklich herunterfahren?")) {
            fetch('/shutdown', { method: 'POST' })
            .then(response => response.json())
            .then(data => alert(data.message))
            .catch(error => alert('Fehler beim Shutdown: ' + error));
        }
    }
    </script>

    </body>
    </html>
    """

    from flask import Flask, jsonify, render_template_string, request
    app = Flask(__name__)
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    @app.route('/')
    def index():
        return render_template_string(HTML)

    @app.route('/data')
    def data():
        return jsonify({
            "bike_width": shared_bike_width.value,
            "sensor_offset": shared_sensor_offset.value,
            "last_overtake": shared_abstand.value,
            "speed": gps_data.get('speed', 0),
            "event_state": shared_event_state.value,
            "dist_zul": shared_dist_zul.value,
            "partial_video_seconds": shared_partial_video_seconds.value
        })

    @app.route('/shutdown', methods=['POST'])
    def shutdown():
        def delayed_shutdown():
            time.sleep(1)  # kurze Verzögerung, damit Antwort vorher gesendet wird
            os.system('sudo shutdown -h now')

        # Shutdown in separatem Thread ausführen, damit HTTP-Antwort sofort zurückkommt
        threading.Thread(target=delayed_shutdown).start()

        return jsonify({"message": "Shutdown-Befehl wurde gesendet"}), 200

    @app.route('/save-settings', methods=['POST'])
    def save_settings():
        data_json = request.get_json()
        bw = data_json.get('shared_bike_width')
        so = data_json.get('shared_sensor_offset')
        dist_zul_val = data_json.get('dist_zul')
        partial_video_sec_val = data_json.get('partial_video_seconds')
        usb_label = get_usb_label()
        if not usb_label:
            return jsonify({"success": False, "message": "USB-Stick nicht gefunden"}), 500
        try:
            bw_path = f"/media/pi/{usb_label}/settings/bikewidth.txt"
            so_path = f"/media/pi/{usb_label}/settings/sensoroffset.txt"
            dist_zul_path = f"/media/pi/{usb_label}/settings/dist_zul.txt"
            partial_video_path = f"/media/pi/{usb_label}/settings/video_sekunden.txt"

            with open(bw_path, 'w') as f:
                f.write(str(bw))
            with open(so_path, 'w') as f:
                f.write(str(so))
            with open(dist_zul_path, 'w') as f:
                f.write(str(dist_zul_val))
            with open(partial_video_path, 'w') as f:
                f.write(str(partial_video_sec_val))

            shared_bike_width.value = bw
            shared_sensor_offset.value = so
            shared_dist_zul.value = dist_zul_val
            shared_partial_video_seconds.value = partial_video_sec_val
            
            # Flag setzen für Overlay-Neuberechnung
            shared_overlay_update_flag.value = True
        
            save_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return jsonify({"success": True, "message": f"{save_time} Einstellungen gespeichert"})
        except Exception:
            save_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return jsonify({"success": False, "message": f"{save_time} Fehler beim Speichern"}), 500

    app.run(host='0.0.0.0', port=5000, debug=False)

def start_webapp(shared_distance, shared_abstand, shared_event_state, shared_bike_width, shared_sensor_offset, gps_data):
    t = threading.Thread(
        target=make_webapp,
        args=(shared_distance, shared_abstand, shared_event_state, shared_bike_width, shared_sensor_offset, gps_data),
        daemon=True
    )
    t.start()
    return t

def create_static_overlay(bike_width, sensor_offset, width, height, font, font_scale, color, thickness):
    overlay = np.zeros((height, width, 3), dtype=np.uint8)
    text = (
        f"  Lenkerbreite: {bike_width} cm    Sensor Offset: {sensor_offset} cm   "
        f"{datetime.now().strftime('%Y-%m-%d')}                     Seitenabstand:     (    )cm             km/h"
    )
    cv2.putText(overlay, text, (0, int(height*0.85)), font, font_scale, color, thickness, cv2.LINE_AA)
    return overlay

def overlay_logo(frame, logo, position=(0,0), rect_color=(255,255,255), rect_alpha=0.5):
    x, y = position
    lh, lw = logo.shape[:2]

    # ROI holen
    roi = frame[y:y+lh, x:x+lw]

    # Neues Overlay Bild mit gleicher Größe wie ROI (volle Fläche)
    overlay = np.full(roi.shape, rect_color, dtype=np.uint8)

    # addWeighted auf neues Array schreiben
    blended = cv2.addWeighted(overlay, rect_alpha, roi, 1 - rect_alpha, 0)

    # Ergebnis zurückkopieren in frame ROI
    frame[y:y+lh, x:x+lw] = blended

    # Logo mit Alpha-Kanal drüberlegen
    logo_bgr = logo[:, :, :3]
    alpha_mask = logo[:, :, 3] / 255.0

    roi = frame[y:y+lh, x:x+lw]  # frisches ROI nach Update

    for c in range(3):
        roi[:, :, c] = (alpha_mask * logo_bgr[:, :, c] + (1 - alpha_mask) * roi[:, :, c]).astype(np.uint8)


def signal_handler(signum, frame):
    # Ignoriere Folge-Signale für dieses Signal während Cleanup
#    signal.signal(signum, signal.SIG_IGN)

    # Setze Flag, sodass Hauptloop säuberlich beendet wird
#    shutdown_flag.set()
    
    # Optional: Warte kurz oder führe Cleanup direkt hier aus
    
    # Wenn Cleanup im Hauptthread nicht vollständig klappt,
    # kann man hier vorzeitig exit erzwingen (vorsichtig nutzen)
    # sys.exit(0) # Optional, wenn nötig
    raise KeyboardInterrupt  # Nutzt die bestehende Cleanup-Logik sauber


######## MAIN ########
def main():
    global shared_distance, shared_bike_width, shared_sensor_offset, gps_data
    global shared_overlay_update_flag

    # Signal Handler für SIGTERM und SIGINT registrieren
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)  # Optional, falls nicht schon behandelt

    #Rücklicht-LED starten
    h_ruecklicht = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_output(h_ruecklicht, RUECKLICHT_PIN)
    lgpio.gpio_write(h_ruecklicht, RUECKLICHT_PIN, 1)  # Rücklicht ON


    led_ctrl = LedController(RED_PIN, GREEN_PIN, BLUE_PIN)
    led_ctrl.start()
    led_ctrl.set_color(1, 0, 0)  # Status LED Rot initial
    led_ctrl.set_on()

    logger.info(f"Programmstart: {os.path.basename(__file__)}")
    usb_label = get_usb_label()
    if not usb_label:
        logger.error("USB Stick nicht gefunden/mountet!")
        led_ctrl.set_off()
        led_ctrl.stop()
        led_ctrl.join()
        sys.exit(1)
    event_path = os.path.join(USB_BASE_PATH, usb_label, EVENT_DIR_NAME)
    create_dirs(SD_PATH, event_path)
    shared_bike_width.value = get_bike_width_cm(f"/media/pi/{usb_label}/settings/bikewidth.txt")
    shared_sensor_offset.value = get_sensor_offset_cm(f"/media/pi/{usb_label}/settings/sensoroffset.txt")
    shared_dist_zul.value = get_dist_zul(f"/media/pi/{usb_label}/settings/dist_zul.txt")
    shared_partial_video_seconds.value = get_partial_video_seconds(f"/media/pi/{usb_label}/settings/video_sekunden.txt")
    
    # logo unten links einfügen mit Alpha-Kanal laden (wichtig für Transparenz)
    logo_path = f"/media/pi/{usb_label}/static/logo200.png"
    logo = cv2.imread(logo_path, cv2.IMREAD_UNCHANGED)
    if logo is None:
        logger.error(f"Logo-Datei nicht gefunden: {logo_path}")
    scale_percent = 40
    width = int(logo.shape[1] * scale_percent / 100)
    height = int(logo.shape[0] * scale_percent / 100)
    logo = cv2.resize(logo, (width, height), interpolation=cv2.INTER_AREA)
    #frame_height, frame_width = FRAME_HEIGHT, FRAME_WIDTH
    logo_height, logo_width = logo.shape[:2]
    x_logo = 10  # links unten
    y_logo = FRAME_HEIGHT - logo_height-10  # unten
    logo_position = (x_logo, y_logo)
    
    sensor_proc = Process(target=sensor_worker, args=(shared_distance, shared_bike_width, shared_sensor_offset), daemon=True)
    sensor_proc.start()

    
    #GPS Daten holen
    # Manager Dict zur gemeinsamen Nutzung der GPS-Daten zwischen Prozessen
    manager = Manager()
    gps_data = manager.dict({
        'timestamp': 0,
        'lat': 0.0,
        'lon': 0.0,
        'speed': 0.0,
        'heading': 0.0
    })
    # GPS Empfangsprozess mit gps_receiver.py
    gps_proc = Process(target=gps_worker_process, args=(gps_data,), daemon=True)
    gps_proc.start()

    #Da alle Werte nun bekannt sind, WebApp starten
    start_webapp(shared_distance, shared_abstand, shared_event_state, shared_bike_width, shared_sensor_offset, gps_data)

    picam2 = Picamera2()
    video_config = picam2.create_video_configuration(main={"size": (FRAME_WIDTH, FRAME_HEIGHT), "format": "BGR888"})
    picam2.configure(video_config)
    picam2.set_controls({"FrameRate": FPS})
    time.sleep(0.25)
    picam2.start()
    time.sleep(0.5)

    # Save-Thread mit led_ctrl referenz starten
    global save_thread
    save_thread = threading.Thread(target=save_worker, args=(led_ctrl,), daemon=True)
    save_thread.start()

    writer_thread, writer_path = make_writer_thread()
    csv_path = writer_path.replace('.mp4', '.csv')
    csv_file = open(csv_path, 'w', newline='')
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow(['timestamp', 'distance_cm', 'lat', 'lon', 'speed_kmh', 'heading_deg', 'status1', 'status2'])

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale_dynamic = 0.6
    thickness_dynamic = 1 
    color_dynamic_green = (0, 255, 0)
    color_dynamic_red = (255, 0, 0) # ist eigentlich blau in BGR. Die Kanäle werden später aber wieder vertauscht, sodass rot rauskommt. 
    
    #Koordinaten ROI für Text overlay (region of interest = der zu rendernde Bereich)
    #statisches ROI
    # Für dynamische Skalierung:
    x, y = 0, 0
    w = FRAME_WIDTH
    h = int(FRAME_HEIGHT * 0.027)  # z.B. ca. 2.7% der Höhe (30/1080 ≈ 0.027)


    logger.info("Starte Aufnahme.")
    led_ctrl.set_blink()
    frame_count = 0
    shared_event_state.value = False 
    start_time = time.monotonic()
    next_frame_time = start_time
    try:
        sensor_last1 = 300
        sensor_last2 = 300
        sensor_last3 = 300
        abstand = 300
        status1 = False
        status2 = False
        
        # statisches Overlay speichern:        
        while True:
 #           #auber Abbrechen, wenn Sytem-Shutdown ausgelöst wurde
 #           if shutdown_flag.is_set():
 #               raise KeyboardInterrupt  # Nutzt die bestehende Cleanup-Logik sauber

            frame = picam2.capture_array()
            sensorwert = shared_distance.value
            PARTIAL_FRAMES = FPS * shared_partial_video_seconds.value
            # Sensorwert-Logik
            if sensor_last1 == 300 and sensor_last2 == 300 and sensor_last3 == 300 and sensorwert < 300:
                abstand = sensorwert
                shared_abstand.value = abstand #zum Senden an Bluetooth Empfänger
            elif sensorwert < abstand:
                abstand = sensorwert
                shared_abstand.value = abstand  #zum Senden an Bluetooth Empfänger

            sensor_last3 = sensor_last2
            sensor_last2 = sensor_last1
            sensor_last1 = sensorwert

            if sensorwert <= shared_dist_zul.value:
                shared_event_state.value = True

            if shared_event_state.value:
                led_ctrl.set_blink_interval(0.2)
            else:
                led_ctrl.set_blink_interval(0.6)

            gps_info = {
                'timestamp': gps_data.get('timestamp', int(time.time() * 1000)),
                'lat': gps_data.get('lat', None),
                'lon': gps_data.get('lon', None),
                'speed': gps_data.get('speed', None),
                'heading': gps_data.get('heading', None),
            }
            
            speed = gps_info.get('speed', None)
            speed_text = f"{speed:.1f}" if speed and speed > 0 else "--,- "
            csv_writer.writerow([
                gps_info['timestamp'],
                sensorwert,
                gps_info['lat'],
                gps_info['lon'],
                gps_info['speed'],
                gps_info['heading'],
                status1,
                status2,
            ])

            farbe_sensorwert = color_dynamic_red if sensorwert <= shared_dist_zul.value else color_dynamic_green
            farbe_abstand = color_dynamic_red if abstand <= shared_dist_zul.value else color_dynamic_green
            
            if shared_overlay_update_flag.value is True:
                shared_overlay_update_flag.value = False  # Flag zurücksetzen
                last_bike_width = shared_bike_width.value
                last_sensor_offset = shared_sensor_offset.value

                static_roi_overlay = create_static_overlay(
                    last_bike_width,
                    last_sensor_offset,
                    w, h,
                    font,
                    font_scale_dynamic,
                    (255, 255, 255),
                    1
                )

    
            #statisches roi anlegen
            roi = frame[y:y + h, x:x + w]
            #statisches Overlay
            roi[:] = static_roi_overlay
            
            #dynamische roi anlegen
            roi1_x = int(FRAME_WIDTH * 1000 / 1920)
            roi1_w = int(FRAME_WIDTH * 140 / 1920)

            roi2_x = int(FRAME_WIDTH * 1440 / 1920)
            roi2_w = int(FRAME_WIDTH * 70 / 1920)

            roi3_x = int(FRAME_WIDTH * 1514 / 1920)
            roi3_w = int(FRAME_WIDTH * 70 / 1920)

            roi4_x = int(FRAME_WIDTH * 1715 / 1920)
            roi4_w = int(FRAME_WIDTH * 75 / 1920)

            roi1 = frame[y:y+h, roi1_x:roi1_x+roi1_w]
            roi2 = frame[y:y+h, roi2_x:roi2_x+roi2_w]
            roi3 = frame[y:y+h, roi3_x:roi3_x+roi3_w]
            roi4 = frame[y:y+h, roi4_x:roi4_x+roi4_w]
            
            #dynamische text in dynamische roi schreiben
            cv2.putText(roi1, f"{datetime.now().strftime('%H:%M:%S')}",(0, int(h*0.85)), font, font_scale_dynamic, (255, 255, 255), 1, cv2.LINE_AA)
            cv2.putText(roi2, f"{sensorwert}",(0, int(h*0.85)), font, font_scale_dynamic, farbe_sensorwert, thickness_dynamic, cv2.LINE_AA)
            cv2.putText(roi3, f"{abstand}",(0, int(h*0.85)), font, font_scale_dynamic, farbe_abstand, thickness_dynamic, cv2.LINE_AA)
            cv2.putText(roi4, f"{speed_text}",(0, int(h*0.85)), font, font_scale_dynamic, (255, 255, 255), thickness_dynamic, cv2.LINE_AA)

            #farbkanäle des videos tauschen
            frame = frame[:, :, [2, 1, 0]]  #opencv nutzt Farbkanäle im format bgr. hier auf rgb zurück tauschen
            
            #Logo auf Bild setzen (wichtig: nach dem tauschen der Farbkanäle, damit die logofarben nicht verändert werden)
            overlay_logo(frame, logo, logo_position)
            
            writer_thread.write(frame)
            frame_count += 1
            
            if frame_count >= PARTIAL_FRAMES:
                led_ctrl.set_on()
                
                # >>> Neuen Writer sofort starten <<<
                new_writer_thread, new_writer_path = make_writer_thread()
                new_csv_path = new_writer_path.replace('.mp4', '.csv')
                new_csv_file = open(new_csv_path, 'w', newline='')
                new_csv_writer = csv.writer(new_csv_file)
                new_csv_writer.writerow(['timestamp', 'distance_cm', 'lat', 'lon', 'speed_kmh', 'heading_deg', 'status1', 'status2'])
                
                # >>> Das alte Paket asynchron speichern <<<
                stop_and_queue_save(writer_thread, csv_file, shared_event_state, writer_path, csv_path, event_path)
                
                # >>> Referenzen auf den neuen Writer setzen <<<
                writer_thread = new_writer_thread
                writer_path = new_writer_path
                csv_path = new_csv_path
                csv_file = new_csv_file
                csv_writer = new_csv_writer
                
                led_ctrl.set_blink()
                frame_count = 0
                shared_event_state.value = False


    except KeyboardInterrupt:
        logger.info("Abbruch durch User.")
        csv_file.close()
    finally:
        lgpio.gpio_write(h_ruecklicht, RUECKLICHT_PIN, 0)  # Rücklicht OFF
        lgpio.gpiochip_close(h_ruecklicht)

        try:
            led_ctrl.set_on()

            if frame_count > 0:
                logger.info("Shutdown: letzte Videoaufnahme prüfen und speichern/löschen.")
                stop_and_queue_save(writer_thread, csv_file, shared_event_state, writer_path, csv_path, event_path)
            else:
                csv_file.close()

        except Exception as e:
            logger.error(f"Fehler im finally Block: {e}")
        
        picam2.stop()
        sensor_proc.terminate()
        sensor_proc.join()

        gps_proc.terminate()
        gps_proc.join()

        # Save-Worker beenden
        save_queue.put(None)
        save_thread.join()

        led_ctrl.set_off()
        led_ctrl.stop()
        led_ctrl.join()

        queue_listener.stop()

        logger.info("Programmende und aufgeräumt.")



if __name__ == "__main__":
    
     # Bluetooth sender Prozess mit shared_distance starten
    ''' Bluetooth export des letzten Überholabstandes
    # hat viel Performance gekostet. optimieren oder löschen
    bt_process = Process(target=BT_Sender.run_bt_sender, args=(shared_abstand,), daemon=True)
    bt_process.start()
    '''
    
    #Hauptprogramm starten
    main()
