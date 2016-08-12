# -*- coding: utf-8 -*-
"""

@author: Lukas Sandström
"""

from RSSscpi.gen import SCPINodeBase
from RSSscpi.gen.SCPI_gen_support import SCPIResponse

import visa

from time import ctime
import timeit, time
import threading, traceback

import Queue  # Use Queue.Queue, not multiprocessing.Queue, to avoid unnecessary pickling
from collections import OrderedDict
import itertools
import re, string


class LimitedCapacityDict(OrderedDict):
    def __init__(self, max_len=None):
        self._max_len = max_len
        super(LimitedCapacityDict, self).__init__()

    @property
    def max_len(self):
        return self._max_len

    @max_len.setter
    def max_len(self, value):
        self._max_len = value
        self._check_len()

    def _check_len(self):
        if self._max_len and self._max_len < len(self):
            for n, key in itertools.izip(range(len(self) - self._max_len), self):
                del self[key]  # Deleting while iterating is valid for OrderedCollections

    def __setitem__(self, key, value, dict_setitem=dict.__setitem__):
        if key in self:  # Move the element to the end, if already inserted
            del self[key]
        super(LimitedCapacityDict, self).__setitem__(key, value, dict_setitem)
        self._check_len()


class VISAEvent(object):
    def __init__(self, duration, stb, esr):
        self.duration = duration
        self.stb = stb
        self.esr = esr


class SCPICmdFormatter(string.Formatter):
    def __init__(self):
        self.last_number = 0
        super(SCPICmdFormatter, self).__init__()

    def vformat(self, format_string, args, kwargs):
        ret = super(SCPICmdFormatter, self).vformat(format_string, args, kwargs)
        self.last_number = 0
        return ret

    def get_value(self, key, args, kwargs):
        if key == '':
            key = self.last_number
            self.last_number += 1
        return super(SCPICmdFormatter, self).get_value(key, args, kwargs)

    def format_field(self, value, format_spec):
        if not format_spec:  # check for empty string
            pass
        elif format_spec[-1] == "*":  # code for list unpack
            return ", ".join(map(lambda x: self.format_field(x, format_spec[:-1]), value))
        elif format_spec[-1] == "q":  # code for single quoted string
            format_spec = format_spec[:-1] + "s"
            return "'" + self.format_field(value, format_spec) + "'"
        elif format_spec[-1] == "s":  # coerce everything with str() for convenience
            value = str(value)

        return super(SCPICmdFormatter, self).format_field(value, format_spec)


# http://stackoverflow.com/questions/16244923/how-to-make-a-custom-exception-class-with-multiple-init-args-pickleable
# http://bugs.python.org/issue1692335
class InstrumentError(BaseException):
    def __init__(self, err_no=0, err_str="", stack=None):
        super(InstrumentError, self).__init__()
        self.err_no = err_no
        self.err_str = err_str
        self.stack = stack

    def __str__(self):
        ret = "SCPI error: {:d},{}\n".format(self.err_no, self.err_str)
        if self.stack:
            ret += "".join(traceback.format_list(self.stack))
        return ret


