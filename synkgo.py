#!/usr/bin/env python3

import socket
import threading
import os
import sys
import time
import json
import random
import hashlib
import signal
from pathlib import Path

PORT = 12345
FILE_PORT = 12346
BUFFER = 8192
TIMEOUT = 10
MAX_FILE_SIZE = 100 * 1024 * 1024
HEARTBEAT_INTERVAL = 5
PEER_TIMEOUT = 15

SESSIONS = {}

my_ip = None
my_name = None
room_id = None
room_folder = None
is_host = False
running = True
peers = {}
blocked_patterns = []
autosync = False
watching_file = None
last_file_states = {}
sync_queue = []
sync_lock = threading.Lock()

GREEN = '\033[92m'
BLUE = '\033[94m'
YELLOW = '\033[93m'
RED = '\033[91m'
CYAN = '\033[96m'
RESET = '\033[0m'
BOLD = '\033[1m'

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 1))
        ip = s.getsockname()[0]
    except:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

def get_file_hash(filepath):
    hasher = hashlib.md5()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            hasher.update(chunk)
    return hasher.hexdigest()

def load_blocklist():
    global blocked_patterns
    if room_folder is None:
        blocked_patterns = []
        return
    blockfile = Path(room_folder) / "%synkblock"
    if blockfile.exists():
        with open(blockfile, 'r') as f:
            patterns = []
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    patterns.append(line)
            blocked_patterns = patterns
    else:
        blocked_patterns = []

def is_blocked(filename):
    for pattern in blocked_patterns:
        if pattern.endswith('*'):
            if filename.startswith(pattern[:-1]):
                return True
        elif pattern.startswith('*'):
            if filename.endswith(pattern[1:]):
                return True
        elif pattern in filename:
            return True
    return False

def save_config():
    if room_folder is None:
        return
    config_dir = Path(room_folder) / ".synkgo"
    config_dir.mkdir(exist_ok=True)
    config = {
        'room_id': room_id,
        'my_name': my_name,
        'port': PORT,
        'file_port': FILE_PORT,
        'autosync': autosync
    }
    with open(config_dir / 'config.json', 'w') as f:
        json.dump(config, f)

def load_config():
    if room_folder is None:
        return None
    config_file = Path(room_folder) / ".synkgo" / "config.json"
    if config_file.exists():
        with open(config_file, 'r') as f:
            return json.load(f)
    return None

def generate_room_id():
    return str(random.randint(1000, 9999))

def heartbeat_sender():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    while running and room_id:
        try:
            msg = f"SYNKGO:HEARTBEAT:{room_id}:{my_name}"
            sock.sendto(msg.encode(), ('<broadcast>', PORT))
        except:
            pass
        time.sleep(HEARTBEAT_INTERVAL)
    sock.close()

def peer_cleaner():
    while running:
        time.sleep(2)
        now = time.time()
        to_remove = []
        for ip, info in peers.items():
            if now - info.get('last_seen', 0) > PEER_TIMEOUT:
                to_remove.append(ip)
        for ip in to_remove:
            print(f"\n{RED}Peer {peers[ip].get('name', ip)} disconnected{RESET}")
            del peers[ip]
            print(f"{GREEN}> {RESET}", end='', flush=True)

def discovery_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(('', PORT))
    sock.settimeout(1)
    while running:
        try:
            data, addr = sock.recvfrom(1024)
            if addr[0] != my_ip:
                msg = data.decode()
                if msg.startswith("SYNKGO:"):
                    parts = msg.split(':')
                    if len(parts) >= 4:
                        cmd = parts[1]
                        rid = parts[2]
                        name = parts[3]
                        if cmd == "HEARTBEAT" or cmd == "LOOKUP":
                            if rid == room_id:
                                peers[addr[0]] = {'name': name, 'last_seen': time.time()}
                            SESSIONS[rid] = {
                                'hostname': name,
                                'ip': addr[0],
                                'last_seen': time.time()
                            }
        except socket.timeout:
            pass
        except:
            pass

