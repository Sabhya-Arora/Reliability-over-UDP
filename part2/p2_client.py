# --- p1_client.py ---
import socket
import struct
import sys
import time
import os

# Constants reused
MAX_PAYLOAD_SIZE = 1200
HEADER_SIZE = 20
DATA_SIZE = MAX_PAYLOAD_SIZE - HEADER_SIZE  # 1180 bytes
SEQ_NUM_SIZE = 4
RESERVED_SIZE = 16
EOF_MARKER = b"EOF"
REQUEST_TIMEOUT = 2.0
MAX_REQUEST_RETRIES = 5


class ReliableUDPClient:
    """Client that sends cumulative ACK + up to two SACK blocks in the 16 reserved bytes.

    The ACK packet layout:
    [next_expected_seq (4)][start1(4)][end1(4)][start2(4)][end2(4)]
    """

    def __init__(self, server_host, server_port, output_filename):
        self.server_host = server_host
        self.server_port = int(server_port)
        self.output_filename = output_filename
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.receive_buffer = {}  # seq -> data
        self.received_seqs = set()
        self.next_expected_seq = 0
        self.received_data = bytearray()
        #print(f"Client initialized for {server_host}:{server_port}")

    def parse_packet(self, packet):
        if len(packet) < HEADER_SIZE:
            return None, None
        seq = struct.unpack("!I", packet[:4])[0]
        data = packet[HEADER_SIZE:]
        return seq, data

    def create_ack(self, next_expected_seq, sack_blocks=None):
        seq_bytes = struct.pack("!I", next_expected_seq)
        if sack_blocks is None:
            sack_blocks = []
        reserved = b"\x00" * RESERVED_SIZE
        # build up to two blocks
        parts = []
        for (s, e) in sack_blocks[:2]:
            parts.append(struct.pack("!I", s))
            parts.append(struct.pack("!I", e))
        # pad if less than 2 blocks
        while len(parts) < 4:
            parts.append(struct.pack("!I", 0))
        reserved = b"".join(parts)
        return seq_bytes + reserved

    def find_sack_blocks(self, recent_seq):
        # produce up to two contiguous blocks of received sequences
        # first block should contain the recent_seq
        blocks = []
        if recent_seq is None:
            return blocks
        if recent_seq not in self.received_seqs:
            # Nothing to SACK for this
            return blocks
        # expand around recent_seq
        start = recent_seq
        end = recent_seq
        while (start - 1) in self.received_seqs:
            start -= 1
        while (end + 1) in self.received_seqs:
            end += 1
        blocks.append((start, end))
        # find another block (the next highest block beyond end)
        # gather all blocks sorted
        all_seqs = sorted(self.received_seqs)
        # find blocks by scanning
        blocks_found = []
        i = 0
        n = len(all_seqs)
        while i < n:
            s = all_seqs[i]
            e = s
            i += 1
            while i < n and all_seqs[i] == e + 1:
                e = all_seqs[i]
                i += 1
            blocks_found.append((s, e))
        # pick a different block if exists
        for (s, e) in blocks_found:
            if s <= recent_seq <= e:
                continue
            blocks.append((s, e))
            break
        return blocks[:2]

    def send_request(self):
        request = b"\x01"
        for attempt in range(MAX_REQUEST_RETRIES):
            self.sock.sendto(request, (self.server_host, self.server_port))
            self.sock.settimeout(REQUEST_TIMEOUT)
            try:
                resp, _ = self.sock.recvfrom(MAX_PAYLOAD_SIZE)
                if resp:
                    #print("Server acknowledged request, starting transfer")
                    return True
            except socket.timeout:
                #print("Timeout waiting for server response (request)")
                continue
        return False

    def receive_file(self):
        self.sock.settimeout(3)
        last_received_seq = None
        while True:
            try:
                packet, server_addr = self.sock.recvfrom(MAX_PAYLOAD_SIZE)
                seq, data = self.parse_packet(packet)
                if seq is None:
                    continue

                # EOF handling
                if data == EOF_MARKER:
                    #print("Received EOF")
                    final_ack = self.create_ack(seq + 1, [])
                    # send final ACK a few times
                    for _ in range(5):
                        self.sock.sendto(final_ack, server_addr)
                    break

                #print(f"Received packet {seq}")
                # if not received, store it
                if seq not in self.received_seqs:
                    self.receive_buffer[seq] = data
                    self.received_seqs.add(seq)

                # deliver any in-order data starting from next_expected_seq
                made_progress = True
                while made_progress:
                    made_progress = False
                    if self.next_expected_seq in self.receive_buffer:
                        d = self.receive_buffer.pop(self.next_expected_seq)
                        self.received_data.extend(d)
                        self.received_seqs.remove(self.next_expected_seq)
                        self.next_expected_seq += 1
                        made_progress = True

                # compute SACK blocks centered on the most recently received packet
                last_received_seq = seq
                sack_blocks = self.find_sack_blocks(last_received_seq)

                ack_pkt = self.create_ack(self.next_expected_seq, sack_blocks)
                self.sock.sendto(ack_pkt, server_addr)
                #print(f"Sent ACK next_expected={self.next_expected_seq}, SACK={sack_blocks}")

            except socket.timeout:
                # periodically resend ACK for the current state to help server
                ack_pkt = self.create_ack(self.next_expected_seq, self.find_sack_blocks(last_received_seq))
                try:
                    self.sock.sendto(ack_pkt, (self.server_host, self.server_port))
                    #print(f"Timeout: resent ACK next_expected={self.next_expected_seq}")
                except Exception:
                    pass
                continue

        # write to file
        with open(self.output_filename, 'wb') as f:
            f.write(self.received_data)
        #print(f"Wrote {len(self.received_data)} bytes to {self.output_filename}")

    def start(self):
        if not self.send_request():
            #print("Failed to contact server")
            return
        try:
            self.receive_file()
        except Exception as e:
            print(f"Error during receive_file: {e}")
        finally:
            self.sock.close()


def main_client():
    if len(sys.argv) != 4:
        #print("Usage: python3 p1_client.py <SERVER_IP> <SERVER_PORT> <OUTPUT_FILE_PREFIX>")
        sys.exit(1)
    server = sys.argv[1]
    port = sys.argv[2]
    prefix = sys.argv[3]
    out = f"{prefix}received_data.txt"
    client = ReliableUDPClient(server, port, out)
    client.start()

if __name__ == "__main__":
    main_client()