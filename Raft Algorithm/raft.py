from random import randint, random
import sys
import os
import re
import socket
import select
from hashtable import HashTable
from threading import Thread, Lock
import mmh3
import time
from queue import Queue
from random import shuffle
from commit_log import CommitLog
import tqdm
from pathlib import Path
from consistent_hashing import ConsistentHashing
import shutil
import utils
import traceback
from queue import Queue

class Raft:
    def __init__(self, ip, port, partitions):
        self.ip = ip
        self.port = port
        self.ht = HashTable()
        self.commit_log = CommitLog(file=f"commit-log-{self.ip}-{self.port}.txt")
        self.partitions = eval(partitions)
        self.conns = [[None]*len(self.partitions[i]) for i in range(len(self.partitions))]
        self.cluster_index = -1
        self.server_index = -1

        # Initialize commit log file
        commit_logfile = Path(self.commit_log.file)
        commit_logfile.touch(exist_ok=True)

        for i in range(len(self.partitions)):
            cluster = self.partitions[i]

            for j in range(len(cluster)):
                ip, port = cluster[j].split(':')
                port = int(port)

                if (ip, port) == (self.ip, self.port):
                    self.cluster_index = i
                    self.server_index = j

                else:
                    self.conns[i][j] = (ip, port)

        self.current_term = 1
        self.voted_for = -1
        self.votes = set()

        u = len(self.partitions[self.cluster_index])

        self.state = 'FOLLOWER' if len(self.partitions[self.cluster_index]) > 1 else 'LEADER'
        self.leader_id = -1
        self.commit_index = 0
        self.next_indices = [0]*u
        self.match_indices = [-1]*u
        self.election_period_ms = randint(5000, 10000)  # Randomized election timeout between 5-10 seconds
        self.rpc_period_ms = 3000
        self.election_timeout = -1
        self.rpc_timeout = [-1]*u
        self.lease_duration = 5000  # Fixed lease duration of 5 seconds
        self.old_leader_lease_timeout = -1  # To track the maximum old leader lease timeout

        print("Ready...")

    def init(self):
        # set initial election timeout
        self.set_election_timeout()

        # Check for election timeout in the background
        utils.run_thread(fn=self.on_election_timeout, args=())

        # Sync logs or send heartbeats from leader to all servers in the background
        utils.run_thread(fn=self.leader_send_append_entries, args=())

    def set_election_timeout(self, timeout=None):
        # Reset this whenever previous timeout expires and starts a new election
        if timeout:
            self.election_timeout = timeout
        else:
            self.election_timeout = time.time() + randint(self.election_period_ms,
                                                          2*self.election_period_ms)/1000.0

    def on_election_timeout(self):
        while True:
            # Check everytime that state is either FOLLOWER or CANDIDATE before sending
            # vote requests.

            # The possibilities in this path are:
            # 1. Requestor sends requests, receives replies and becomes leader
            # 2. Requestor sends requests, receives replies and becomes follower again, repeat on election timeout
            if time.time() > self.election_timeout and \
                    (self.state == 'FOLLOWER' or self.state == 'CANDIDATE'):

                print(f"Node {self.server_index} election timer timed out, Starting election.")
                self.set_election_timeout()
                self.start_election()

    def start_election(self):
        print("Starting election...")

        # At the start of election, set state to CANDIDATE and increment term
        # also vote for self.
        self.state = 'CANDIDATE'
        self.voted_for = self.server_index
        self.current_term += 1
        self.votes.add(self.server_index)
        self.old_leader_lease_timeout = -1  # Reset the old leader lease timeout

        # Send vote requests in parallel
        threads = []
        for j in range(len(self.partitions[self.cluster_index])):
            if j != self.server_index:
                t = utils.run_thread(fn=self.request_vote, args=(j,))
                threads += [t]

        # Wait for completion of request flow
        for t in threads:
            t.join()

        return True

    def request_vote(self, server):
        # Get last index and term from commit log
        last_index, last_term = self.commit_log.get_last_index_term()

        while True:
            # Retry on timeout
            print(f"Requesting vote from {server}...")

            # Check if state if still CANDIDATE
            if self.state == 'CANDIDATE' and time.time() < self.election_timeout:
                ip, port = self.conns[self.cluster_index][server]
                msg = f"VOTE-REQ {self.server_index} {self.current_term} {last_term} {last_index}"

                resp = \
                    utils.send_and_recv_no_retry(msg, ip, port,
                                                 timeout=self.rpc_period_ms/1000.0)

                # If timeout happens resp returns None, so it won't go inside this condition
                if resp:
                    vote_rep = re.match(
                        '^VOTE-REP ([0-9]+) ([0-9\-]+) ([0-9\-]+) ([0-9\-]+)$', resp)

                    if vote_rep:
                        server, curr_term, voted_for, old_leader_lease_timeout = vote_rep.groups()

                        server = int(server)
                        curr_term = int(curr_term)
                        voted_for = int(voted_for)
                        old_leader_lease_timeout = int(old_leader_lease_timeout)

                        self.process_vote_reply(
                            server, curr_term, voted_for, old_leader_lease_timeout)
                        break
            else:
                break

    def process_vote_request(self, server, term, last_term, last_index):
        print(f"Processing vote request from {server} {term}...")

        if term > self.current_term:
            # Requestor term is higher hence update
            self.step_down(term)

        # Get last index and term from log
        self_last_index, self_last_term = self.commit_log.get_last_index_term()

        # Vote for requestor only if requestor term is equal to self term
        # and self has either voted for no one yet or has voted for same requestor (can happen during failure/timeout and retry)
        # and either both requestor and self have empty logs (starting up)
        # or the last term of requestor is greater
        # or if they are equal then the last index of requestor should be greater.
        # This is to ensure that only vote for all requestors who have updated logs.
        if term == self.current_term \
            and (self.voted_for == server or self.voted_for == -1) \
            and (last_term > self_last_term or
                 (last_term == self_last_term and last_index >= self_last_index)):

            self.voted_for = server
            self.state = 'FOLLOWER'
            self.set_election_timeout()

            if self.state == 'FOLLOWER':
                print(
                    f"Vote granted for Node {server} in term {self.current_term}.")

        else:
            print(f"Vote denied for Node {server} in term {self.current_term}.")

        return f"VOTE-REP {self.server_index} {self.current_term} {self.voted_for} {self.old_leader_lease_timeout}"

    def process_vote_reply(self, server, term, voted_for, old_leader_lease_timeout):
        print(f"Processing vote reply from {server} {term}...")

        # It is not possible to have term < self.current_term because during vote request
        # the server will update its term to match requestor term if requestor term is higher
        if term > self.current_term:
            # Requestor term is lower hence update
            self.step_down(term)

        if term == self.current_term and self.state == 'CANDIDATE':
            if voted_for == self.server_index:
                self.votes.add(server)

                # Keep track of the maximum old leader lease timeout received from voters
                self.old_leader_lease_timeout = max(
                    self.old_leader_lease_timeout, old_leader_lease_timeout)

            # Convert to leader if received votes from majority
            if len(self.votes) > len(self.partitions[self.cluster_index])/2.0:
                self.state = 'LEADER'
                self.leader_id = self.server_index

                print(
                    f"Node {self.server_index} became the leader for term {self.current_term}.")
                print(f"{self.votes}-{self.current_term}")

                # Wait for the maximum of the old leader's lease timer and its own lease timer to run out before acquiring its own lease
                self.wait_for_old_leader_lease_timeout()

                # Start the lease timer and send heartbeats with lease duration
                self.start_lease_timer()
                self.send_heartbeats_with_lease_duration()

    def step_down(self, term):
        print(f"{self.server_index} Stepping down...")

        # Revert to follower state
        self.current_term = term
        self.state = 'FOLLOWER'
        self.voted_for = -1
        self.set_election_timeout()

    def wait_for_old_leader_lease_timeout(self):
        if self.old_leader_lease_timeout > 0:
            print("New Leader waiting for Old Leader Lease to timeout.")
            time.sleep(self.old_leader_lease_timeout / 1000.0)

    def start_lease_timer(self):
        self.lease_start_time = time.time()

    def send_heartbeats_with_lease_duration(self):
        print(
            f"Leader {self.server_index} sending heartbeat & Renewing Lease")
        self.append_noop_entry()

        for j in range(len(self.partitions[self.cluster_index])):
            if j != self.server_index:
                utils.run_thread(fn=self.send_append_entries_request,
                                 args=(j, None))

    def leader_send_append_entries(self):
        print(f"Sending append entries from leader...")

        while True:
            # Check everytime if it is leader before sending append queries
            if self.state == 'LEADER':
                if time.time() - self.lease_start_time > self.lease_duration / 1000.0:
                    # Lease has expired, renew the lease
                    if self.send_heartbeats_with_lease_duration():
                        self.start_lease_timer()
                    else:
                        # Failed to renew the lease, step down as leader
                        print(
                            f"Leader {self.server_index} lease renewal failed. Stepping Down.")
                        self.step_down(self.current_term)
                        break

                self.append_entries()

                # Commit entry after it has been replicated
                last_index, _ = self.commit_log.get_last_index_term()
                self.commit_index = last_index

    def append_noop_entry(self):
        self.commit_log.log(self.current_term, f"NO-OP {self.current_term}")

    def append_entries(self):
        res = Queue()

        for j in range(len(self.partitions[self.cluster_index])):
            if j != self.server_index:
                # Send append entries requests in parallel
                utils.run_thread(
                    fn=self.send_append_entries_request, args=(j, res,))

        if len(self.partitions[self.cluster_index]) > 1:
            cnts = 0

            while True:
                # Wait for servers to respond
                res.get(block=True)
                cnts += 1
                # Once we get reply from majority of servers, then return
                # and don't wait for remaining servers
                # Exclude self
                if cnts > (len(self.partitions[self.cluster_index])/2.0)-1:
                    return
        else:
            return

    def send_append_entries_request(self, server, res=None):
        print(f"Sending append entries to {server}...")

        # Fetch previous index and previous term for log matching
        prev_idx = self.next_indices[server]-1

        # Get all logs from prev_idx onwards, because all logs after prev_idx will be
        # used to replicate to server
        log_slice = self.commit_log.read_logs_start_end(prev_idx)

        if prev_idx == -1:
            prev_term = 0
        else:
            if len(log_slice) > 0:
                prev_term = log_slice[0][0]
                log_slice = log_slice[1:] if len(log_slice) > 1 else []
            else:
                prev_term = 0
                log_slice = []

        # Include lease duration in the AppendEntries RPC
        msg = f"APPEND-REQ {self.server_index} {self.current_term} {prev_idx} {prev_term} {str(log_slice)} {self.commit_index} {self.lease_duration}"

        while True:
            if self.state == 'LEADER':
                ip, port = self.conns[self.cluster_index][server]

                resp = \
                    utils.send_and_recv_no_retry(msg, ip, port,
                                                 timeout=self.rpc_period_ms/1000.0)

                # If timeout happens resp returns None, so it won't go inside this condition
                if resp:
                    append_rep = re.match(
                        '^APPEND-REP ([0-9]+) ([0-9\-]+) ([0-9]+) ([0-9\-]+)$', resp)

                    if append_rep:
                        server, curr_term, flag, index = append_rep.groups()
                        server = int(server)
                        curr_term = int(curr_term)
                        flag = int(flag)
                        success = True if flag == 1 else False
                        index = int(index)

                        self.process_append_reply(
                            server, curr_term, success, index)
                        break
            else:
                break

        if res:
            res.put('ok')

    def process_append_requests(self, server, term, prev_idx, prev_term, logs, commit_index):
        print(f"Processing append request from {server} {term}...")

        # Follower/Candidate received vote reply, reset election timeout
        self.set_election_timeout()

        flag, index = 0, 0

        # If term < self.current_term then the append request came from an old leader
        # and we should not take action in that case.
        if term > self.current_term:
            # Most likely term == self.current_term, if this server participated
            # in voting rounds. If server was down during voting rounds and previous append requests, then term > self.current_term
            # and we should update its term
            self.step_down(term)

        if term == self.current_term:
            # Request came from current leader
            self.leader_id = server

            # Check if the term corresponding to the prev_idx matches with that of the leader
            self_logs = self.commit_log.read_logs_start_end(
                prev_idx, prev_idx) if prev_idx != -1 else []

            # Even with retries, this is idempotent
            success = prev_idx == - \
                1 or (len(self_logs) > 0 and self_logs[0][0] == prev_term)

            if success:
                # On retry, we will overwrite the same logs
                last_index, last_term = self.commit_log.get_last_index_term()

                if len(logs) > 0 and last_term == logs[-1][0] and last_index == self.commit_index:
                    # Check if last term in self log matches leader log and last index in self log matches commit index
                    # Then this is a retry and will avoid overwriting the logs
                    index = self.commit_index
                else:
                    index = self.store_entries(prev_idx, logs)

            flag = 1 if success else 0

        return f"APPEND-REP {self.server_index} {self.current_term} {flag} {index}"

    def process_append_reply(self, server, term, success, index):
        print(f"Processing append reply from {server} {term}...")

        # It cannot be possible that term < self.current_term because at the time of append request,
        # all servers will be updated to current term

        if term > self.current_term:
            # This could be an old leader which become active and sent
            # append requests but on receiving higher term, it will revert
            # back to follower state.
            self.step_down(term)

        if self.state == 'LEADER' and term == self.current_term:
            if success:
                # If server log repaired successfully from leader then increment next index
                self.next_indices[server] = index+1
            else:
                # If server log could not be repaired successfully, then retry with 1 index less lower than current next index
                # Process repeats until we find a matching index and term on server
                self.next_indices[server] = max(0, self.next_indices[server]-1)
                self.send_append_entries_request(server)

    def store_entries(self, prev_idx, leader_logs):
        # Update/Repair server logs from leader logs, replacing non-matching entries and adding non-existent entries
        # Repair starts from prev_idx+1 where prev_idx is the index till where
        # both leader and server logs match.
        commands = [f"{leader_logs[i][1]}" for i in range(len(leader_logs))]
        last_index, _ = self.commit_log.log_replace(
            self.current_term, commands, prev_idx+1)
        self.commit_index = last_index

        # Update state machine
        for command in commands:
            self.update_state_machine(command)

        return last_index

    def update_state_machine(self, command):
        # Update state machine i.e. in memory hash map in this case
        set_ht = re.match('^SET ([^\s]+) ([^\s]+) ([0-9]+)$', command)

        if set_ht:
            key, value, req_id = set_ht.groups()
            req_id = int(req_id)
            self.ht.set(key=key, value=value, req_id=req_id)

    def handle_commands(self, msg, conn):
        set_ht = re.match('^SET ([^\s]+) ([^\s]+) ([0-9]+)$', msg)
        get_ht = re.match('^GET ([^\s]+) ([0-9]+)$', msg)
        vote_req = re.match('^VOTE-REQ ([0-9]+) ([0-9\-]+) ([0-9\-]+) ([0-9\-]+)$', msg)
        append_req = re.match('^APPEND-REQ ([0-9]+) ([0-9\-]+) ([0-9\-]+) ([0-9\-]+) (\[.*?\]) ([0-9\-]+)$', msg)

        if set_ht:
            output = "ko"

            try:
                key, value, req_id = set_ht.groups()
                req_id = int(req_id)

                # Hash based partitioning
                node = mmh3.hash(key, signed=False) % len(self.partitions)

                if self.cluster_index == node:
                    # The key is intended for current cluster

                    while True:
                        if self.state == 'LEADER':
                            # Replicate if this is leader server
                            last_index, _ = self.commit_log.log(self.current_term, msg)
                            # Get response from at-least N/2 number of servers

                            while True:
                                if last_index == self.commit_index:
                                    break

                            # Set state machine
                            self.ht.set(key=key, value=value, req_id=req_id)
                            output = "ok"
                            break
                        else:
                            # If sent to non-leader, then forward to leader
                            # Do not retry here because it might happen that current server becomes leader after sometime
                            # Retry at client/upstream service end
                            if self.leader_id != -1 and self.leader_id != self.server_index:
                                # If request lands up on the server which was not present in the majority
                                # when the leader sent and received append queries successfully. The leader_id
                                # for these servers will still be -1
                                output = utils.send_and_recv_no_retry(msg,
                                                                    self.conns[node][self.leader_id][0],
                                                                    self.conns[node][self.leader_id][1],
                                                                    timeout=self.rpc_period_ms)
                                if output is not None:
                                    break
                            else:
                                output = 'ko'
                                break
                else:
                    # Forward to relevant cluster (1st in partitions config) if key is not intended for this cluster
                    # Retry here because this is different partition
                    output = utils.send_and_recv(msg,
                                                self.conns[node][0][0],
                                                self.conns[node][0][1])
                    if output is None:
                        output = "ko"

                # Introduce a delay after processing SET requests
                time.sleep(0.01)  # Adjust the delay as needed

            except Exception as e:
                traceback.print_exc(limit=1000)

        elif get_ht:
            output = "ko"

            try:
                key, _ = get_ht.groups()
                node = mmh3.hash(key, signed=False) % len(self.partitions)

                if self.cluster_index == node:
                    # The key is intended for current cluster

                    while True:
                        if self.state == 'LEADER':
                            output = self.ht.get_value(key=key)
                            if output:
                                output = str(output)
                            else:
                                output = 'Error: Non existent key'
                            break

                        else:
                            # If sent to non-leader, then forward to leader
                            # Do not retry here because it might happen that current server becomes leader after sometime
                            # Retry at client/upstream service end
                            if self.leader_id != -1 and self.leader_id != self.server_index:
                                output = utils.send_and_recv_no_retry(msg,
                                                                      self.conns[node][self.leader_id][0],
                                                                      self.conns[node][self.leader_id][1],
                                                                      timeout=self.rpc_period_ms)
                                if output is not None:
                                    break
                            else:
                                output = 'ko'
                                break

                else:
                    # Forward to relevant cluster (1st in partitions config) if key is not intended for this cluster
                    # Retry here because this is different partition
                    output = utils.send_and_recv(msg,
                                                 self.conns[node][0][0],
                                                 self.conns[node][0][1])
                    if output is None:
                        output = "ko"

            except Exception as e:
                traceback.print_exc(limit=1000)

        elif vote_req:
            try:
                server, curr_term, last_term, last_indx = vote_req.groups()
                server = int(server)
                curr_term = int(curr_term)
                last_term = int(last_term)
                last_indx = int(last_indx)

                output = self.process_vote_request(
                    server, curr_term, last_term, last_indx)

            except Exception as e:
                traceback.print_exc(limit=1000)

        elif append_req:
            try:
                server, curr_term, prev_idx, prev_term, logs, commit_index = append_req.groups()
                server = int(server)
                curr_term = int(curr_term)
                prev_idx = int(prev_idx)
                prev_term = int(prev_term)
                logs = eval(logs)
                commit_index = int(commit_index)

                output = self.process_append_requests(
                    server, curr_term, prev_idx, prev_term, logs, commit_index)

            except Exception as e:
                traceback.print_exc(limit=1000)

        else:
            print("Hello1 - " + msg + " - Hello2")
            output = "Error: Invalid command"

        return output

    def process_request(self, conn):
        while True:
            try:
                msg = conn.recv(2048)
                if not msg:
                    # Client disconnected, clean up the connection
                    print("Client disconnected")
                    conn.close()
                    break

                msg = msg.decode()
                print(f"{msg} received")
                output = self.handle_commands(msg, conn)
                conn.sendall(output.encode())

            except ConnectionResetError:
                # Connection was reset by the client
                print("Connection reset by client")
                conn.close()
                break

            except Exception as e:
                traceback.print_exc(limit=1000)
                print("Error processing message from client")
                conn.close()
                break

            except Exception as e:
                traceback.print_exc(limit=1000)
                print("Error processing message from client")
                conn.close()
                break

    def listen_to_clients(self):
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(('0.0.0.0', int(self.port)))
        server_socket.listen(50)

        print(f"Server listening on {self.ip}:{self.port}")

        while True:
            try:
                client_socket, client_address = server_socket.accept()
                print(f"Connected to new client at address {client_address}")
                client_thread = Thread(target=self.process_request, args=(client_socket,))
                client_thread.daemon = True
                client_thread.start()

            except Exception as e:
                print(f"Error accepting connection: {e}")
                continue


if __name__ == '__main__':
    ip_address = str(sys.argv[1])
    port = int(sys.argv[2])
    partitions = str(sys.argv[3])

    dht = Raft(ip=ip_address, port=port, partitions=partitions)
    utils.run_thread(fn=dht.init, args=())
    dht.listen_to_clients()



    #source myenv/bin/activate          