def start_background_discovery():
    t = threading.Thread(target=discovery_loop, daemon=True)
    t.start()
    h = threading.Thread(target=heartbeat_sender, daemon=True)
    h.start()
    c = threading.Thread(target=peer_cleaner, daemon=True)
    c.start()

def broadcast_room():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    while running:
        try:
            msg = f"SYNKGO:ANNOUNCE:{room_id}:{my_name}"
            sock.sendto(msg.encode(), ('<broadcast>', PORT))
        except:
            pass
        time.sleep(3)
    sock.close()

def send_file(ip, filepath):
    filepath_str = str(filepath)
    filename = os.path.basename(filepath_str)
    if is_blocked(filename):
        print(f"{RED}Blocked: {filename}{RESET}")
        return False
    if not os.path.exists(filepath_str):
        print(f"{RED}File not found: {filename}{RESET}")
        return False
    filesize = os.path.getsize(filepath_str)
    if filesize > MAX_FILE_SIZE:
        print(f"{RED}File too large: {filename} ({filesize} bytes max {MAX_FILE_SIZE}){RESET}")
        return False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        sock.connect((ip, FILE_PORT))
        filehash = get_file_hash(filepath_str)
        sock.send(f"{filename}|{filesize}|{filehash}".encode())
        time.sleep(0.1)
        with open(filepath_str, 'rb') as f:
            sent = 0
            while sent < filesize:
                chunk = f.read(BUFFER)
                if not chunk:
                    break
                sock.send(chunk)
                sent += len(chunk)
        sock.close()
        print(f"{GREEN}Sent {filename} to {ip}{RESET}")
        return True
    except socket.timeout:
        print(f"{RED}Timeout sending to {ip}{RESET}")
        return False
    except Exception as e:
        print(f"{RED}Send failed: {e}{RESET}")
        return False

def file_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', FILE_PORT))
    server.listen(10)
    server.settimeout(1)
    while running:
        try:
            conn, addr = server.accept()
            threading.Thread(target=receive_file, args=(conn, addr[0]), daemon=True).start()
        except socket.timeout:
            continue
        except:
            pass
    server.close()

def receive_file(conn, sender_ip):
    try:
        conn.settimeout(TIMEOUT)
        header = conn.recv(1024).decode().strip()
        if not header:
            conn.close()
            return
        
        parts = header.split('|')
        if len(parts) == 3:
            raw_filename, raw_size, sent_hash = parts
        else:
            raw_filename, raw_size = parts
            sent_hash = None
        
        filename = os.path.basename(raw_filename.replace('\\', '/'))
        
        try:
            size = int(raw_size)
        except ValueError:
            conn.close()
            return

        if size > MAX_FILE_SIZE:
            print(f"{RED}Rejected oversized file: {filename} ({size} bytes){RESET}")
            conn.close()
            return

        if is_blocked(filename):
            print(f"{RED}Blocked incoming file: {filename} from {sender_ip}{RESET}")
            conn.close()
            return
        
        timestamp = int(time.time())
        save_name = f"received_{timestamp}_{filename}"
        target_path = Path(room_folder) / save_name if room_folder else Path(save_name)
        
        hasher = hashlib.md5()
        with open(target_path, 'wb') as f:
            received = 0
            while received < size:
                chunk = conn.recv(min(BUFFER, size - received))
                if not chunk:
                    break
                f.write(chunk)
                hasher.update(chunk)
                received += len(chunk)
        
        if sent_hash and hasher.hexdigest() != sent_hash:
            print(f"{RED}File corrupted: {filename} (hash mismatch){RESET}")
            target_path.unlink()
        else:
            print(f"{GREEN}Saved {filename} as {save_name} from {sender_ip}{RESET}")
    except socket.timeout:
        print(f"{RED}Timeout receiving file from {sender_ip}{RESET}")
    except Exception as e:
        print(f"{RED}File receive error: {e}{RESET}")
    finally:
        conn.close()

def chat_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', PORT))
    server.listen(10)
    server.settimeout(1)
    while running:
        try:
            conn, addr = server.accept()
            threading.Thread(target=handle_chat, args=(conn, addr[0]), daemon=True).start()
        except socket.timeout:
            continue
        except:
            pass
    server.close()

