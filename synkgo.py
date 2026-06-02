#!/usr/bin/env python3

import socket
import threading
import os
import sys
import time
import json
import random
from pathlib import Path

PORT = 12345
FILE_PORT = 12346
BUFFER = 8192

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
        'file_port': FILE_PORT
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

def broadcast_room():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    msg = f"SYNKGO:{room_id}:{my_name}"
    while running:
        sock.sendto(msg.encode(), ('<broadcast>', PORT))
        time.sleep(3)

def discovery_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', PORT))
    while running:
        try:
            data, addr = sock.recvfrom(1024)
            if addr[0] != my_ip:
                msg = data.decode()
                if msg.startswith("SYNKGO:"):
                    _, rid, name = msg.split(':', 2)
                    if rid == room_id:
                        peers[addr[0]] = {'name': name, 'last_seen': time.time()}
        except:
            pass

def send_file(ip, filepath):
    filepath_str = str(filepath)
    filename = os.path.basename(filepath_str)
    if is_blocked(filename):
        print(f"{RED}Blocked: {filename}{RESET}")
        return False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((ip, FILE_PORT))
        filesize = os.path.getsize(filepath_str)
        sock.send(f"{filename}|{filesize}".encode())
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
    except Exception as e:
        print(f"{RED}Send failed: {e}{RESET}")
        return False

def file_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((my_ip, FILE_PORT))
    server.listen(5)
    while running:
        conn, addr = server.accept()
        threading.Thread(target=receive_file, args=(conn, addr[0]), daemon=True).start()

def receive_file(conn, sender_ip):
    try:
        header = conn.recv(1024).decode().strip()
        if not header:
            return
        filename, size = header.split('|')
        size = int(size)
        if is_blocked(filename):
            print(f"{RED}Blocked incoming file: {filename} from {sender_ip}{RESET}")
            conn.close()
            return
        save_name = f"received_{int(time.time())}_{filename}"
        with open(save_name, 'wb') as f:
            received = 0
            while received < size:
                chunk = conn.recv(min(BUFFER, size - received))
                if not chunk:
                    break
                f.write(chunk)
                received += len(chunk)
        print(f"{GREEN}Saved {filename} as {save_name} from {sender_ip}{RESET}")
    except Exception as e:
        print(f"{RED}File receive error: {e}{RESET}")
    finally:
        conn.close()

def chat_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((my_ip, PORT))
    server.listen(5)
    while running:
        conn, addr = server.accept()
        threading.Thread(target=handle_chat, args=(conn, addr[0]), daemon=True).start()

def handle_chat(conn, ip):
    try:
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
        sock.connect((ip, PORT))
        sock.send(message.encode())
        sock.close()
        return True
    except:
        return False

def broadcast_chat(message):
    full_msg = f"{my_name}: {message}"
    for ip in peers:
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
    last_content = None
    while watching_file == str(filepath) and running:
        try:
            with open(filepath, 'r') as f:
                content = f.read()
            if last_content is not None and content != last_content:
                print(f"\n{YELLOW}[CHANGE] {filename} updated at {time.strftime('%H:%M:%S')}{RESET}")
                if autosync:
                    for ip in peers:
                        send_file(ip, str(filepath))
            last_content = content
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
            for filepath in folder_path.rglob('*'):
                if filepath.is_file():
                    if filepath.name == '%synkblock':
                        continue
                    if filepath.name.startswith('.synkgo'):
                        continue
                    if is_blocked(filepath.name):
                        continue
                    key = str(filepath)
                    mtime = os.path.getmtime(filepath)
                    if key in last_file_states and last_file_states[key] != mtime:
                        print(f"{BLUE}Auto-syncing: {filepath.name}{RESET}")
                        for ip in peers:
                            send_file(ip, str(filepath))
                    last_file_states[key] = mtime
        time.sleep(2)

def join_room(room_id_to_join):
    global my_ip, my_name, room_id, room_folder, is_host, running, peers
    
    my_ip = get_local_ip()
    my_name = input(f"{CYAN}Enter your name: {RESET}").strip()
    if not my_name:
        my_name = f"User{random.randint(100,999)}"
    
    room_id = room_id_to_join
    room_folder = os.getcwd()
    is_host = False
    
    synkgo_dir = Path(room_folder) / ".synkgo"
    synkgo_dir.mkdir(exist_ok=True)
    save_config()
    
    print(f"{GREEN}Looking for room {room_id} on LAN...{RESET}")
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(2)
    
    found_host = None
    for attempt in range(10):
        sock.sendto(f"SYNKGO:{room_id}:LOOKUP".encode(), ('<broadcast>', PORT))
        try:
            data, addr = sock.recvfrom(1024)
            decoded = data.decode()
            if decoded.startswith(f"SYNKGO:{room_id}"):
                parts = decoded.split(':')
                if len(parts) >= 3 and parts[2] != "LOOKUP":
                    found_host = addr[0]
                    break
        except socket.timeout:
            continue
    
    sock.close()
    
    if found_host:
        print(f"{GREEN}Found room at {found_host}{RESET}")
        threading.Thread(target=discovery_listener, daemon=True).start()
        threading.Thread(target=chat_server, daemon=True).start()
        threading.Thread(target=file_server, daemon=True).start()
        threading.Thread(target=monitor_folder_changes, daemon=True).start()
        peers[found_host] = {'name': 'Host', 'last_seen': time.time()}
        interactive_terminal()
    else:
        print(f"{RED}Room {room_id} not found on LAN{RESET}")
        print(f"{YELLOW}Make sure the host is running: python synkgo.py -host .{RESET}")
        running = False

