#!/router/bin/python

try:
    # support import for Python 2
    import outer_packages
except ImportError:
    # support import for Python 3
    import client.outer_packages
from client_utils.jsonrpc_client import JsonRpcClient, BatchMessage
from client_utils.packet_builder import CTRexPktBuilder
import json
from common.trex_stats import *
from common.trex_streams import *
from collections import namedtuple
from common.text_opts import *

from trex_async_client import CTRexAsyncClient

RpcCmdData = namedtuple('RpcCmdData', ['method', 'params'])

class RpcResponseStatus(namedtuple('RpcResponseStatus', ['success', 'id', 'msg'])):
        __slots__ = ()
        def __str__(self):
            return "{id:^3} - {msg} ({stat})".format(id=self.id,
                                                  msg=self.msg,
                                                  stat="success" if self.success else "fail")

# simple class to represent complex return value
class RC:

    def __init__ (self, rc = None, data = None):
        self.rc_list = []

        if (rc != None) and (data != None):
            tuple_rc = namedtuple('RC', ['rc', 'data'])
            self.rc_list.append(tuple_rc(rc, data))

    def add (self, rc):
        self.rc_list += rc.rc_list

    def good (self):
        return all([x.rc for x in self.rc_list])

    def bad (self):
        return not self.good()

    def data (self):
        return all([x.data if x.rc else "" for x in self.rc_list])

    def err (self):
        return all([x.data if not x.rc else "" for x in self.rc_list])

    def annotate (self, desc):
        print format_text('\n{:<40}'.format(desc), 'bold'),

        if self.bad():
            # print all the errors
            for x in self.rc_list:
                if not x.rc:
                    print format_text("\n{0}".format(x.data), 'bold')

            print format_text("[FAILED]\n", 'red', 'bold')


        else:
            print format_text("[SUCCESS]\n", 'green', 'bold')


def RC_OK():
    return RC(True, "")
def RC_ERR (err):
    return RC(False, err)