def handle_chat(conn, ip):
    try:
        conn.settimeout(TIMEOUT)
        data = conn.recv(4096).decode()
        if data:
            peer_name = peers.get(ip, {}).get('name', ip)
            print(f"\n{CYAN}[{peer_name}]{RESET} {data}")
            print(f"{GREEN}> {RESET}", end='', flush=True)
    except:
        pass
    finally:
        conn.close()

def send_chat(ip, message):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        sock.connect((ip, PORT))
        sock.send(message.encode())
        sock.close()
        return True
    except:
        return False

def broadcast_chat(message):
    full_msg = f"{my_name}: {message}"
    for ip in list(peers.keys()):
        send_chat(ip, full_msg)
    print(f"{YELLOW}[You]{RESET} {message}")

def watch_file(filename):
    global watching_file
    if room_folder is None:
        print("No room folder")
        return
    filepath = Path(room_folder) / filename
    if not filepath.exists():
        print(f"{RED}File not found: {filename}{RESET}")
        return
    watching_file = str(filepath)
    print(f"{BLUE}Watching {filename} for changes...{RESET}")
    last_hash = None
    while watching_file == str(filepath) and running:
        try:
            if not filepath.exists():
                print(f"\n{RED}File deleted: {filename}{RESET}")
                break
            current_hash = get_file_hash(filepath)
            if last_hash is not None and current_hash != last_hash:
                print(f"\n{YELLOW}[CHANGE] {filename} updated at {time.strftime('%H:%M:%S')}{RESET}")
                if autosync:
                    for ip in peers:
                        send_file(ip, str(filepath))
            last_hash = current_hash
        except:
            pass
        time.sleep(1)
    watching_file = None

def stop_watch():
    global watching_file
    watching_file = None
    print(f"{BLUE}Stopped watching{RESET}")

def monitor_folder_changes():
    global last_file_states
    while running:
        if autosync and room_folder:
            folder_path = Path(room_folder)
            for filepath in folder_path.iterdir():
                if filepath.is_file():
                    if filepath.name == '%synkblock':
                        continue
                    if filepath.name.startswith('.synkgo'):
                        continue
                    if filepath.name.startswith('received_'):
                        continue
                    if is_blocked(filepath.name):
                        continue
                    key = str(filepath)
                    current_hash = get_file_hash(filepath)
                    last_hash = last_file_states.get(key)
                    if last_hash and last_hash != current_hash:
                        print(f"{BLUE}Auto-syncing: {filepath.name}{RESET}")
                        for ip in peers:
                            send_file(ip, str(filepath))
                    last_file_states[key] = current_hash
        time.sleep(3)

def request_missing_file(filename, from_ip):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        sock.connect((from_ip, PORT + 10))
        sock.send(f"REQUEST:{filename}".encode())
        sock.close()
    except:
        pass

def sync_all_files():
    if not room_folder:
        print("No room folder")
        return
    folder_path = Path(room_folder)
    files = []
    for filepath in folder_path.iterdir():
        if filepath.is_file():
            if filepath.name == '%synkblock':
                continue
            if filepath.name.startswith('.synkgo'):
                continue
            if filepath.name.startswith('received_'):
                continue
            if not is_blocked(filepath.name):
                files.append(filepath)
    
    if not files:
        print("No files to sync")
        return
    
    print(f"Syncing {len(files)} files to {len(peers)} peers...")
    for filepath in files:
        for ip in peers:
            send_file(ip, str(filepath))

