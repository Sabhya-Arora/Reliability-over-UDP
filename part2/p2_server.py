# --- p1_server_cubic.py ---
import socket
import struct
import time
import sys
import os
import math

# Constants
MAX_PAYLOAD_SIZE = 1200
HEADER_SIZE = 20
DATA_SIZE = MAX_PAYLOAD_SIZE - HEADER_SIZE  # 1180 bytes
SEQ_NUM_SIZE = 4
RESERVED_SIZE = 16
EOF_MARKER = b"EOF"
TIMEOUT_INITIAL = 1.0
ALPHA = 0.125
BETA_RTT = 0.25

# CUBIC parameters
C_CUBIC = 10000
BETA_CUBIC = 0.3
CWND_DEC = 0.5
MAX_CWND = 10000  # in MSS units


class ReliableUDPServerCubic:
    """Server with SACK + per-packet timers + TCP CUBIC congestion control + Recovery/Drain phase."""

    def __init__(self, host, port, filename):
        self.host = host
        self.port = port
        self.filename = filename
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.host, self.port))

        # RTT estimation
        self.estimated_rtt = TIMEOUT_INITIAL
        self.dev_rtt = 0
        self.rto = TIMEOUT_INITIAL

        # File / packet storage
        self.packets = []
        self.packet_bytes = []

        # Tracking state
        self.sent_once = set()
        self.unacked = set()
        self.send_times = {}
        self.bytes_in_flight = 0

        # CUBIC state
        self.cwnd = DATA_SIZE        # Start with 1 MSS
        self.Wmax = DATA_SIZE * 200  # last maximum cwnd before congestion
        self.epoch_start = None
        self.K = 0
        self.last_loss_time = 0

        # fast retransmit / recovery
        self.last_cum_ack = -1
        self.dup_ack_count = 0
        self.in_recovery = False
        self.recovery_ack_point = None
        self.lost_seq = None

        #print(f"[CUBIC Server] Initialized on {host}:{port}")
        #print(f"File: {filename}, Initial cwnd={self.cwnd} bytes")

    # ---------- helper methods ----------
    def create_packet(self, seq_num, data):
        seq_bytes = struct.pack("!I", seq_num)
        reserved = b"\x00" * RESERVED_SIZE
        return seq_bytes + reserved + data

    def parse_ack(self, ack_packet):
        if len(ack_packet) < SEQ_NUM_SIZE + RESERVED_SIZE:
            return None, []
        cum_ack = struct.unpack("!I", ack_packet[:4])[0]
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
            self.dev_rtt = (1 - BETA_RTT) * self.dev_rtt + BETA_RTT * abs(sample_rtt - self.estimated_rtt)
            self.estimated_rtt = (1 - ALPHA) * self.estimated_rtt + ALPHA * sample_rtt
        self.rto = max(0.05, min(self.estimated_rtt + 4 * self.dev_rtt, 5.0))

    # ---------- CUBIC functions ----------
    def cubic_update(self):
        """Update cwnd as per CUBIC function (only if not in recovery)."""
        if self.in_recovery:
            #print(f"[IN RECOVERY] Skipping cwnd update (cwnd={self.cwnd})")
            return

        now = time.time()
        if self.epoch_start is None:
            self.epoch_start = now
            self.K = ((self.Wmax * BETA_CUBIC) / C_CUBIC) ** (1 / 3)
            #print(f"[CUBIC] Epoch start, K={self.K:.3f}, Wmax={self.Wmax}")
            return

        t = now - self.epoch_start
        Wt = C_CUBIC * ((t - self.K) ** 3) + self.Wmax
        self.cwnd = max(10 * DATA_SIZE, min(Wt, MAX_CWND * DATA_SIZE))
        #print(f"[CUBIC] Updated cwnd={self.cwnd:.1f} bytes (Wt={Wt:.1f})")

    def on_congestion_event(self, severe=False):
        """Handle congestion (either timeout or triple duplicate ACK)."""
        if severe:
            self.Wmax = self.cwnd / 2
            self.cwnd = 10 * DATA_SIZE  # reset to 1 MSS
            #print(f"[CUBIC] Timeout: cwnd reset to 1 MSS ({self.cwnd}), Wmax={self.Wmax}")
        else:
            self.Wmax = self.cwnd
            self.cwnd *= (CWND_DEC)
            #print(f"[CUBIC] Triple duplicate ACK: cwnd reduced to {self.cwnd:.1f}, Wmax={self.Wmax}")
        self.epoch_start = None  # reset epoch

    # ---------- core file sending ----------
    def send_file(self, client_addr):
        with open(self.filename, 'rb') as f:
            file_data = f.read()
        congestion_last_sent = None
        offset = 0
        while offset < len(file_data):
            chunk = file_data[offset:offset + DATA_SIZE]
            self.packets.append(chunk)
            self.packet_bytes.append(len(chunk))
            offset += DATA_SIZE

        total_packets = len(self.packets)
        #print(f"File size: {len(file_data)} bytes, total packets: {total_packets}")

        next_seq = 0
        while True:
            made_progress = False

            # Send new data only if within cwnd limit or not in drain phase
            while next_seq < total_packets and (self.bytes_in_flight + self.packet_bytes[next_seq]) <= self.cwnd:
                # Pause new data if in recovery and flight > cwnd
                if self.in_recovery and self.bytes_in_flight > self.cwnd:
                    break

                pkt = self.create_packet(next_seq, self.packets[next_seq])
                self.sock.sendto(pkt, client_addr)
                now = time.time()
                if next_seq not in self.sent_once:
                    self.sent_once.add(next_seq)
                    self.unacked.add(next_seq)
                    self.bytes_in_flight += self.packet_bytes[next_seq]
                self.send_times[next_seq] = now
                #print(f"Sent packet {next_seq}, bytes_in_flight={self.bytes_in_flight}, cwnd={self.cwnd:.1f}")
                next_seq += 1
                made_progress = True

            if next_seq >= total_packets and len(self.unacked) == 0:
                #print("All data packets acknowledged")
                break

            self.sock.settimeout(self.rto + 0.01)
            try:
                ack_packet, _ = self.sock.recvfrom(1024)
                cum_ack, sack_blocks = self.parse_ack(ack_packet)
                #print(f"Received ACK: cum_ack={cum_ack}, SACK={sack_blocks}")

                # Duplicate ACK detection
                if cum_ack == self.last_cum_ack:
                    self.dup_ack_count += 1
                    #print(f"Duplicate ACK #{self.dup_ack_count} for cum_ack={cum_ack}")
                else:
                    # On ACK progress: exit recovery if lost packet ACKed
                    if self.in_recovery and cum_ack > congestion_last_sent:
                        #print(f"[EXIT RECOVERY] cum_ack={cum_ack} > lost_seq={self.lost_seq}")
                        self.in_recovery = False
                        self.recovery_ack_point = None
                        # self.epoch_start = time.time()
                    self.last_cum_ack = cum_ack
                    self.dup_ack_count = 0

                # --- FAST RETRANSMIT (triggered once per event) ---
                if self.dup_ack_count >= 3 and len(self.unacked) > 0:
                    if not self.in_recovery:
                        oldest_unacked = min(self.unacked)
                        self.lost_seq = cum_ack
                        pkt = self.create_packet(oldest_unacked, self.packets[oldest_unacked])
                        self.sock.sendto(pkt, client_addr)
                        self.send_times[oldest_unacked] = time.time()
                        self.in_recovery = True
                        self.recovery_ack_point = cum_ack
                        congestion_last_sent = next_seq - 1
                        #print(f"[FAST RETRANSMIT] Resent packet {oldest_unacked}, entered recovery.")
                        self.on_congestion_event(severe=False)
                        self.dup_ack_count = 0
                    else:
                        if self.dup_ack_count % 100 == 0:
                            now = time.time()
                            timeouts = []
                            for seq in list(self.unacked):
                                t_sent = self.send_times.get(seq)
                                if t_sent and (now - t_sent > self.rto):
                                    timeouts.append(seq)

                            if timeouts:
                                #print(f"[TIMEOUT] Packets {timeouts} expired, retransmitting")
                                # self.on_congestion_event(severe=True)
                                for seq in timeouts:
                                    pkt = self.create_packet(seq, self.packets[seq])
                                    self.sock.sendto(pkt, client_addr)
                                    self.send_times[seq] = time.time()
                                # continue
                        #print("[FAST RETRANSMIT] Already in recovery, skipping cwnd/Wmax change")
                if self.dup_ack_count > 0 and self.in_recovery:
                    self.cwnd += DATA_SIZE
                # Process ACKed packets
                newly_acked = set()
                for seq in list(self.unacked):
                    if seq < cum_ack:
                        newly_acked.add(seq)
                for (start, end) in sack_blocks:
                    for seq in range(start, end + 1):
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
                    #print(f"Packet {seq} ACKed (bytes_in_flight={self.bytes_in_flight})")

                # Update cwnd by CUBIC law only if not recovering
                if not self.in_recovery:
                    self.cubic_update()
                # else:
                    #print("[IN RECOVERY] Holding cwnd constant.")

            except socket.timeout:
                now = time.time()
                timeouts = []
                for seq in list(self.unacked):
                    t_sent = self.send_times.get(seq)
                    if t_sent and (now - t_sent > self.rto):
                        timeouts.append(seq)

                if timeouts:
                    #print(f"[TIMEOUT] Packets {timeouts} expired, retransmitting")
                    self.on_congestion_event(severe=True)
                    for seq in timeouts:
                        pkt = self.create_packet(seq, self.packets[seq])
                        self.sock.sendto(pkt, client_addr)
                        self.send_times[seq] = time.time()
                    continue

                if not made_progress:
                    time.sleep(0.01)

        # EOF phase
        eof_seq = total_packets
        eof_packet = self.create_packet(eof_seq, EOF_MARKER)
        for i in range(5):
            self.sock.sendto(eof_packet, client_addr)
            #print(f"Sent EOF (attempt {i+1})")
            self.sock.settimeout(self.rto)
            try:
                ack_packet, _ = self.sock.recvfrom(1024)
                cum_ack, _ = self.parse_ack(ack_packet)
                if cum_ack > eof_seq:
                    #print("EOF acknowledged.")
                    break
            except socket.timeout:
                continue
        #print("File transfer complete.")

    def start(self):
        #print("Waiting for client...")
        while True:
            try:
                self.sock.settimeout(10)
                data, client_addr = self.sock.recvfrom(1024)
                if data:
                    #print(f"Client {client_addr} connected.")
                    self.sock.sendto(self.create_packet(0, b"ACK"), client_addr)
                    self.send_file(client_addr)
                    break
            except socket.timeout:
                #print("Waiting for client...")
                continue
        self.sock.close()


def main():
    if len(sys.argv) != 3:
        #print("Usage: python3 p1_server_cubic.py <IP> <PORT>")
        sys.exit(1)
    host = sys.argv[1]
    port = int(sys.argv[2])
    filename = "data.txt"
    if not os.path.exists(filename):
        #print(f"{filename} not found.")
        sys.exit(1)
    server = ReliableUDPServerCubic(host, port, filename)
    server.start()


if __name__ == "__main__":
    main()