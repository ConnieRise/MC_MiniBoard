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

def log_message(msg):
    print(msg)


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
    
def save_config(key_value_tuple, update_type="partial"):
    """
    Update the config.json file with the new key-value pair.
    key_value_tuple: tuple of ("key.path.in.config", new_value)
    update_type: "partial" to only update that key, "full" to overwrite the whole config
    """
    try:
        key_path, new_value = key_value_tuple
        keys = key_path.split(".")

        with open(CONFIG_FILE, "r") as f:
            current_config = json.load(f)

        if update_type == "partial":
            d = current_config
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = new_value
        else:
            current_config = new_value  # overwrite everything

        with open(CONFIG_FILE, "w") as f:
            json.dump(current_config, f)

        print(f"Config updated: {key_path} = {new_value}")
        return True
    except Exception as e:
        print(f"Failed to update config: {e}")
        return False

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

async def actions(uart, websocket):
    print("Started actions")
    load_config()

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
        try:
            print(f"Connecting to {uri}...")
            connected = await websocket.handshake(uri)
            if websocket and await websocket.open():
                print("Connected to WebSocket")

                while True:
                    try:
                        message = await recv_message(websocket, receive_timeout)
                        if not message.strip():
                            print("Received an empty message, reconnecting...")
                            break

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
                            action = command.get('action')
                            value = command.get('value')
                            should_send_result = True
                            response = ""

                            if action == 'remote':
                                code = value
                                response = send_command(uart, code)

                            elif action == 'get_config':
                                # try:
                                #     with open(CONFIG_FILE, "r") as f:
                                #         current_config = json.load(f)
                                #     response = {
                                #         "type": "config_data",
                                #         "chair_id": config['chair']['id'],
                                #         "data": current_config
                                #     }
                                # except Exception as e:
                                #     response = {
                                #         "type": "config_data",
                                #         "chair_id": config['chair']['id'],
                                #         "error": f"Failed to read config: {str(e)}"
                                #     }

                                test_data = {
                                    "type": "config_data",
                                    "msg": "hello from device"
                                }
                                await websocket.send(json.dumps(test_data))

                                # await websocket.send(json.dumps(response))
                                should_send_result = False  # skip command_result

                            elif action == 'update_config':
                                save_config((value['key'], value['value']), update_type='partial')
                                response = f"Configuration key '{value['key']}' updated successfully"

                            elif action == 'reboot':
                                response = reboot_system()

                            else:
                                response = "Invalid command"

                            if should_send_result:
                                await websocket.send(json.dumps({
                                    "type": "command_result",
                                    "chair_id": config['chair']['id'],
                                    "result": response
                                }))

                            if action == 'reboot':
                                await websocket.close()
                                return

                    except asyncio.TimeoutError:
                        print(f"No message received for {receive_timeout} seconds, reconnecting...")
                        break
                    except Exception as e:
                        print(f"Error processing message: {e}")
                        break

        except Exception as e:
            print(f"Failed to connect: {e}")
        finally:
            if websocket:
                await websocket.close()
                print("WebSocket connection closed")

        print(f"Reconnecting to WebSocket in {reconnect_interval} seconds...")
        await asyncio.sleep(reconnect_interval)

async def receive_status(uart, websocket):
    FRAME_SIZE = 26
    log_message("Starting UART status receiver")

    if not await websocket.open():
        log_message("WebSocket is closed in receive_status")
        return

    log_message("WebSocket connected in receive_status")

    last_sent_time = time.time()

    while True:
        try:
            if uart.any() >= FRAME_SIZE:
                raw = uart.read()
                if not raw:
                    await asyncio.sleep(0.1)
                    continue

                for i in range(0, len(raw) - FRAME_SIZE + 1, FRAME_SIZE):
                    frame = raw[i:i + FRAME_SIZE]
                    if frame[0] == 0x55 and frame[1] == 0xAA:
                        now = time.time()
                        if now - last_sent_time >= 10:
                            hb = {
                                "type": "HB",
                                "value": frame.hex()
                            }
                            await websocket.send(json.dumps(hb))
                            log_message(f"Sent to WebSocket: {hb}")
                            last_sent_time = now
                        else:
                            log_message("Skipping HB due to rate limit")
        except Exception as e_inner:
            log_message(f"UART receive loop error: {e_inner}")
            return

        await asyncio.sleep(0.1)



        
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

            websocket = AsyncWebsocketClient()
            uri = f"{config['server']['url']}/{config['chair']['id']}"
            print(uri)
            connected = await websocket.handshake(uri)

            if not connected or not await websocket.open():
                print("Failed to connect WebSocket in run_tasks")
                await asyncio.sleep(5)
                continue

            print("Shared WebSocket connected")

            # Run all tasks and restart if any of them crash
            task1 = asyncio.create_task(track(uart))
            task2 = asyncio.create_task(actions(uart, websocket))
            task3 = asyncio.create_task(receive_status(uart, websocket))

            while True:
                await asyncio.sleep(1)
                for t in [task1, task2, task3]:
                    if t.done():
                        raise Exception("A task exited unexpectedly")
        except KeyboardInterrupt:
            print("Stopping due to user interrupt")
            raise  # propagate to top-level
        except Exception as e:
            print(f"Exception occurred: {e}")

        print("Sleeping before restart...")
        await asyncio.sleep(3)




# Start the event loop
try:
    asyncio.run(run_tasks())
except KeyboardInterrupt:
    print("Program stopped.")



