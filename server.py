import argparse
import threading
import time
from pythonosc import dispatcher
from pythonosc import osc_server
from pythonosc import udp_client
from utils import read_state, getSendingMsg
from record import Record
from majority import MajorityCheck
from election import Election
from message import Message

from threading import Lock

class ServerProcess:
    def __init__(self, pid, server_count, client_count):
        self.mutex = Lock()

        self.pid = pid
        self.view = -1

        serverConfig = read_state("servers_config", server_count)
        self.port = serverConfig[self.pid].port
        self.sendChannels = []
        for p in serverConfig:
            s = udp_client.SimpleUDPClient(p.ip, p.port)
            self.sendChannels.append(s)

        clientConfig = read_state("clients_config", client_count)
        self.clientChannels = []
        for p in clientConfig:
            s = udp_client.SimpleUDPClient(p.ip, p.port)
            self.clientChannels.append(s)

        self.totalNumber = len(serverConfig)
        MajorityCheck.total = self.totalNumber
        self.electionStatus = None

        # multi-paxos
        self.roundNumber = -1  # index for messages
        self.records = []
        self.buffer = []
        for _ in range(100):
            self.records += [None]


    def start(self):
        # initialze listening channels
        d = dispatcher.Dispatcher()
        d.map("/iAmLeader", self.iAmLeader_handler, self)
        d.map("/youAreLeader", self.youAreLeader_handler, self)
        d.map("/valueProposal", self.valueProposal_handler, self)
        d.map("/accept", self.accept_handler, self)
        d.map("/leaderFaulty", self.leaderFaulty_handler, self)
        d.map("/clientRequest", self.clientRequest_handler, self)

        listen = osc_server.ThreadingOSCUDPServer(("127.0.0.1", self.port), d)
        listeningThread = threading.Thread(target=listen.serve_forever)
        listeningThread.start()
        print("Process {} started.".format(self.pid))

        self.shouldIBeLeader(self.view)


    def shouldIBeLeader(self, view):
        nextView = view + 1
        if (nextView % self.totalNumber) == self.pid:
            print("Sending iAmLeader.")
            self.electionStatus = Election(nextView, self.roundNumber+1, None)
            self.sendMessageToServers("/iAmLeader", self.electionStatus.toString())
        else:
            self.electionStatus = None
            print("It's not my turn to be a leader.")

    # acceptor
    def iAmLeader_handler(self, addr, args, recievedMsg):
        print("\n"+addr)
        election = Election.fromString(recievedMsg)
        newView = election.view
        newLeader = newView % self.totalNumber
        newRound = election.roundNumber
        print("Process {} sent iAmLeader.".format(newLeader))

        if newView >= self.view:
            previousView = self.view
            self.view = newView

            # send back my previous state
            if self.roundNumber < newRound:
                previousValue = None
            elif self.roundNumber == newRound:
                record = self.records[newRound]
                if record.learned == False: # only when it is not learned yet
                    previousValue = record.message
            else:
                # this guy is missing some records.
                self.sendLeaderFaulty()
                return

            print("Sending youAreLeader...")
            response = Election(previousView, self.roundNumber, previousValue)
            leaderChannel = self.sendChannels[newLeader]
            leaderChannel.send_message("/youAreLeader", response.toString())

    # new_leader
    def youAreLeader_handler(self, addr, args, recievedMsg):
        print("\n" + addr)
        if self.electionStatus is not None and self.electionStatus.decided == False:
            response = Election.fromString(recievedMsg)
            if response.view > self.electionStatus.view and (response.view % self.totalNumber) != self.pid:  # not me
                self.electionStatus.latestValue = Message.fromString(response.latestValue)

            if self.electionStatus.majorityCheck.addVoteAndCheck():
                self.electionStatus.decided = True
                print("Yeah, I become a leader. Ready to process client requests.")
                if self.electionStatus.latestValue is not None:
                    # TODO there's some value accepted but not learned yet.
                    print("I need to propose this value")

    # all processes
    def clientRequest_handler(self, addr, args, recievedMsg):
        print("\n" + addr)
        print("ClientRequest, msg: ", recievedMsg)
        message = Message.fromString(recievedMsg)
        if (self.view % self.totalNumber == self.pid):
            self.roundNumber += 1
            record = Record(self.view, self.roundNumber, message)
            self.sendMessageToServers("/valueProposal", record.toString())
        else:
            self.buffer.append(message)

    # acceptor
    def valueProposal_handler(self, addr, args, recievedMsg):
        print("\n"+addr)
        record = Record.fromString(recievedMsg)

        if record.view >= self.view:
            self.view = record.view # in case you missed the leader election
            # self.appendRecord(record)
            # TODO here, the acceptor detects if it misses some rounds.
            self.sendMessageToServers("/accept", record.toString())

    # learner
    def accept_handler(self, addr, args, recievedMsg):
        print("\n"+addr)
        received = Record.fromString(recievedMsg)
        record = self.records[received.roundNumber]
        if record is None:
            print("I don't have this record, yet.")
            roundNumber = received.roundNumber
            if len(self.records) <= roundNumber:
                print("allocate array for record")
                for _ in range(100):
                    self.records += [None]

            self.mutex.acquire()
            try:
                self.roundNumber = roundNumber
                self.records[self.roundNumber] = received
            finally:
                self.mutex.release()

            record = self.records[self.roundNumber]

        if record.majorityCheck.addVoteAndCheck() == True:
            record.learned = True
            self.buffer.remove(record.message)
            print("Learned:", record.toString())
            with open("log_server_" + str(self.pid), 'a') as f_log:
                f_log.write(record.toString() + "\n")

            print("sending msg to client")
            self.sendMessageToClients(record.toString())


    def sendLeaderFaulty(self):
        print("sendLeaderFaulty")
        self.sendMessageToServers("/leaderFaulty", self.view)


    def leaderFaulty_handler(self, addr, args, recievedMsg):
        if int(recievedMsg) >= self.view:
            print("Someone says my leader/ future leader is faulty.")
            self.shouldIBeLeader(int(recievedMsg))
        else:
            print("It's not my current leader. Maybe I already had dealt with this issue.")


    def sendMessageToServers(self, label, value, exceptMe=False):
        for i, s in enumerate(self.sendChannels):
            if i == self.pid and exceptMe:
                continue
            s.send_message(label, value)
            # time.sleep(0.5)


    def sendMessageToClients(self, message):
        for c in self.clientChannels:
            c.send_message("/processResponse", message)


# --------- ServerProcess


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=-1, help="The id of the process")
    parser.add_argument("--server_count", type=int, default=3, help="number of servers")
    parser.add_argument("--client_count", type=int, default=2, help="number of clients")
    args = parser.parse_args()

    server = ServerProcess(args.pid, args.server_count, args.client_count)
    server.start()

