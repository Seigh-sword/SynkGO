#!/usr/bin/env python3
"""
SYNKGO - LAN-first, account-free, uncensorable file sharing & chat
Technology: Asynkicgo

Created by: You (age 11!)
This is a complete peer-to-peer tool that needs no internet, no accounts,
and no central servers. Works over LAN/WiFi between computers.
"""

import socket
import threading
import os
import sys
import time
import json
import random
import shutil
from pathlib import Path

# ===== CONFIGURATION =====
PORT = 12345          # Port for chat and discovery
FILE_PORT = 12346     # Separate port for file transfers
BUFFER = 8192         # 8KB chunks for streaming files (no 4GB limit)

# ===== GLOBAL STATE =====
my_ip = None          # This computer's IP address
my_name = None        # User's chosen name
room_id = None        # Numeric room ID (like 6767)
room_folder = None    # The folder being shared
is_host = False       # Whether this computer created the room
running = True        # Whether the program is still running
peers = {}            # Dictionary of other computers in the room
blocked_patterns = [] # List of file patterns to block (from %synkblock)
autosync = False      # Whether to auto-send changed files
watching_file = None  # File being watched for changes
last_file_states = {} # For tracking file changes

# ===== COLORS FOR TERMINAL =====
# These make the command line look cool
GREEN = '\033[92m'
BLUE = '\033[94m'
YELLOW = '\033[93m'
RED = '\033[91m'
CYAN = '\033[96m'
RESET = '\033[0m'
BOLD = '\033[1m'

# ===== UTILITY FUNCTIONS =====

def clear_screen():
    """Clear the terminal screen - works on Windows, Mac, Linux"""
    os.system('cls' if os.name == 'nt' else 'clear')

def get_local_ip():
    """Get this computer's local IP address on the LAN"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connect to a fake IP to get the real interface IP
        s.connect(('8.8.8.8', 1))
        ip = s.getsockname()[0]
    except:
        ip = '127.0.0.1'  # Fallback to localhost
    finally:
        s.close()
    return ip

def load_blocklist():
    """Load the %synkblock file which contains blocked file patterns"""
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
                # Skip empty lines and comments
                if line and not line.startswith('#'):
                    patterns.append(line)
            blocked_patterns = patterns
    else:
        blocked_patterns = []

def is_blocked(filename):
    """Check if a filename matches any blocked pattern"""
    for pattern in blocked_patterns:
        # Handle wildcard at end (like *.exe)
        if pattern.endswith('*'):
            if filename.startswith(pattern[:-1]):
                return True
        # Handle wildcard at start
        elif pattern.startswith('*'):
            if filename.endswith(pattern[1:]):
                return True
        # Direct match or substring
        elif pattern in filename:
            return True
    return False

def save_config():
    """Save room configuration to .synkgo folder"""
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
    """Load room configuration from .synkgo folder"""
    if room_folder is None:
        return None
    config_file = Path(room_folder) / ".synkgo" / "config.json"
    if config_file.exists():
        with open(config_file, 'r') as f:
            return json.load(f)
    return None

def generate_room_id():
    """Generate a 4-digit numeric room ID (like 6767)"""
    return str(random.randint(1000, 9999))

# ===== DISCOVERY (Finding other computers) =====

def broadcast_room():
    """Send UDP broadcasts to announce this room exists"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # Message format: SYNKGO:room_id:name
    msg = f"SYNKGO:{room_id}:{my_name}"
    while running:
        sock.sendto(msg.encode(), ('<broadcast>', PORT))
        time.sleep(5)  # Announce every 5 seconds

def discovery_listener():
    """Listen for other rooms broadcasting on the LAN"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', PORT))
    while running:
        try:
            data, addr = sock.recvfrom(1024)
            if addr[0] != my_ip:  # Don't add ourselves
                msg = data.decode()
                if msg.startswith("SYNKGO:"):
                    _, rid, name = msg.split(':', 2)
                    if rid == room_id:  # Only care about our room
                        peers[addr[0]] = {'name': name, 'last_seen': time.time()}
        except:
            pass

# ===== FILE TRANSFER =====

def send_file(ip, filepath):
    """Send a file to a specific IP address"""
    # Convert to string safely
    filepath_str = str(filepath)
    filename = os.path.basename(filepath_str)
    
    # Check if file is blocked
    if is_blocked(filename):
        print(f"{RED}Blocked: {filename}{RESET}")
        return False
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((ip, FILE_PORT))
        filesize = os.path.getsize(filepath_str)
        
        # Send header with filename and size
        sock.send(f"{filename}|{filesize}".encode())
        time.sleep(0.1)  # Small delay to ensure header arrives
        
        # Send file in chunks (no size limit!)
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
    """Background server that receives files from peers"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((my_ip, FILE_PORT))
    server.listen(5)
    while running:
        conn, addr = server.accept()
        threading.Thread(target=receive_file, args=(conn, addr[0]), daemon=True).start()