def join_room(session_id):
    global my_ip, my_name, room_id, room_folder, is_host, running, peers, SESSIONS
    
    my_ip = get_local_ip()
    my_name = input(f"{CYAN}Enter your name: {RESET}").strip()
    if not my_name:
        my_name = f"User{random.randint(100,999)}"
    
    room_id = session_id
    room_folder = os.getcwd()
    is_host = False
    
    synkgo_dir = Path(room_folder) / ".synkgo"
    synkgo_dir.mkdir(exist_ok=True)
    save_config()
    
    print(f"{GREEN}Looking for room {session_id} on LAN...{RESET}")
    
    start_background_discovery()
    
    found_ip = None
    for attempt in range(10):
        if session_id in SESSIONS:
            found_ip = SESSIONS[session_id]['ip']
            break
        time.sleep(1)
    
    if found_ip:
        print(f"{GREEN}Found room at {found_ip}{RESET}")
        threading.Thread(target=chat_server, daemon=True).start()
        threading.Thread(target=file_server, daemon=True).start()
        threading.Thread(target=monitor_folder_changes, daemon=True).start()
        peers[found_ip] = {'name': SESSIONS[session_id]['hostname'], 'last_seen': time.time()}
        interactive_terminal()
    else:
        print(f"{RED}Room {session_id} not found on LAN{RESET}")
        print(f"{YELLOW}Make sure the host is running: python synkgo.py -host .{RESET}")
        running = False

def interactive_terminal():
    global autosync, blocked_patterns, running, my_name
    
    clear_screen()
    
    print(f"{CYAN}{BOLD}")
    print("  ███████╗██╗   ██╗███╗   ██╗██╗  ██╗ ██████╗  ██████╗ ")
    print("  ██╔════╝╚██╗ ██╔╝████╗  ██║██║ ██╔╝██╔════╝ ██╔═══██╗")
    print("  ███████╗ ╚████╔╝ ██╔██╗ ██║█████╔╝ ██║  ███╗██║   ██║")
    print("  ╚════██║  ╚██╔╝  ██║╚██╗██║██╔═██╗ ██║   ██║██║   ██║")
    print("  ███████║   ██║   ██║ ╚████║██║  ██╗╚██████╔╝╚██████╔╝")
    print("  ╚══════╝   ╚═╝   ╚═╝  ╚═══╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ")
    print(f"{RESET}")
    print(f"{BOLD}Synkgo v2.0 | LAN Sync & Chat{RESET}")
    print()
    print(f"  Room ID: {BOLD}{room_id}{RESET}")
    print(f"  Role:    {BOLD}{'HOST' if is_host else 'PEER'}{RESET}")
    print(f"  Folder:  {BOLD}{room_folder}{RESET}")
    print(f"  Peers:   {BOLD}{len(peers)}{RESET}")
    print()
    print(f"{YELLOW}Commands:{RESET}")
    print("  /peers           - Show connected peers")
    print("  /ls              - List shared files")
    print("  /chat <msg>      - Send message to all")
    print("  /send <file>     - Send a file")
    print("  /sync            - Sync all files to all peers")
    print("  /block <pattern> - Block files")
    print("  /unblock <pat>   - Remove block")
    print("  /blocklist       - Show blocked patterns")
    print("  /watch <file>    - Watch file for changes")
    print("  /watch off       - Stop watching")
    print("  /autosync on/off - Auto-sync changed files")
    print("  /clear           - Clear screen")
    print("  /exit            - Leave room")
    print()
    
    while running:
        try:
            user_input = input(f"{GREEN}> {RESET}").strip()
            if not user_input:
                continue
            
            if user_input == "/peers":
                if peers:
                    for ip, info in peers.items():
                        print(f"  {ip} - {info['name']} (last seen {int(time.time() - info.get('last_seen', 0))}s ago)")
                else:
                    print("  No peers connected")
            
            elif user_input == "/ls":
                if room_folder:
                    folder = Path(room_folder)
                    found = False
                    for f in folder.iterdir():
                        if f.is_file():
                            if f.name == '%synkblock':
                                continue
                            if f.name.startswith('.synkgo'):
                                continue
                            if f.name.startswith('received_'):
                                continue
                            size = os.path.getsize(f)
                            print(f"  {f.name} ({size} bytes)")
                            found = True
                    if not found:
                        print("  No files in shared folder")
            
            elif user_input.startswith("/chat "):
                msg = user_input[6:]
                broadcast_chat(msg)
            
            elif user_input.startswith("/send "):
                filename = user_input[6:]
                if room_folder:
                    filepath = Path(room_folder) / filename
                    if filepath.exists() and not is_blocked(filename):
                        for ip in peers:
                            send_file(ip, str(filepath))
                    else:
                        print(f"{RED}File not found or blocked{RESET}")
            
            elif user_input == "/sync":
                sync_all_files()
            
            elif user_input.startswith("/block "):
                pattern = user_input[7:]
                if pattern not in blocked_patterns:
                    blocked_patterns.append(pattern)
                    if room_folder:
                        blockfile = Path(room_folder) / "%synkblock"
                        with open(blockfile, 'a') as f:
                            f.write(f"{pattern}\n")
                    print(f"{GREEN}Blocked: {pattern}{RESET}")
                else:
                    print(f"{YELLOW}Already blocked{RESET}")
            
            elif user_input.startswith("/unblock "):
                pattern = user_input[9:]
                if pattern in blocked_patterns:
                    blocked_patterns.remove(pattern)
                    print(f"{GREEN}Unblocked: {pattern}{RESET}")
                else:
                    print(f"{YELLOW}Pattern not found{RESET}")
            
            elif user_input == "/blocklist":
                if blocked_patterns:
                    for p in blocked_patterns:
                        print(f"  {p}")
                else:
                    print("  No blocked patterns")
            
            elif user_input.startswith("/watch "):
                fname = user_input[7:]
                if fname == "off":
                    stop_watch()
                else:
                    threading.Thread(target=watch_file, args=(fname,), daemon=True).start()
            
            elif user_input.startswith("/autosync "):
                arg = user_input[10:]
                if arg == "on":
                    autosync = True
                    save_config()
                    print(f"{GREEN}Auto-sync ON{RESET}")
                elif arg == "off":
                    autosync = False
                    save_config()
                    print(f"{GREEN}Auto-sync OFF{RESET}")
                else:
                    print(f"Auto-sync: {'ON' if autosync else 'OFF'}")
            
            elif user_input == "/clear":
                clear_screen()
            
            elif user_input == "/exit":
                print(f"{YELLOW}Exiting Synkgo...{RESET}")
                running = False
                break
            
            else:
                print(f"{RED}Unknown command. Type /help{RESET}")
        
        except KeyboardInterrupt:
            print(f"\n{YELLOW}Exiting...{RESET}")
            break
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}")

