# This file is executed on every boot (including wake-boot from deepsleep)
#import esp
#esp.osdebug(None)
#import webrepl
#webrepl.start()
import network
import time
import uasyncio as asyncio
from ws import AsyncWebsocketClient
from machine import UART, Pin
import json
import os
import ntptime
import sys

# Configuration file path
CONFIG_FILE = "./config.json"

# Global configuration variables
config = {}
current_websocket = None

async def send_log_to_websocket(websocket, message):
    """Sends a log message to the WebSocket if connected."""
    if websocket and await websocket.open():
        try:
            timestamp = time.localtime()
            formatted_time = f"{timestamp[0]}-{timestamp[1]:02d}-{timestamp[2]:02d} {timestamp[3]:02d}:{timestamp[4]:02d}:{timestamp[5]:02d}"
            log_data = {
                "type": "LOG",
                "timestamp": formatted_time,
                "message": message
            }
            await websocket.send(json.dumps(log_data))
        except Exception as e:
            print(f"Error sending log to WebSocket: {e}")
    print(message)

def set_current_websocket(websocket):
    global current_websocket
    current_websocket = websocket

def log_message(message):
    """Logs a message to the WebSocket and prints it."""
    print(message)  # Always print to console
    if current_websocket:
        asyncio.create_task(send_log_to_websocket(current_websocket, message))

def load_config():
    global config
    try:
        with open(CONFIG_FILE, "r") as file:
            config = json.load(file)
            if not config.get("chair") or not config.get("server"):
                raise ValueError("Required keys are missing in config.json")
    except Exception as e:
        log_message(f"Error loading config: {e}")
        raise

def connect_wifi():
    wifi_config = config.get("WIFI", {})
    ssid = wifi_config.get("ssid", "")
    password = wifi_config.get("password", "")
    if not ssid or not password:
        log_message("Wi-Fi SSID or password is missing in config.json")
        raise ValueError("Wi-Fi SSID or password is missing in config.json")

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    retries = 5
    while retries > 0:
        try:
            log_message(f"Attempting to connect to Wi-Fi: SSID={ssid}")
            wlan.connect(ssid, password)
            for _ in range(10):
                if wlan.isconnected():
                    log_message(f"Connected to Wi-Fi: {wlan.ifconfig()}")
                    return
                time.sleep(1)
            log_message("Wi-Fi connection attempt timed out. Retrying...")
        except Exception as e:
            log_message(f"Error during Wi-Fi connection: {e}")
        retries -= 1
        time.sleep(5)

    log_message("Failed to connect to Wi-Fi after multiple attempts.")
    raise OSError("Wi-Fi Internal Error")

def initialize_uart():
    chair_config = config.get("chair", {})
    baudrate = chair_config.get("baudrate", 9600)
    uart = UART(1, baudrate=baudrate, tx=Pin(18), rx=Pin(17))
    log_message(f"Initialized UART with baudrate {baudrate}")
    return uart

async def handle_uart(websocket, uart):
    FRAME_SIZE = 26
    log_message("Starting UART handler")

    while True:
        try:
            if not websocket or not await websocket.open():
                log_message("WebSocket is closed in handle_uart")
                return

            if uart.any() >= FRAME_SIZE:
                data = uart.read(FRAME_SIZE)
                if data and len(data) == FRAME_SIZE and data[0] == 0x55 and data[1] == 0xAA:
                    log_message(f"Valid UART frame: {data}")
                    hb = {"type": "HB", "value": data.hex()}
                    await websocket.send(json.dumps(hb))
                    log_message(f"Sent to WebSocket: {hb}")
        except Exception as e:
            log_message(f"UART handling error: {e}")
            return

        await asyncio.sleep(0.1)  # Check every 100ms

async def handle_websocket(websocket, uart):
    FRAME_SIZE = 26
    log_message("Starting WebSocket handler")
    while True:
        try:
            if websocket and await websocket.open():
                message = await websocket.recv()
                if message:
                    try:
                        data = json.loads(message)
                        if "stopTime" in data:
                            stop_time = data["stopTime"]
                            log_message(f"Received stopTime: {stop_time}")
                            with open(CONFIG_FILE, "r") as file:
                                config_data = json.load(file)
                            config_data["state"]["stopTime"] = stop_time
                            with open(CONFIG_FILE, "w") as file:
                                json.dump(config_data, file)
                            log_message(f"Saved stopTime to config.json: {stop_time}")

                        if data["type"] == "control":
                            value = data["command"]["value"]
                            command = bytes.fromhex(value.replace("_", " "))
                            log_message(f"Converted command: {command}")
                            uart.write(command)
                            log_message(f"Sent to UART: {command}")
                            await websocket.send(json.dumps({"command_result": "OK", "value": "OK", "type": "response"}))
                            if uart.any() >= FRAME_SIZE:
                                data = uart.read(FRAME_SIZE)
                                if data and len(data) == FRAME_SIZE:
                                    if data[0] == 0x55 and data[1] == 0xAA:
                                        log_message(f"Valid UART frame: {data}")
                                        hb = {"type": "HB", "value": data.hex()}
                                        await websocket.send(json.dumps(hb))
                                        log_message(f"Sent to WebSocket: {hb}")
                    except Exception as e:
                        log_message(f"Error processing message: {e}")
            else:
                log_message("WebSocket is closed or not available")
                return
        except Exception as e:
            log_message(f"WebSocket handling error: {e}")
            return
        await asyncio.sleep(0.1)