def receive_file(conn, sender_ip):
    """Receive a file sent by a peer"""
    try:
        # Receive the header (filename|size)
        header = conn.recv(1024).decode().strip()
        if not header:
            return
        
        filename, size = header.split('|')
        size = int(size)
        
        # Block if needed
        if is_blocked(filename):
            print(f"{RED}Blocked incoming file: {filename} from {sender_ip}{RESET}")
            conn.close()
            return
        
        # Save with timestamp to avoid overwriting
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

# ===== CHAT SYSTEM =====

def chat_server():
    """Background server that receives chat messages"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((my_ip, PORT))
    server.listen(5)
    while running:
        conn, addr = server.accept()
        threading.Thread(target=handle_chat, args=(conn, addr[0]), daemon=True).start()

def handle_chat(conn, ip):
    """Handle an incoming chat message"""
    try:
        data = conn.recv(4096).decode()
        if data:
            peer_name = peers.get(ip, {}).get('name', ip)
            print(f"\n{CYAN}[{peer_name}]{RESET} {data}")
            # Print the prompt again
            print(f"{GREEN}> {RESET}", end='', flush=True)
    except:
        pass
    finally:
        conn.close()

def send_chat(ip, message):
    """Send a chat message to a specific peer"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((ip, PORT))
        sock.send(message.encode())
        sock.close()
        return True
    except:
        return False

def broadcast_chat(message):
    """Send a chat message to all peers in the room"""
    full_msg = f"{my_name}: {message}"
    for ip in peers:
        send_chat(ip, full_msg)
    print(f"{YELLOW}[You]{RESET} {message}")

# ===== REAL-TIME FILE WATCHING =====

def watch_file(filename):
    """Watch a file for changes and display them live"""
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
                # Auto-sync if enabled
                if autosync:
                    for ip in peers:
                        send_file(ip, str(filepath))
            
            last_content = content
        except:
            pass
        time.sleep(1)
    
    watching_file = None

def stop_watch():
    """Stop watching the current file"""
    global watching_file
    watching_file = None
    print(f"{BLUE}Stopped watching{RESET}")

def monitor_folder_changes():
    """Background thread that watches for file changes and auto-syncs"""
    global last_file_states
    while running:
        if autosync and room_folder:
            folder_path = Path(room_folder)
            for filepath in folder_path.rglob('*'):
                if filepath.is_file():
                    # Skip special files
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

# ===== JOINING A ROOM =====

def join_room(room_id_to_join):
    """Join an existing room on the LAN"""
    global my_ip, my_name, room_id, room_folder, is_host, running
    
    my_ip = get_local_ip()
    my_name = input(f"{CYAN}Enter your name: {RESET}").strip()
    if not my_name:
        my_name = f"User{random.randint(100,999)}"
    
    room_id = room_id_to_join
    room_folder = os.getcwd()  # Use current folder
    is_host = False
    
    # Create .synkgo folder
    synkgo_dir = Path(room_folder) / ".synkgo"
    synkgo_dir.mkdir(exist_ok=True)
    save_config()
    
    print(f"{GREEN}Looking for room {room_id} on LAN...{RESET}")
    
    # Scan for the host
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(3)
    
    found_host = None
    for _ in range(5):  # Try 5 times
        sock.sendto(f"LOOKUP:{room_id}".encode(), ('<broadcast>', PORT))
        try:
            data, addr = sock.recvfrom(1024)
            if data.decode().startswith(f"SYNKGO:{room_id}"):
                found_host = addr[0]
                break
        except socket.timeout:
            continue
    
    sock.close()
    
    if found_host:
        print(f"{GREEN}Found room at {found_host}{RESET}")
        # Start background threads for this peer
        threading.Thread(target=discovery_listener, daemon=True).start()
        threading.Thread(target=chat_server, daemon=True).start()
        threading.Thread(target=file_server, daemon=True).start()
        threading.Thread(target=monitor_folder_changes, daemon=True).start()
        
        # Add the host as a peer
        peers[found_host] = {'name': 'Host', 'last_seen': time.time()}
        
        # Start interactive terminal
        interactive_terminal()
    else:
        print(f"{RED}Room {room_id} not found on LAN{RESET}")
        running = False

# ===== INTERACTIVE TERMINAL =====

