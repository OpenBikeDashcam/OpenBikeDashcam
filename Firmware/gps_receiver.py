#License: GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007

import socket
import time

TCP_IP = '0.0.0.0'
TCP_PORT = 55555
BUFFER_SIZE = 1024
SOCKET_ACCEPT_TIMEOUT = 5.0  # Sekunden Timeout für accept()
SOCKET_RECV_TIMEOUT = 5.0    # Sekunden Timeout für recv()

def nmea_to_decimal(coord, direction):
    """
    Wandelt NMEA-Koordinaten (Grad und Minuten) in Dezimalgrad um.
    coord: z.B. '4807.038' (DDMM.MMMM)
    direction: 'N', 'S', 'E', 'W'
    """
    if not coord or not direction:
        return 0.0

    try:
        if direction in ['N', 'S']:
            degrees = int(coord[0:2])
            minutes = float(coord[2:])
        elif direction in ['E', 'W']:
            degrees = int(coord[0:3])
            minutes = float(coord[3:])
        else:
            return 0.0

        decimal = degrees + minutes / 60.0
        if direction in ['S', 'W']:
            decimal = -decimal
        return decimal
    except (ValueError, IndexError):
        return 0.0

class GPSReceiver:
    def __init__(self, ip=TCP_IP, port=TCP_PORT):
        self.TCP_IP = ip
        self.TCP_PORT = port
        self.BUFFER_SIZE = BUFFER_SIZE
        self.accept_timeout = SOCKET_ACCEPT_TIMEOUT
        self.recv_timeout = SOCKET_RECV_TIMEOUT
        self.sock = None
        self.conn = None

    def start(self):
        # Setup des Listening-Sockets
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.accept_timeout)  # Timeout für accept()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.TCP_IP, self.TCP_PORT))
        self.sock.listen(1)
        print(f"Listening for GPS2IP data on port {self.TCP_PORT}...")
        self._accept_connection_loop()

    def _accept_connection_loop(self):
        """
        Wartet auf eine eingehende Verbindung, mit Timeout und erneuter Versuchsschleife.
        """
        while True:
            try:
                self.conn, addr = self.sock.accept()
                print(f"Connection from: {addr}")
                self.conn.settimeout(self.recv_timeout)  # Timeout für recv()
                break
            except socket.timeout:
                pass
                #print("Timeout beim Warten auf neue Verbindung. Warte weiter...")
            except OSError as e:
                print(f"Socket-Fehler beim accept: {e}, warte 1 Sekunde und versuche neu.")
                time.sleep(2)

    def receive_sentence(self):
        """
        Versucht eine NMEA-Nachricht zu empfangen.
        Wenn keine Verbindung besteht oder Timeout auftritt, versucht automatisch reconnect.
        Rückgabe:
          - str: NMEA-Satz ohne EOL und leer bei Timeout oder Fehler
          - None: wenn Verbindung geschlossen und keine neue Verbindung möglich
        """
        while True:
            if self.conn is None:
                # Keine aktive Verbindung, versuche neue anzunehmen
                try:
                    self._accept_connection_loop()
                except Exception as e:
                    print(f"Fehler beim Verbindungsaufbau: {e}")
                    time.sleep(2)
                    continue

            try:
                data = self.conn.recv(self.BUFFER_SIZE)
                if not data:
                    # Verbindung geschlossen vom Client, Verbindung kappen und neu verbinden
                    print("Verbindung vom Client geschlossen, versuche reconnect...")
                    self.conn.close()
                    self.conn = None
                    continue
                nmea_sentence = data.decode('utf-8', errors='ignore').strip()
                print(f"Received NMEA sentence: {nmea_sentence}")
                return nmea_sentence
            except socket.timeout:
                # Timeout - keine Daten empfangen, gib einen "leeren Satz" zurück
                return ''
            except (ConnectionResetError, ConnectionAbortedError):
                print("Verbindung vom Client zurückgesetzt, reconnect wird versucht.")
                if self.conn:
                    self.conn.close()
                self.conn = None
                continue
            except Exception as e:
                print(f"Unerwarteter Fehler beim empfangen: {e}")
                if self.conn:
                    self.conn.close()
                self.conn = None
                time.sleep(2)
                continue

    def parse_nmea_sentence(self, nmea_sentence):
        """
        Parst einen NMEA Satz (z.B. GPGGA oder GPRMC) und gibt
        ein Dictionary mit relevanten Daten inkl. umgewandelter Koordinaten zurück.
        Falls leerer oder ungültiger Satz, return dict mit 0-Koordinaten.
        """
        if not nmea_sentence:
            # Leerer Satz, z.B. Timeout
            return {
                "type": "NO_DATA",
                "latitude": 0.0,
                "longitude": 0.0,
                "fix_quality": 0,
                "num_satellites": 0,
                "horizontal_dilution": 0.0,
                "altitude": 0.0,
                "altitude_units": '',
                "geoid_height": 0.0,
                "geoid_units": '',
            }

        fields = nmea_sentence.split(',')

        if fields[0].startswith('$GPGGA') and len(fields) >= 15:
            try:
                latitude = nmea_to_decimal(fields[2], fields[3])
                longitude = nmea_to_decimal(fields[4], fields[5])
                fix_quality = int(fields[6]) if fields[6].isdigit() else 0
                num_satellites = int(fields[7]) if fields[7].isdigit() else 0
                horizontal_dilution = float(fields[8]) if fields[8] else 0.0
                altitude = float(fields[9]) if fields[9] else 0.0
                altitude_units = fields[10]
                geoid_height = float(fields[11]) if fields[11] else 0.0
                geoid_units = fields[12]
            except (ValueError, IndexError):
                # Ungültige Daten, 0-Koordinaten zurückgeben
                return {
                    "type": "NO_DATA",
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "fix_quality": 0,
                    "num_satellites": 0,
                    "horizontal_dilution": 0.0,
                    "altitude": 0.0,
                    "altitude_units": '',
                    "geoid_height": 0.0,
                    "geoid_units": '',
                }

            return {
                "type": "GPGGA",
                "utc_time": fields[1],
                "latitude": latitude,
                "longitude": longitude,
                "fix_quality": fix_quality,
                "num_satellites": num_satellites,
                "horizontal_dilution": horizontal_dilution,
                "altitude": altitude,
                "altitude_units": altitude_units,
                "geoid_height": geoid_height,
                "geoid_units": geoid_units,
            }

        elif fields[0].startswith('$GPRMC') and len(fields) >= 12:
            try:
                latitude = nmea_to_decimal(fields[3], fields[4])
                longitude = nmea_to_decimal(fields[5], fields[6])
                speed = float(fields[7]) if fields[7] else 0.0  # Knoten
                course = float(fields[8]) if fields[8] else 0.0  # Grad
                mag_var = fields[10]
                mag_var_dir = fields[11].split('*')[0] if len(fields) > 11 else ''
            except (ValueError, IndexError):
                return {
                    "type": "NO_DATA",
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "fix_quality": 0,
                }

            return {
                "type": "GPRMC",
                "utc_time": fields[1],
                "status": fields[2],
                "latitude": latitude,
                "longitude": longitude,
                "speed_knots": speed,
                "course": course,
                "date": fields[9],
                "magnetic_variation": f"{mag_var} {mag_var_dir}".strip(),
            }
        else:
            # Andere Sätze: einfach ignorieren, 0-Koordinaten zurückgeben
            return {
                "type": "NO_DATA",
                "latitude": 0.0,
                "longitude": 0.0,
                "fix_quality": 0,
            }

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None
        if self.sock:
            self.sock.close()
            self.sock = None


def gps_generator():
    """
    Generator-Funktion die GPSReceiver nutzt,
    nonstop Werte (auch Nullwerte) liefert, ohne zu blockieren.
    """
    gps = GPSReceiver()
    gps.start()
    try:
        while True:
            sentence = gps.receive_sentence()
            data = gps.parse_nmea_sentence(sentence)
            yield data
    except KeyboardInterrupt:
        print("GPS receiver interrupted by user.")
    finally:
        gps.close()

if __name__ == "__main__":
    print("Starte GPSReceiver")
    for gps_data in gps_generator():
        print("GPS Data:", gps_data)