def host_mode(folder):
    global my_ip, my_name, room_id, room_folder, is_host, running, autosync
    
    clear_screen()
    
    print(f"{CYAN}{BOLD}")
    print("  ███████╗██╗   ██╗███╗   ██╗██╗  ██╗ ██████╗  ██████╗ ")
    print("  ██╔════╝╚██╗ ██╔╝████╗  ██║██║ ██╔╝██╔════╝ ██╔═══██╗")
    print("  ███████╗ ╚████╔╝ ██╔██╗ ██║█████╔╝ ██║  ███╗██║   ██║")
    print("  ╚════██║  ╚██╔╝  ██║╚██╗██║██╔═██╗ ██║   ██║██║   ██║")
    print("  ███████║   ██║   ██║ ╚████║██║  ██╗╚██████╔╝╚██████╔╝")
    print("  ╚══════╝   ╚═╝   ╚═╝  ╚═══╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ")
    print(f"{RESET}")
    print(f"{BOLD}>>> HOST MODE <<<{RESET}")
    print()
    
    my_ip = get_local_ip()
    my_name = input(f"{CYAN}Enter your name: {RESET}").strip()
    if not my_name:
        my_name = f"Host{random.randint(100,999)}"
    
    room_id = generate_room_id()
    room_folder = os.path.abspath(folder)
    is_host = True
    autosync = False
    
    Path(room_folder).mkdir(parents=True, exist_ok=True)
    
    synkgo_dir = Path(room_folder) / ".synkgo"
    synkgo_dir.mkdir(exist_ok=True)
    
    blockfile = Path(room_folder) / "%synkblock"
    if not blockfile.exists():
        with open(blockfile, 'w') as f:
            f.write("# Blocked patterns - one per line\n")
            f.write("# Use * at start or end for wildcards\n")
            f.write("*.exe\n")
            f.write("*.bat\n")
            f.write("*.dll\n")
    
    load_blocklist()
    save_config()
    
    print(f"{GREEN}Room created!{RESET}")
    print(f"  Room ID: {BOLD}{room_id}{RESET}")
    print(f"  Sharing: {room_folder}")
    print(f"  Your IP: {my_ip}")
    print()
    print(f"{YELLOW}On another computer run:{RESET}")
    print(f"  python synkgo.py -join {room_id}")
    print()
    print(f"{BLUE}Waiting for peers to join...{RESET}")
    print()
    
    start_background_discovery()
    threading.Thread(target=broadcast_room, daemon=True).start()
    threading.Thread(target=chat_server, daemon=True).start()
    threading.Thread(target=file_server, daemon=True).start()
    threading.Thread(target=monitor_folder_changes, daemon=True).start()
    
    interactive_terminal()
    
    running = False
    print(f"{YELLOW}Room closed{RESET}")