def interactive_terminal():
    """The main command-line interface for users"""
    global autosync, blocked_patterns, running
    
    clear_screen()
    
    # ASCII ART
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
    print("  /block <pattern> - Block files (e.g., /block *.exe)")
    print("  /unblock <pat>   - Remove block")
    print("  /blocklist       - Show blocked patterns")
    print("  /watch <file>    - Watch file for changes")
    print("  /watch off       - Stop watching")
    print("  /autosync on/off - Auto-send changed files")
    print("  /exit            - Leave room")
    print()
    
    while running:
        try:
            # Colored prompt
            user_input = input(f"{GREEN}> {RESET}").strip()
            if not user_input:
                continue
            
            # ===== COMMAND HANDLING =====
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
                            # Skip special files
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
                    # Save to %synkblock
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
                print(f"{RED}Unknown command. Type /help for commands{RESET}")
        
        except KeyboardInterrupt:
            print(f"\n{YELLOW}Exiting...{RESET}")
            break
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}")

# ===== HOST MODE =====

def host_mode(folder):
    """Start hosting a room"""
    global my_ip, my_name, room_id, room_folder, is_host, running
    
    clear_screen()
    
    # ASCII ART
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
    
    # Create .synkgo folder and %synkblock
    synkgo_dir = Path(room_folder) / ".synkgo"
    synkgo_dir.mkdir(exist_ok=True)
    
    blockfile = Path(room_folder) / "%synkblock"
    if not blockfile.exists():
        with open(blockfile, 'w') as f:
            f.write("# Synkgo blocklist\n# Add patterns like:\n# *.exe\n# *.bat\n# secret/*\n")
    
    load_blocklist()
    save_config()
    
    print(f"{GREEN}Room created!{RESET}")
    print(f"  Room ID: {BOLD}{room_id}{RESET}")
    print(f"  Sharing: {room_folder}")
    print(f"  Your IP: {my_ip}")
    print()
    print(f"{YELLOW}Tell your friends to run:{RESET}")
    print(f"  python synkgo.py -join {room_id}")
    print()
    
    # Start all background threads
    threading.Thread(target=broadcast_room, daemon=True).start()
    threading.Thread(target=discovery_listener, daemon=True).start()
    threading.Thread(target=chat_server, daemon=True).start()
    threading.Thread(target=file_server, daemon=True).start()
    threading.Thread(target=monitor_folder_changes, daemon=True).start()
    
    # Start interactive terminal
    interactive_terminal()
    
    running = False
    print(f"{YELLOW}Room closed{RESET}")

# ===== LIST MODE =====

def list_mode():
    """Scan LAN for available Synkgo rooms"""
    clear_screen()
    print(f"{BOLD}Scanning for Synkgo rooms on LAN...{RESET}")
    print()
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', PORT))
    sock.settimeout(3)
    
    found_rooms = {}
    start_time = time.time()
    
    while time.time() - start_time < 5:  # Scan for 5 seconds
        try:
            data, addr = sock.recvfrom(1024)
            decoded = data.decode()
            if decoded.startswith("SYNKGO:"):
                _, rid, name = decoded.split(':', 2)
                if rid not in found_rooms:
                    found_rooms[rid] = {'name': name, 'ip': addr[0]}
                    print(f"  {GREEN}Room {rid}{RESET} - {name} at {addr[0]}")
        except socket.timeout:
            pass
    
    sock.close()
    
    if not found_rooms:
        print(f"{RED}  No Synkgo rooms found{RESET}")
        print()
        print("Make sure someone is hosting with:")
        print("  python synkgo.py -host .")
    
    print()

# ===== INTERACTIVE MODE (outside room) =====

def interactive_outside():
    """Handle when user runs -int but isn't in a room"""
    clear_screen()
    print(f"{RED}{BOLD}ERROR: You are not in a room{RESET}")
    print()
    print("First, either:")
    print(f"  {GREEN}python synkgo.py -host .{RESET}    (to create a room)")
    print(f"  {GREEN}python synkgo.py -join 1234{RESET} (to join a room)")
    print()

# ===== MAIN ENTRY POINT =====

def main():
    """Main function - parses arguments and starts Synkgo"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Synkgo - LAN file sharing & chat')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-host', metavar='folder', help='Host a room sharing FOLDER')
    group.add_argument('-join', metavar='room_id', help='Join a room by ID (4 digits)')
    group.add_argument('-list', action='store_true', help='List rooms on LAN')
    group.add_argument('-int', action='store_true', help='Interactive terminal (must be in a room)')
    
    args = parser.parse_args()
    
    # Check if we're already in a room (has .synkgo folder)
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