class Instrument(SCPINodeBase):
    _cmd = ""

    Error = InstrumentError

    def __init__(self, visa_res):
        """
        :type visa_res: pyvisa.resources.tcpip.TCPIPInstrument
        :param visa_res:
        """

        super(Instrument, self).__init__(None)
        self._visa_res = visa_res
        self.command_cnt = 0
        """
        The number of writes/queries performed in total
        """
        self.logger = None
        """
        A file-like object used for logging VISA operations
        """
        self._service_request_callback_handle = None
        self.last_cmd_time = 0

        self._visa_lock = threading.Lock()
        self._in_callback = threading.Lock()
        """Locks used to synchronize VISA operations."""

        self.event_queue = Queue.Queue()
        """
        Events generated by the VISA library are queued here.
        """

        self.error_queue = Queue.Queue()
        """
        Errors fetched from the instrument are queued here.
        """

        self.exception_on_error = True
        self._cmd_debug = LimitedCapacityDict(max_len=500)
        """
        _call_visa(...) stores the stack trace here for each command.
        """

    def init(self):
        """
        Setup the Service Request handling and turn on event reporting in the instrument.
        """
        # Clear the status register
        # Enable Operation Complete reporting with *OPC
        # Generate a Service Request when the event status register changes, or the error queue is non-empty
        self._write("*CLS;*ESE 127;*SRE 36")

        self._service_request_callback_handle = self._visa_res.install_handler(
            visa.constants.EventType.service_request, self._service_request_handler, 0)
        self._visa_res.enable_event(visa.constants.EventType.service_request, visa.constants.VI_HNDLR)

    # noinspection PyUnusedLocal
    def _service_request_handler(self, session, event_type, context, user_handle):
        """
        This function is invoked as a callback from the VISA library.

        :param session:
        :param event_type:
        :param context:
        :param user_handle:
        :return:
        """
        duration = timeit.default_timer() - self.last_cmd_time
        #print "Handling service request"
        with self._visa_lock:
            with self._in_callback:
                stb = self._visa_res.read_stb()  # Read out the SRQ status byte
                if stb & 32:
                    esr = self._call_visa(self._visa_res.query, "*ESR?")  # read and reset the event status register
                else:
                    esr = 0
                self.log("VISA event: STB: {:08b}, ESR: {:08b}, duration {:.2f} ms".format(stb, int(esr), duration*1e3))
                self.event_queue.put_nowait(VISAEvent(duration, stb, esr))

                if stb & (1 << 2):  # Error queue not empty bit
                    self._get_error_queue()
        return visa.constants.VI_SUCCESS

    def _get_error_queue(self):
        err = self._query("SYSTem:ERRor:ALL?")
        cnt = 0
        for r in re.finditer(r'(-?\d+),"(.*?([A-Z]{3}.*?)?(?:\n.*?)?)"', str(err)):
            cnt += 1
            x = (int(r.group(1)), r.group(2).replace("\n", " "))
            bad_cmd = r.group(3)
            tb = self._cmd_debug.get(bad_cmd)
            if not tb:
                print "No stack for", str(err), r.groups()
            self.error_queue.put_nowait(InstrumentError(x[0], x[1], tb))
            self.log("%d %s" % x)
        if not cnt:
            self.error_queue.put_nowait(InstrumentError(-1, str(err), None))

    def log(self, line):
        if not self.logger:
            return
        self.logger.write(ctime())
        self.logger.write("\t")
        self.logger.write(line)
        self.logger.write("\n")

    def check_error_queue(self):
        if not self.error_queue.empty() and self.exception_on_error and self._in_callback.acquire(False):
            # http://blog.bstpierre.org/python-exception-handling-cleanup-and-reraise
            self._in_callback.release()  # Don't raise the exception from the VISA library callback thread
            # TODO: raise with original stack trace instead?
            raise self.error_queue.get(block=False)

    def _call_visa(self, func, arg):
        self.check_error_queue()

        self.command_cnt += 1
        self._cmd_debug[arg] = traceback.extract_stack()[:-2]  # Store the current stack for later debugging
        start = timeit.default_timer()
        err = None
        try:
            ret = func(arg)
        except visa.Error, e:
            err = "Resource error: " + str(e) + ", " + arg
            print err
            raise
        finally:
            self.last_cmd_time = timeit.default_timer()
            elapsed = (self.last_cmd_time - start) * 1e3
            self.log("%.2f ms \t %s" % (elapsed, arg))
            if err:
                self.log(err)
        return ret

    @staticmethod
    def _build_arg_str(cmd, args, kwargs):
        fmt = kwargs.get("fmt")
        if not fmt:
            args = (args, )
            if kwargs.get("quote") or "'string'" in cmd.args:
                fmt = "{:q*}"
            else:
                fmt = "{:s*}"
        return SCPICmdFormatter().vformat(fmt, args, kwargs)

    def _write(self, cmd_str):
        self._call_visa(self._visa_res.write, cmd_str)

    def write(self, cmd, *args, **kwargs):
        """
        Send a string to the instrument, without reading a response.

        :param cmd: The SCPI command
        :type cmd: SCPINodeBase
        :param args: Any number of arguments for the command, will be converted with str()
        :rtype: None
        """
        x = cmd.build_cmd() + " " + self._build_arg_str(cmd, args, kwargs)
        with self._visa_lock:
            self._call_visa(self._visa_res.write, x)

    def _query(self, cmd_str):
        return SCPIResponse(self._call_visa(self._visa_res.query, cmd_str))

    def query(self, cmd, *args, **kwargs):
        """
        Execute a SCPI query

        :param cmd: The SCPI command
        :type cmd: SCPINodeBase
        :param args: A list of arguments for the command, will be converted with str() and joined with ", "
        :return: The response from the pyvisa query
        :rtype: SCPIResponse
        """
        # TODO: add function to read back result later
        x = cmd.build_cmd() + "? " + self._build_arg_str(cmd, args, kwargs)
        try:
            with self._visa_lock:
                return SCPIResponse(self._call_visa(self._visa_res.query, x))
        except visa.VisaIOError, e:
            if e.error_code == visa.constants.VI_ERROR_TMO:  # timeout
                if self.exception_on_error:
                    try:
                        raise self.error_queue.get(timeout=1)  # Wait for up to 1 s for the error callback to be processed
                    except Queue.Empty:
                        pass
            raise e

    def preset(self):
        self.RST.w()
