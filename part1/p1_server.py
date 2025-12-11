# --- p1_server.py ---
import socket
import struct
import time
import sys
import os

# Constants
MAX_PAYLOAD_SIZE = 1200
HEADER_SIZE = 20
DATA_SIZE = MAX_PAYLOAD_SIZE - HEADER_SIZE  # 1180 bytes
SEQ_NUM_SIZE = 4
RESERVED_SIZE = 16
EOF_MARKER = b"EOF"
TIMEOUT_INITIAL = 1.0  # Initial RTO in seconds
ALPHA = 0.125  # For RTT estimation
BETA = 0.25


class ReliableUDPServer:
    """Server with selective-ACK support and per-packet timers.

    SACK format (reserved 16 bytes): up to two SACK blocks, each 8 bytes:
    [start1(4)][end1(4)][start2(4)][end2(4)]

    ACK packet format (client -> server):
    [cum_ack (4 bytes)][start1(4)][end1(4)][start2(4)][end2(4)]
    """

    def __init__(self, host, port, filename, window_size):
        self.host = host
        self.port = port
        self.filename = filename
        # window_size is SWS in bytes (maximum number of unique unacked bytes allowed in flight)
        self.window_size = window_size
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host, self.port))

        # RTT estimation
        self.estimated_rtt = TIMEOUT_INITIAL
        self.dev_rtt = 0
        self.rto = TIMEOUT_INITIAL

        # Packet storage
        self.packets = []  # list of packet payloads (data part only)
        self.packet_bytes = []  # length of each data payload

        # Tracking send/ack state
        self.sent_once = set()  # seq numbers that have been sent at least once
        self.unacked = set()  # seq numbers sent but not yet acknowledged
        self.send_times = {}  # per-packet send time: seq -> timestamp

        # bytes currently in flight (unique unacked bytes)
        self.bytes_in_flight = 0

        print(f"Server initialized on {host}:{port}")
        print(f"File: {filename}, Window Size (bytes): {window_size}")

    # ---------- helpers for packet creation/parsing ----------
    def create_packet(self, seq_num, data):
        seq_bytes = struct.pack("!I", seq_num)
        reserved = b"\x00" * RESERVED_SIZE
        return seq_bytes + reserved + data

    def parse_ack(self, ack_packet):
        if len(ack_packet) < SEQ_NUM_SIZE + RESERVED_SIZE:
            return None, []
        cum_ack = struct.unpack("!I", ack_packet[:4])[0]
        # parse two SACK blocks
        sack_bytes = ack_packet[4:4 + RESERVED_SIZE]
        blocks = []
        for i in range(0, RESERVED_SIZE, 8):
            start = struct.unpack("!I", sack_bytes[i:i+4])[0]
            end = struct.unpack("!I", sack_bytes[i+4:i+8])[0]
            if start == 0 and end == 0:
                continue
            blocks.append((start, end))
        return cum_ack, blocks

    def update_rto_on_sample(self, sample_rtt):
        if self.estimated_rtt == TIMEOUT_INITIAL:
            self.estimated_rtt = sample_rtt
            self.dev_rtt = sample_rtt / 2
        else:
            self.dev_rtt = (1 - BETA) * self.dev_rtt + BETA * abs(sample_rtt - self.estimated_rtt)
            self.estimated_rtt = (1 - ALPHA) * self.estimated_rtt + ALPHA * sample_rtt
        self.rto = max(0.05, min(self.estimated_rtt + 4 * self.dev_rtt, 5.0))

    # ---------- core sending logic ----------
    def send_file(self, client_addr):
        try:
            with open(self.filename, 'rb') as f:
                file_data = f.read()

            # split into chunks
            offset = 0
            while offset < len(file_data):
                chunk = file_data[offset:offset + DATA_SIZE]
                self.packets.append(chunk)
                self.packet_bytes.append(len(chunk))
                offset += DATA_SIZE

            total_packets = len(self.packets)
            print(f"File size: {len(file_data)} bytes, total packets: {total_packets}")

            next_seq = 0
            self.last_cum_ack = -1
            self.dup_ack_count = 0

            while True:
                # Send as many new packets as window allows
                made_progress = False
                while next_seq < total_packets and (self.bytes_in_flight + self.packet_bytes[next_seq]) <= self.window_size:
                    pkt = self.create_packet(next_seq, self.packets[next_seq])
                    self.sock.sendto(pkt, client_addr)
                    now = time.time()
                    if next_seq not in self.sent_once:
                        self.sent_once.add(next_seq)
                        self.unacked.add(next_seq)
                        self.bytes_in_flight += self.packet_bytes[next_seq]
                    self.send_times[next_seq] = now
                    print(f"Sent packet {next_seq} (bytes_in_flight={self.bytes_in_flight})")
                    next_seq += 1
                    made_progress = True

                if next_seq >= total_packets and len(self.unacked) == 0:
                    print("All data packets acknowledged")
                    break

                self.sock.settimeout(0.05)
                try:
                    ack_packet, _ = self.sock.recvfrom(1024)
                    cum_ack, sack_blocks = self.parse_ack(ack_packet)
                    print(f"Received ACK: cum_ack={cum_ack}, SACK={sack_blocks}")

                    # --- NEW: duplicate ACK detection ---
                    if cum_ack == self.last_cum_ack:
                        self.dup_ack_count += 1
                        print(f"Duplicate ACK #{self.dup_ack_count} for cum_ack={cum_ack}")
                    else:
                        self.last_cum_ack = cum_ack
                        self.dup_ack_count = 0

                    # --- FAST RETRANSMIT trigger ---
                    if self.dup_ack_count >= 3 and len(self.unacked) > 0:
                        oldest_unacked = min(self.unacked)
                        print(f"Triple duplicate ACK detected → Fast retransmit packet {oldest_unacked}")
                        pkt = self.create_packet(oldest_unacked, self.packets[oldest_unacked])
                        self.sock.sendto(pkt, client_addr)
                        self.send_times[oldest_unacked] = time.time()
                        self.dup_ack_count = 0  # optional reset

                    # cumulative ACK handling
                    newly_acked = set()
                    for seq in list(self.unacked):
                        if seq < cum_ack:
                            newly_acked.add(seq)

                    # SACK block handling
                    for (start, end) in sack_blocks:
                        s = max(0, start)
                        e = min(end, total_packets - 1)
                        for seq in range(s, e + 1):
                            if seq in self.unacked:
                                newly_acked.add(seq)

                    now = time.time()
                    for seq in sorted(newly_acked):
                        if seq in self.unacked:
                            self.unacked.remove(seq)
                        self.bytes_in_flight -= self.packet_bytes[seq]
                        if seq in self.send_times:
                            sample_rtt = now - self.send_times[seq]
                            self.update_rto_on_sample(sample_rtt)
                            del self.send_times[seq]
                        print(f"Packet {seq} acknowledged (bytes_in_flight={self.bytes_in_flight})")

                except socket.timeout:
                    # Check per-packet timers
                    now = time.time()
                    timed_out_seqs = []
                    for seq in list(self.unacked):
                        send_t = self.send_times.get(seq)
                        if send_t and (now - send_t > self.rto):
                            timed_out_seqs.append(seq)

                    if timed_out_seqs:
                        for seq in timed_out_seqs:
                            pkt = self.create_packet(seq, self.packets[seq])
                            self.sock.sendto(pkt, client_addr)
                            self.send_times[seq] = time.time()
                            print(f"Retransmitted timed-out packet {seq}")
                        continue

                    if not made_progress:
                        time.sleep(0.01)
                        continue

            # -- send EOF reliably (same SACK-aware ACK logic) --
            eof_seq = total_packets
            eof_packet = self.create_packet(eof_seq, EOF_MARKER)
            max_retries = 10
            retries = 0
            eof_acked = False
            while retries < max_retries and not eof_acked:
                self.sock.sendto(eof_packet, client_addr)
                print(f"Sent EOF (attempt {retries+1})")
                self.sock.settimeout(self.rto)
                try:
                    ack_packet, _ = self.sock.recvfrom(1024)
                    cum_ack, _ = self.parse_ack(ack_packet)
                    if cum_ack > eof_seq:
                        eof_acked = True
                        print("EOF acknowledged by client")
                        break
                except socket.timeout:
                    retries += 1
                    print("Timeout waiting for EOF ACK")
                    continue

            if not eof_acked:
                print("Warning: EOF not acknowledged after retries")
            else:
                print("File transfer complete")

        except Exception as e:
            print(f"Error during send_file: {e}")
            raise

    def start(self):
        print("Waiting for client request...")
        while True:
            try:
                self.sock.settimeout(10)
                data, client_addr = self.sock.recvfrom(1024)
                if data:
                    print(f"Received request from {client_addr}")
                    # send small ACK to indicate server is ready
                    ready_pkt = self.create_packet(0, b"ACK")
                    self.sock.sendto(ready_pkt, client_addr)
                    self.send_file(client_addr)
                    break
            except socket.timeout:
                print("Waiting for client...")
                continue
        self.sock.close()


def main_server():
    if len(sys.argv) != 4:
        print("Usage: python3 p1_server.py <SERVER_IP> <SERVER_PORT> <SWS_bytes>")
        sys.exit(1)
    host = sys.argv[1]
    port = int(sys.argv[2])
    sws = int(sys.argv[3])
    filename = "data.txt"
    if not os.path.exists(filename):
        print(f"File {filename} not found")
        sys.exit(1)
    server = ReliableUDPServer(host, port, filename, sws)
    server.start()


if __name__ == "__main__":
    main_server()