def list_mode():
    global SESSIONS
    
    clear_screen()
    print(f"{BOLD}Scanning for Synkgo rooms on LAN...{RESET}")
    print()
    
    SESSIONS = {}
    
    temp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    temp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    temp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    temp_sock.bind(('', PORT))
    temp_sock.settimeout(2)
    
    for i in range(6):
        temp_sock.sendto(b"SYNKGO:LOOKUP:LOOKUP:LOOKUP", ('<broadcast>', PORT))
        try:
            data, addr = temp_sock.recvfrom(1024)
            msg = data.decode()
            if msg.startswith("SYNKGO:"):
                parts = msg.split(':')
                if len(parts) >= 4:
                    cmd = parts[1]
                    rid = parts[2]
                    name = parts[3]
                    if cmd == "ANNOUNCE" and rid not in SESSIONS:
                        SESSIONS[rid] = {'hostname': name, 'ip': addr[0]}
                        print(f"  {GREEN}Room {rid}{RESET} - {name} at {addr[0]}")
        except socket.timeout:
            pass
        time.sleep(0.3)
    
    temp_sock.close()
    
    if not SESSIONS:
        print(f"{RED}  No Synkgo rooms found{RESET}")
        print()
        print("Make sure someone is hosting:")
        print(f"  {GREEN}python synkgo.py -host .{RESET}")
    else:
        print()
        print(f"{YELLOW}To join a room: python synkgo.py -join <room_id>{RESET}")
    
    print()

def interactive_outside():
    clear_screen()
    print(f"{RED}{BOLD}ERROR: You are not in a room{RESET}")
    print()
    print("First, either:")
    print(f"  {GREEN}python synkgo.py -host .{RESET}    (to create a room)")
    print(f"  {GREEN}python synkgo.py -join 1234{RESET} (to join a room)")
    print()

def signal_handler(sig, frame):
    global running
    print(f"\n{YELLOW}Shutting down...{RESET}")
    running = False
    sys.exit(0)

def main():
    signal.signal(signal.SIGINT, signal_handler)
    
    import argparse
    
    parser = argparse.ArgumentParser(description='Synkgo v2.0 - LAN file sharing & chat')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-host', metavar='folder', help='Host a room sharing FOLDER')
    group.add_argument('-join', metavar='room_id', help='Join a room by ID (4 digits)')
    group.add_argument('-list', action='store_true', help='List rooms on LAN')
    group.add_argument('-int', action='store_true', help='Interactive terminal (must be in a room)')
    
    args = parser.parse_args()
    
    in_room = False
    global room_folder, room_id, my_name, autosync
    if Path(".synkgo").exists():
        in_room = True
        room_folder = os.getcwd()
        config = load_config()
        if config:
            room_id = config.get('room_id')
            my_name = config.get('my_name')
            autosync = config.get('autosync', False)
    
    if args.host:
        host_mode(args.host)
    elif args.join:
        join_room(args.join)
    elif args.list:
        list_mode()
    elif args.int:
        if in_room:
            interactive_terminal()
        else:
            interactive_outside()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()