# describes a single port
class Port:

    STATE_DOWN       = 0
    STATE_IDLE       = 1
    STATE_STREAMS    = 2
    STATE_TX         = 3
    STATE_PAUSE      = 4

    def __init__ (self, port_id, user, transmit):
        self.port_id = port_id
        self.state = self.STATE_IDLE
        self.handler = None
        self.transmit = transmit
        self.user = user

        self.streams = {}

    def err (self, msg):
        return RC_ERR("port {0} : {1}".format(self.port_id, msg))

    # take the port
    def acquire (self, force = False):
        params = {"port_id": self.port_id,
                  "user":    self.user,
                  "force":   force}

        command = RpcCmdData("acquire", params)
        rc = self.transmit(command.method, command.params)
        if rc.success:
            self.handler = rc.data
            return RC_OK()
        else:
            return RC_ERR(rc.data)


    # release the port
    def release (self):
        params = {"port_id": self.port_id,
                  "handler": self.handler}

        command = RpcCmdData("release", params)
        rc = self.transmit(command.method, command.params)
        if rc.success:
            self.handler = rc.data
            return RC_OK()
        else:
            return RC_ERR(rc.data)

    def is_acquired (self):
        return (self.handler != None)

    def is_active (self):
        return (self.state == self.STATE_TX ) or (self.state == self.STATE_PAUSE)

    def sync (self, sync_data):

        self.handler = sync_data['handler']

        if sync_data['state'] == "DOWN":
            self.state = self.STATE_DOWN
        elif sync_data['state'] == "IDLE":
            self.state = self.STATE_IDLE
        elif sync_data['state'] == "STREAMS":
            self.state = self.STATE_STREAMS
        elif sync_data['state'] == "TX":
            self.state = self.STATE_TX
        elif sync_data['state'] == "PAUSE":
            self.state = self.STATE_PAUSE
        else:
            raise Exception("port {0}: bad state received from server '{1}'".format(self.port_id, sync_data['state']))

        return RC_OK()
        

    # return TRUE if write commands
    def is_port_writeable (self):
        # operations on port can be done on state idle or state sreams
        return ((self.state == self.STATE_IDLE) or (self.state == self.STATE_STREAMS))

    # add stream to the port
    def add_stream (self, stream_id, stream_obj):

        if not self.is_port_writeable():
            return self.err("Please stop port before attempting to add streams")


        params = {"handler": self.handler,
                  "port_id": self.port_id,
                  "stream_id": stream_id,
                  "stream": stream_obj}

        rc, data = self.transmit("add_stream", params)
        if not rc:
            r = self.err(data)
            print r.good()

        # add the stream
        self.streams[stream_id] = stream_obj

        # the only valid state now
        self.state = self.STATE_STREAMS

        return RC_OK()

    # remove stream from port
    def remove_stream (self, stream_id):

        if not stream_id in self.streams:
            return self.err("stream {0} does not exists".format(stream_id))

        params = {"handler": self.handler,
                  "port_id": self.port_id,
                  "stream_id": stream_id}
                  

        rc, data = self.transmit("remove_stream", params)
        if not rc:
            return self.err(data)

        self.streams[stream_id] = None

        return RC_OK()

    # remove all the streams
    def remove_all_streams (self):

        params = {"handler": self.handler,
                  "port_id": self.port_id}

        rc, data = self.transmit("remove_all_streams", params)
        if not rc:
            return self.err(data)

        self.streams = {}

        return RC_OK()

    # start traffic
    def start (self, mul):
        if self.state == self.STATE_DOWN:
            return self.err("Unable to start traffic - port is down")

        if self.state == self.STATE_IDLE:
            return self.err("Unable to start traffic - no streams attached to port")

        if self.state == self.STATE_TX:
            return self.err("Unable to start traffic - port is already transmitting")

        params = {"handler": self.handler,
                  "port_id": self.port_id,
                  "mul": mul}

        rc, data = self.transmit("start_traffic", params)
        if not rc:
            return self.err(data)

        self.state = self.STATE_TX

        return RC_OK()

    # stop traffic
    # with force ignores the cached state and sends the command
    def stop (self, force = False):

        if (not force) and (self.state != self.STATE_TX) and (self.state != self.STATE_PAUSE):
            return self.err("port is not transmitting")

        params = {"handler": self.handler,
                  "port_id": self.port_id}

        rc, data = self.transmit("stop_traffic", params)
        if not rc:
            return self.err(data)

        # only valid state after stop
        self.state = self.STATE_STREAMS

        return RC_OK()