def parse_iso_time(iso_time):
    try:
        date_part, time_part = iso_time.split(" ")
        year, month, day = map(int, date_part.split("-"))
        hour, minute, second = map(int, time_part.split(":"))
        return time.mktime((year, month, day, hour, minute, second, 0, 0))
    except Exception as e:
        log_message(f"Error parsing ISO time: {e}")
        return None

def clear_stop_time():
    try:
        with open(CONFIG_FILE, "r") as file:
            config_data = json.load(file)
        if "state" in config_data and "stopTime" in config_data["state"]:
            del config_data["state"]["stopTime"]
        with open(CONFIG_FILE, "w") as file:
            json.dump(config_data, file)
        log_message("Cleared stopTime from config.json")
    except Exception as e:
        log_message(f"Error clearing stopTime: {e}")

async def monitor_stop_time(uart, websocket):
    while True:
        try:
            with open(CONFIG_FILE, "r") as file:
                config_data = json.load(file)
            stop_time_str = config_data.get("state", {}).get("stopTime", None)
            if stop_time_str:
                stop_time = parse_iso_time(stop_time_str)
                current_time = time.time() + 8 * 3600
                log_message(f"Current time (UTC+8): {current_time}, Stop time: {stop_time}")
                if stop_time and current_time >= stop_time:
                    uart_command = bytes.fromhex("59 59 06 02 02 BC")
                    uart.write(uart_command)
                    log_message(f"Sent to UART: {uart_command}")
                    clear_stop_time()
            #else:
                #log_message("No stopTime found in config.json")
        except Exception as e:
            log_message(f"Error in monitoring stopTime: {e}")
            return
        await asyncio.sleep(10)

async def main():
    while True:  # Outer loop to handle complete restarts
        try:
            # Initialize everything from scratch
            load_config()
            connect_wifi()
            server_config = config.get("server", {})
            ws_url = f"{server_config.get('url', '')}/{config.get('chair', {}).get('id', '')}"
            log_message(f"WebSocket URL: {ws_url}")
            reconnect_delay = server_config.get("reconnect_delay", 30)

            uart = initialize_uart()
            
            while True:  # Inner loop for WebSocket connection
                websocket = None
                try:
                    # Check WiFi connection
                    wlan = network.WLAN(network.STA_IF)
                    if not wlan.isconnected():
                        log_message("WiFi disconnected, reconnecting...")
                        raise OSError("WiFi disconnected")
                    
                    retries = 5
                    while retries > 0:
                        try:
                            ntptime.host = "3.my.pool.ntp.org"
                            ntptime.settime()
                            log_message(f"Local time after synchronization: {time.localtime()}")
                            break
                        except Exception as e:
                            log_message(f"Error synchronizing time: {e}")
                            retries -= 1
                            time.sleep(2)

                    if retries == 0:
                        log_message("Failed to synchronize time after multiple attempts.")

                    log_message("Connecting to WebSocket...")
                    websocket = AsyncWebsocketClient()
                    connected = await websocket.handshake(ws_url)

                    if not connected:
                        log_message("Failed to connect to WebSocket")
                        raise OSError("WebSocket connection failed")

                    # Set the current websocket for logging
                    set_current_websocket(websocket)
                    log_message("Connected to WebSocket")

                    await asyncio.gather(
                        handle_uart(websocket, uart),
                        handle_websocket(websocket, uart),
                        monitor_stop_time(uart, websocket)
                    )

                except Exception as e:
                    log_message(f"Connection error: {e}")
                    if websocket:
                        try:
                            await websocket.close()
                        except:
                            pass
                    set_current_websocket(None)
                    
                    # Cancel all tasks
                    try:
                        uart_task.cancel()
                        ws_task.cancel()
                        monitor_task.cancel()
                    except:
                        pass

                    if isinstance(e, OSError) and "WiFi disconnected" in str(e):
                        break
                    
                    log_message(f"Waiting {reconnect_delay} seconds before reconnecting...")
                    await asyncio.sleep(reconnect_delay)

        except Exception as e:
            log_message(f"Critical error in main loop: {e}")
            sys.print_exception(e)
            await asyncio.sleep(reconnect_delay)

loop = asyncio.get_event_loop()
loop.run_until_complete(main())

