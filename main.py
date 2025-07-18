import uasyncio as asyncio
import json
import network
import time
from ws import AsyncWebsocketClient
from machine import UART, Pin
import os
import ntptime
import sys
from uwebsockets.client import connect

# Configuration file path
CONFIG_FILE = "./config.json"

# Global configuration variables
config = {}

def load_config():
    global config
    try:
        with open(CONFIG_FILE, "r") as file:
            config = json.load(file)
            if not config.get("chair") or not config.get("server"):
                raise ValueError("Required keys are missing in config.json")
    except Exception as e:
        print(f"Error loading config: {e}")
        raise

def connect_wifi():
    wifi_config = config.get("WIFI", {})
    ssid = wifi_config.get("ssid", "")
    password = wifi_config.get("password", "")
    if not ssid or not password:
        print("Wi-Fi SSID or password is missing in config.json")
        raise ValueError("Wi-Fi SSID or password is missing in config.json")

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    retries = 5
    while retries > 0:
        try:
            print(f"Attempting to connect to Wi-Fi: SSID={ssid}")
            wlan.connect(ssid, password)
            for _ in range(10):
                if wlan.isconnected():
                    print(f"Connected to Wi-Fi: {wlan.ifconfig()}")
                    return
                time.sleep(1)
            print("Wi-Fi connection attempt timed out. Retrying...")
        except Exception as e:
            print(f"Error during Wi-Fi connection: {e}")
        retries -= 1
        time.sleep(5)

    print("Failed to connect to Wi-Fi after multiple attempts.")
    raise OSError("Wi-Fi Internal Error")

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
        print("Cleared stopTime from config.json")
    except Exception as e:
        print(f"Error clearing stopTime: {e}")

async def track(uart):
    while True:
        try:
            with open(CONFIG_FILE, "r") as file:
                config_data = json.load(file)
            stop_time = config_data.get("state", {}).get("stopTime", None)
            print(stop_time)
            if stop_time:
                current_time = time.time()
                print(f"Current time (UTC+8): {current_time}, Stop time: {stop_time}")
                if stop_time and current_time >= stop_time:
                    uart_command = bytes.fromhex("59 59 06 02 02 BC")
                    uart.write(uart_command)
                    print(f"Sent to UART: {uart_command}")
                    clear_stop_time()
            else:
                print("No stopTime found in config.json")
        except Exception as e:
            print(f"Error in monitoring stopTime: {e}")
            break
        await asyncio.sleep(5)

async def recv_message(websocket, timeout):
    """
    Coroutine to handle asynchronous recv with a timeout.
    """
    try:
        message = await asyncio.wait_for(websocket.recv(), timeout)
        if not message:
            print("WebSocket disconnected or received empty message")
            return {"type":"None"}
        print(f"Message received: {message}")
        return message
    except asyncio.TimeoutError:
        print("Timeout while waiting for message")
        raise
    except Exception as e:
        print(f"Error in recv_message: {e}")
        raise

async def ping_pong_task(websocket, interval=10):
    """
    Periodically send a custom ping message to the WebSocket server.
    """
    while True:
        try:
            ping_message = json.dumps({"type": "ping"})
            print("Sending ping to WebSocket server...")
            await websocket.send(ping_message)  # Send a custom ping message
            await asyncio.sleep(interval)
        except Exception as e:
            print(f"Error sending ping: {e}")
            break

def is_wifi_connected():
    wlan = network.WLAN(network.STA_IF)
    return wlan.isconnected()
    
def send_command(uart,message):
    try:
        if message.replace("_", " ") == '59 59 06 02 01 BB':
            command = bytes.fromhex(message.replace("_", " "))
            print(command)
            uart.write(command)
            command = bytes.fromhex("59 59 06 04 1E DA")
            print(command)
            uart.write(command)
        elif message.replace("_", " ") == '59 59 06 02 02 BC':
            command = bytes.fromhex("59 59 06 03 01 BC")
            print(command)
            uart.write(command)
            command = bytes.fromhex(message.replace("_", " "))
            print(command)
            uart.write(command)
        else:
            command = bytes.fromhex(message.replace("_", " "))
            print(command)
            uart.write(command)
        return "Accepted"
    except Exception as e:
        print(f"Serial communication error: {e}")
        return "Serial communication error"