class CTRexStatelessClient(object):
    """docstring for CTRexStatelessClient"""

    def __init__(self, username, server="localhost", sync_port = 5050, async_port = 4500, virtual=False):
        super(CTRexStatelessClient, self).__init__()
        self.user = username
        self.comm_link = CTRexStatelessClient.CCommLink(server, sync_port, virtual)
        self.verbose = False
        self._conn_handler = {}
        self._active_ports = set()
        self._system_info = None
        self._server_version = None
        self.__err_log = None

        self._async_client = CTRexAsyncClient(async_port)

        self.connected = False

    ############# helper functions section ##############

    def validate_port_list(self, port_id):
        if isinstance(port_id, list) or isinstance(port_id, set):
            # check each item of the sequence
            return all([self._is_ports_valid(port)
                        for port in port_id])
        elif (isinstance(port_id, int)) and (port_id >= 0) and (port_id <= self.get_port_count()):
            return True
        else:
            return False

    # some preprocessing for port argument
    def __ports (self, port_id_list):

        # none means all
        if port_id_list == None:
            return range(0, self.get_port_count())

        # always list
        if isinstance(port_id_list, int):
            port_id_list = [port_id_list]

        if not isinstance(port_id_list, list):
             raise ValueError("bad port id list: {0}".format(port_id_list))

        for port_id in port_id_list:
            if not isinstance(port_id, int) or (port_id < 0) or (port_id > self.get_port_count()):
                raise ValueError("bad port id {0}".format(port_id))

        return port_id_list

    ############ boot up section ################

    # connection sequence
    def connect (self):

        self.connected = False

        # connect
        rc, data = self.comm_link.connect()
        if not rc:
            return RC_ERR(data)

        # cache system info
        rc, data = self.transmit("get_system_info")
        if not rc:
            return RC_ERR(data)

        self.system_info = data

        # cache supported cmds
        rc, data = self.transmit("get_supported_cmds")
        if not rc:
            return RC_ERR(data)

        self.supported_cmds = data

        # create ports
        self.ports = []
        for port_id in xrange(0, self.get_port_count()):
            self.ports.append(Port(port_id, self.user, self.transmit))

        # acquire all ports
        rc = self.acquire()
        if rc.bad():
            return rc

        rc = self.sync_with_server()
        if rc.bad():
            return rc

        self.connected = True

        return RC_OK()

    def is_connected (self):
        return self.connected


    def disconnect(self):
        self.connected = False
        self.comm_link.disconnect()


    ########### cached queries (no server traffic) ###########

    def get_supported_cmds(self):
        return self.supported_cmds

    def get_version(self):
        return self.server_version

    def get_system_info(self):
        return self.system_info

    def get_port_count(self):
        return self.system_info.get("port_count")

    def get_port_ids(self, as_str=False):
        port_ids = range(self.get_port_count())
        if as_str:
            return " ".join(str(p) for p in port_ids)
        else:
            return port_ids

    def get_stats_async (self):
        return self._async_client.get_stats()

    def get_connection_port (self):
        return self.comm_link.port

    def get_acquired_ports(self):
        return [port.port_id for port in self.ports if port.is_acquired()]


    def get_active_ports(self):
        return [port.port_id for port in self.ports if port.is_active()]

    def set_verbose(self, mode):
        self.comm_link.set_verbose(mode)
        self.verbose = mode

    ############# server actions ################

    # ping server
    def ping(self):
        rc, info = self.transmit("ping")
        return RC(rc, info)


    def sync_with_server(self, sync_streams=False):
        rc, data = self.transmit("sync_user", {"user": self.user, "sync_streams": sync_streams})
        if not rc:
            return RC_ERR(data)

        for port_info in data:

            rc = self.ports[port_info['port_id']].sync(port_info)
            if rc.bad():
                return rc

        return RC_OK()



    ########## port commands ##############
    # acquire ports, if port_list is none - get all
    def acquire (self, port_id_list = None, force = False):
        port_id_list = self.__ports(port_id_list)

        rc = RC()

        for port_id in port_id_list:
            rc.add(self.ports[port_id].acquire(force))
     
        return rc
    
    # release ports
    def release (self, port_id_list = None):
        port_id_list = self.__ports(port_id_list)

        rc = RC()

        for port_id in port_id_list:
            rc.add(self.ports[port_id].release(force))
        
        return rc

 
    def add_stream(self, stream_id, stream_obj, port_id_list = None):

        port_id_list = self.__ports(port_id_list)

        rc = RC()

        for port_id in port_id_list:
            rc.add(self.ports[port_id].add_stream(stream_id, stream_obj))
        
        return rc

      
    def add_stream_pack(self, stream_pack_list, port_id_list = None):

        port_id_list = self.__ports(port_id_list)

        rc = RC()

        for stream_pack in stream_pack_list:
            rc.add(self.add_stream(stream_pack.stream_id, stream_pack.stream, port_id_list))
        
        return rc



    def remove_stream(self, stream_id, port_id_list = None):
        port_id_list = self.__ports(port_id_list)

        rc = RC()

        for port_id in port_id_list:
            rc.add(self.ports[port_id].remove_stream(stream_id))
        
        return rc



    def remove_all_streams(self, port_id_list = None):
        port_id_list = self.__ports(port_id_list)

        rc = RC()

        for port_id in port_id_list:
            rc.add(self.ports[port_id].remove_all_streams())
        
        return rc

    
    def get_stream(self, stream_id, port_id, get_pkt = False):

        return self.ports[port_id].get_stream(stream_id)


    def get_all_streams(self, port_id, get_pkt = False):

        return self.ports[port_id].get_all_streams()


    def get_stream_id_list(self, port_id):

        return self.ports[port_id].get_stream_id_list()


    def start_traffic (self, multiplier, port_id_list = None):

        port_id_list = self.__ports(port_id_list)

        rc = RC()

        for port_id in port_id_list:
            rc.add(self.ports[port_id].start(multiplier))
        
        return rc



    def stop_traffic (self, port_id_list = None, force = False):

        port_id_list = self.__ports(port_id_list)
        rc = RC()

        for port_id in port_id_list:
            rc.add(self.ports[port_id].stop(force))
        
        return rc


    def get_port_stats(self, port_id=None):
        pass

    def get_stream_stats(self, port_id=None):
        pass


    def transmit(self, method_name, params={}):
        return self.comm_link.transmit(method_name, params)



    ######################### Console (high level) API #########################

    # reset
    # acquire, stop, remove streams and clear stats
    #
    #
    def cmd_reset (self):


        # sync with the server
        rc = self.sync_with_server()
        rc.annotate("Syncing with the server:")
        if rc.bad():
            return rc

        rc = self.acquire(force = True)
        rc.annotate("Force acquiring all ports:")
        if rc.bad():
            return rc


        # force stop all ports
        rc = self.stop_traffic(self.get_port_ids(), True)
        rc.annotate("Stop traffic on all ports:")
        if rc.bad():
            return rc


        # remove all streams
        rc = self.remove_all_streams(self.get_port_ids())
        rc.annotate("Removing all streams from all ports:")
        if rc.bad():
            return rc

        # TODO: clear stats
        return RC_OK()
        

    # stop cmd
    def cmd_stop (self, port_id_list):

        # find the relveant ports
        active_ports = list(set(self.get_active_ports()).intersection(port_id_list))

        if not active_ports:
            print format_text("No active traffic on porvided ports", 'bold')
            return True

        rc = self.stop_traffic(active_ports)
        rc.annotate("Stopping traffic on port(s) {0}:".format(port_id_list))
        if rc.bad():
            return False

        return True

    # start cmd
    def cmd_start (self, port_id_list, stream_list, mult, force):

        active_ports = list(set(self.get_active_ports()).intersection(port_id_list))

        if active_ports:
            if not force:
                print format_text("Port(s) {0} are active - please stop them or add '--force'".format(active_ports), 'bold')
                return False
            else:
                rc = self.cmd_stop(active_ports)
                if not rc:
                    return False


        rc = self.remove_all_streams(port_id_list)
        rc.annotate("Removing all streams from ports {0}:".format(port_id_list))
        if rc.bad():
            return False


        rc = self.add_stream_pack(stream_list.compiled, port_id_list)
        rc.annotate("Attaching streams to port {0}:".format(port_id_list))
        if rc.bad():
            return False


        # finally, start the traffic
        rc = self.start_traffic(mult, port_id_list)
        rc.annotate("Starting traffic on ports {0}:".format(port_id_list))
        if rc.bad():
            return False

        return True

   
    # ------ private classes ------ #
    class CCommLink(object):
        """describes the connectivity of the stateless client method"""
        def __init__(self, server="localhost", port=5050, virtual=False):
            super(CTRexStatelessClient.CCommLink, self).__init__()
            self.virtual = virtual
            self.server = server
            self.port = port
            self.verbose = False
            self.rpc_link = JsonRpcClient(self.server, self.port)

        @property
        def is_connected(self):
            if not self.virtual:
                return self.rpc_link.connected
            else:
                return True

        def set_verbose(self, mode):
            self.verbose = mode
            return self.rpc_link.set_verbose(mode)

        def connect(self):
            if not self.virtual:
                return self.rpc_link.connect()

        def disconnect(self):
            if not self.virtual:
                return self.rpc_link.disconnect()

        def transmit(self, method_name, params={}):
            if self.virtual:
                self._prompt_virtual_tx_msg()
                _, msg = self.rpc_link.create_jsonrpc_v2(method_name, params)
                print msg
                return
            else:
                return self.rpc_link.invoke_rpc_method(method_name, params)

        def transmit_batch(self, batch_list):
            if self.virtual:
                self._prompt_virtual_tx_msg()
                print [msg
                       for _, msg in [self.rpc_link.create_jsonrpc_v2(command.method, command.params)
                                      for command in batch_list]]
            else:
                batch = self.rpc_link.create_batch()
                for command in batch_list:
                    batch.add(command.method, command.params)
                # invoke the batch
                return batch.invoke()

        def _prompt_virtual_tx_msg(self):
            print "Transmitting virtually over tcp://{server}:{port}".format(server=self.server,
                                                                             port=self.port)


if __name__ == "__main__":
    pass