def interactive_terminal():
    global autosync, blocked_patterns, running
    
    clear_screen()
    
    print(f"{CYAN}{BOLD}")
    print("  ███████╗██╗   ██╗███╗   ██╗██╗  ██╗ ██████╗  ██████╗ ")
    print("  ██╔════╝╚██╗ ██╔╝████╗  ██║██║ ██╔╝██╔════╝ ██╔═══██╗")
    print("  ███████╗ ╚████╔╝ ██╔██╗ ██║█████╔╝ ██║  ███╗██║   ██║")
    print("  ╚════██║  ╚██╔╝  ██║╚██╗██║██╔═██╗ ██║   ██║██║   ██║")
    print("  ███████║   ██║   ██║ ╚████║██║  ██╗╚██████╔╝╚██████╔╝")
    print("  ╚══════╝   ╚═╝   ╚═╝  ╚═══╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ")
    print(f"{RESET}")
    print(f"{BOLD}Technology: Asynkicgo | No accounts | No limits | No spying{RESET}")
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
    print("  /block <pattern> - Block files")
    print("  /unblock <pat>   - Remove block")
    print("  /blocklist       - Show blocked patterns")
    print("  /watch <file>    - Watch file for changes")
    print("  /watch off       - Stop watching")
    print("  /autosync on/off - Auto-send changed files")
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
                        print(f"  {ip} - {info['name']}")
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
            
            elif user_input.startswith("/block "):
                pattern = user_input[7:]
                if pattern not in blocked_patterns:
                    blocked_patterns.append(pattern)
                    if room_folder:
                        blockfile = Path(room_folder) / "%synkblock"
                        with open(blockfile, 'a') as f:
                            f.write(f"\n{pattern}")
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
                    print(f"{GREEN}Auto-sync ON{RESET}")
                elif arg == "off":
                    autosync = False
                    print(f"{GREEN}Auto-sync OFF{RESET}")
                else:
                    print(f"Auto-sync: {'ON' if autosync else 'OFF'}")
            
            elif user_input == "/exit":
                print(f"{YELLOW}Exiting Synkgo...{RESET}")
                running = False
                break
            
            else:
                print(f"{RED}Unknown command{RESET}")
        
        except KeyboardInterrupt:
            print(f"\n{YELLOW}Exiting...{RESET}")
            break
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}")

def host_mode(folder):
    global my_ip, my_name, room_id, room_folder, is_host, running
    
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
    
    synkgo_dir = Path(room_folder) / ".synkgo"
    synkgo_dir.mkdir(exist_ok=True)
    
    blockfile = Path(room_folder) / "%synkblock"
    if not blockfile.exists():
        with open(blockfile, 'w') as f:
            f.write("# Synkgo blocklist\n# *.exe\n# *.bat\n")
    
    load_blocklist()
    save_config()
    
    print(f"{GREEN}Room created!{RESET}")
    print(f"  Room ID: {BOLD}{room_id}{RESET}")
    print(f"  Sharing: {room_folder}")
    print(f"  Your IP: {my_ip}")
    print()
    print(f"{YELLOW}On your brother's computer run:{RESET}")
    print(f"  python synkgo.py -join {room_id}")
    print()
    
    threading.Thread(target=broadcast_room, daemon=True).start()
    threading.Thread(target=discovery_listener, daemon=True).start()
    threading.Thread(target=chat_server, daemon=True).start()
    threading.Thread(target=file_server, daemon=True).start()
    threading.Thread(target=monitor_folder_changes, daemon=True).start()
    
    interactive_terminal()
    
    running = False
    print(f"{YELLOW}Room closed{RESET}")

def list_mode():
    clear_screen()
    print(f"{BOLD}Scanning for Synkgo rooms on LAN...{RESET}")
    print()
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', PORT))
    sock.settimeout(2)
    
    found_rooms = {}
    start_time = time.time()
    
    while time.time() - start_time < 5:
        try:
            data, addr = sock.recvfrom(1024)
            decoded = data.decode()
            if decoded.startswith("SYNKGO:"):
                parts = decoded.split(':')
                if len(parts) >= 3 and parts[2] != "LOOKUP":
                    rid = parts[1]
                    name = parts[2]
                    if rid not in found_rooms:
                        found_rooms[rid] = {'name': name, 'ip': addr[0]}
                        print(f"  {GREEN}Room {rid}{RESET} - {name} at {addr[0]}")
        except socket.timeout:
            pass
    
    sock.close()
    
    if not found_rooms:
        print(f"{RED}  No Synkgo rooms found{RESET}")
        print()
        print("Make sure someone is hosting:")
        print(f"  {GREEN}python synkgo.py -host .{RESET}")
    
    print()

def interactive_outside():
    clear_screen()
    print(f"{RED}{BOLD}ERROR: You are not in a room{RESET}")
    print()
    print("First, either:")
    print(f"  {GREEN}python synkgo.py -host .{RESET}    (to create a room)")
    print(f"  {GREEN}python synkgo.py -join 1234{RESET} (to join a room)")
    print()

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Synkgo - LAN file sharing & chat')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-host', metavar='folder', help='Host a room sharing FOLDER')
    group.add_argument('-join', metavar='room_id', help='Join a room by ID (4 digits)')
    group.add_argument('-list', action='store_true', help='List rooms on LAN')
    group.add_argument('-int', action='store_true', help='Interactive terminal (must be in a room)')
    
    args = parser.parse_args()
    
    in_room = False
    global room_folder
    if Path(".synkgo").exists():
        in_room = True
        room_folder = os.getcwd()
        config = load_config()
        if config:
            global room_id, my_name
            room_id = config.get('room_id')
            my_name = config.get('my_name')
    
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