def reboot_system():
    print("Rebooting the system...")

async def actions(uart):
    print("Started actions")
    load_config()
    # Check if Wi-Fi is already connected
    if not is_wifi_connected():
        print("Wi-Fi not connected. Attempting to connect...")
        connect_wifi()
    else:
        wlan = network.WLAN(network.STA_IF)
        print(f"Wi-Fi already connected: {wlan.ifconfig()}")
    uri = f"{config['server']['url']}/{config['chair']['id']}"
    reconnect_interval = config.get('server', {}).get('reconnect_interval', 5)
    receive_timeout = config.get('server', {}).get('receive_timeout', 5)
    while True:
        websocket = None
        try:
            print(f"Connecting to {uri}...")
            websocket = AsyncWebsocketClient()
            connected = await websocket.handshake(uri)
            if websocket and await websocket.open():
                print("Connected to WebSocket")
                # Start the ping task
                while True:
                    try:
                        message = await recv_message(websocket, receive_timeout)
                        if not message.strip():
                            print("Received an empty message, reconnecting...")
                            break  # Break inner loop to reconnect
                        print(f"Received: {message}")
                        data = json.loads(message)
                        if data['type'] == 'control':
                            if "minutes" in data:
                                minutes = data["minutes"]
                                print(f"Received minutes: {minutes}")
                                with open(CONFIG_FILE, "r") as file:
                                    config_data = json.load(file)
                                stop_time = time.time() + minutes * 60
                                config_data["state"]["stopTime"] = stop_time
                                with open(CONFIG_FILE, "w") as file:
                                    json.dump(config_data, file)
                            print(f"Saved stopTime to config.json: {stop_time}")
                            command = data['command']
                            action = command['action']
                            value = command.get('value')
                            if action == 'remote':
                                code = value
                                response = send_command(uart, code)
                            elif action == 'update_config':
                                #save_config((value['key'], value['value']), update_type='partial')
                                response = f"Configuration key '{value['key']}' updated successfully"
                            elif action == 'reboot':
                                response = reboot_system()
                            else:
                                response = "Invalid command"
                            
                            await websocket.send(json.dumps({
                                "type": "command_result",
                                "chair_id": config['chair']['id'],
                                "result": response
                            }))
                            
                            if action == 'reboot':
                                # Close the websocket connection before reboot
                                await websocket.close()
                                return
                    except asyncio.TimeoutError:
                        print(f"No message received for {receive_timeout} seconds, reconnecting...")
                        break  # Changed from return to break
                    except Exception as e:
                        print(f"Error processing message: {e}")
                        break  # Changed from return to break
        except Exception as e:
            print(f"Failed to connect: {e}")
            # Removed return statement
        finally:
            if websocket:
                await websocket.close()
                print("WebSocket connection closed")
        
        print(f"Reconnecting to WebSocket in {reconnect_interval} seconds...")
        await asyncio.sleep(reconnect_interval)

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

        
def initialize_uart():
    chair_config = config.get("chair", {})
    baudrate = chair_config.get("baudrate", 9600)
    uart = UART(1, baudrate=baudrate, tx=Pin(18), rx=Pin(17))
    print(f"Initialized UART with baudrate {baudrate}")
    return uart

async def run_tasks():
    while True:
        try:
            load_config()
            connect_wifi()
            uart = initialize_uart()
            retries = 5
            
            tasks = await asyncio.gather(
                track(uart),
                actions(uart),
                receive_status(uart),
                return_exceptions=True  # Add this to handle exceptions
            )
            
            # If any task completed (which means it failed), raise an exception
            # to restart all tasks
            if any(isinstance(t, Exception) for t in tasks):
                print("Task failed, restarting all tasks...")
                continue

        except Exception as e:
            print(f"Exception occurred: {e}")
            print("Restarting tasks...")
            # Optional delay before restarting to prevent rapid restarts
            await asyncio.sleep(1)

# Start the event loop
try:
    asyncio.run(run_tasks())
except KeyboardInterrupt:
    print("Program stopped.")

