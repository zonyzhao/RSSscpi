# -*- coding: utf-8 -*-
"""


@author: Lukas Sandström
"""

try:
    # For Python 3.0 and later
    from urllib.request import urlopen
except ImportError:
    # Fall back to Python 2's urllib2
    from urllib2 import urlopen, HTTPError
    from urllib import urlretrieve

from bs4 import BeautifulSoup
import re

class CmdNode(dict):
    leaf = "_leaf"  # Constant indicating that there are no sub-nodes.
    
    def __init__(self, is_countable=False):
        super(CmdNode, self).__init__()
        self.units = []
        self.args = []
        self.has_query = False # query command supported
        self.has_set = False # set command supported
        self.is_countable = is_countable # An integer can be appended to the node name

    def add_cmd(self, node_list, arg, unit, query):
        cmd = node_list.pop(0)
        is_countable = False
        if cmd[-1].isdigit():
            cmd = cmd[:-1]
            is_countable = True
        node = self.setdefault(cmd, CmdNode(is_countable))

        if len(node_list):
            return node.add_cmd(node_list, arg, unit, query)

        arg and arg not in node.args and node.args.append(arg)
        unit and node.units.append(unit)
        node.has_query |= query
        node.has_set |= not query
   
class CmdListParser(object):
    """Parses a command set file obtained from RS GPIB Explorer (ICEWIN32)"""
    def __init__(self, filename):
        self.filename = filename   
        self.cmd_tree = CmdNode()
        self._parse()
    
    def _parse(self):
        with open(self.filename) as fd:
            for line in fd:
                if not line or line[0] == '/' or line.isspace():
                    continue
                try:
                    self._add_cmd(*line.split())
                except:
                    print "Error on line: '" + line + "'"
                    raise
            
    def _add_cmd(self, cmd, arg=None, unit=None):
        query = False
        if cmd[-1] == '?':
            query = True
            cmd = cmd[:-1]
        c = cmd.split(':')
        
        self.cmd_tree.add_cmd(c, arg, unit, query)


class Webhelp(object):
    def get_help_url(self, cmd):
        return None


class RohdeZVAWebhelp(Webhelp):
    def __init__(self):
        html = open("SCPI_cmd_lists/ZVA_help_index.htm")

        self.common_commands = "https://www.rohde-schwarz.com/webhelp/webhelp_zva_5/scpi_reference/common_commands.htm"
        self.interface_messages = "https://www.rohde-schwarz.com/webhelp/webhelp_zva_5/annexes/hw_interfaces/gpib_bus_interface.htm#Interface_Messages"
        self.soup = BeautifulSoup(html)
        self.urls = dict()
        self.load_urls()

    def load_urls(self):
        """
        Load help URLs from the Index page of the online manual, saved locally.
        :return:
        """
        for u in self.soup("a"):
            try:
                url = u['onclick'].split("'")[5]
            except IndexError:
                continue
            cmd = u['alt'].split()[0]  # TODO: The next item in the list can contain "(deprecated)". Use this info?
            cmd_key = str(cmd).translate(None, "[]?")
            cmd_key = re.sub(r"([^:])<\w+?>", r"\1", cmd_key)
            self.urls[cmd_key] = (cmd, url)

    def get_help_url(self, cmd):
        if cmd[0][0] == "*":
            return self.common_commands
        if cmd[0][0] == "@":
            return self.interface_messages
        try:
            return self.urls[":".join(cmd)][1]
        except KeyError:
            return None


class RohdeZNBWebhelp(Webhelp):
    def __init__(self):

        self._base_url = "http://www.rohde-schwarz.com/webhelp/znb_znbt_webhelp_en_{1}{0}"
        self._help_rev = 6

        self.toc_file = "SCPI_cmd_lists/ZNB_webhelp_toc.xml"
        self.cmd_list_file = "SCPI_cmd_lists/ZNB_webhelp_command_list.htm"

        self._urls = dict()
        self._common_commands = None  # *CLS, *OPC, etc.
        self._interface_messages = None  # VXI-11 Interface messages, @LOC, @DCL, etc.

        self.download_cmd_list()
        self.load_urls()

    def download_cmd_list(self):
        doc_rev = None

        for rev in range(self._help_rev, self._help_rev + 10):  # Search after newer revisions of the manual
            try:
                url = self._base_url.format("/Data/Toc.xml", rev)
                toc = urlopen(url)
            except HTTPError:
                continue
            print "ZNB webhelp is at revision", rev, ", started search at rev", self._help_rev
            self._help_rev = rev
            break
        else:
            raise RuntimeError("No valid ZNB web help URL found")

        toc = BeautifulSoup(toc)
        with open(self.toc_file, "w") as f:
            f.write(toc.prettify("utf-8"))

        cmd_list_url = toc.find(title="List of Commands")["link"]
        url = self._base_url.format(cmd_list_url, self._help_rev)
        urlretrieve(url, "SCPI_cmd_lists/ZNB_webhelp_command_list.htm")

        with open("SCPI_cmd_lists/ZNB_webhelp_rev.txt", "w") as u:
            u.write("%i" % self._help_rev)

    def load_urls(self):
        """
        Load help URLs from the SCPI command list.
        https://www.rohde-schwarz.com/webhelp/znb_znbt_webhelp_en_5/Content/c0e6efab4c3f4280.htm
        :return:
        """
        toc_soup = BeautifulSoup(open(self.toc_file))

        common = toc_soup.find(title="Command Reference").find(title="Common Commands")["link"]
        self._common_commands = self._base_url.format(common, self._help_rev)

        if_msg = toc_soup.find(title="VXI-11 Interface Messages")["link"]
        self._interface_messages = self._base_url.format(if_msg, self._help_rev)

        cmd_soup = BeautifulSoup(open(self.cmd_list_file))
        d = cmd_soup.find("div", class_="block")
        for u in d("a"):
            cmd = u.string
            url = u['href']
            cmd_key = str(cmd).translate(None, "[]?")
            cmd_key = re.sub(r"([^:])<\w+?>", r"\1", cmd_key)
            self._urls[cmd_key] = (cmd, url)

    def get_help_url(self, cmd):
        if cmd[0][0] == "*":
            return self._common_commands
        if cmd[0][0] == "@":
            return self._interface_messages
        try:
            return self._base_url.format("/Content/" + self._urls[":".join(cmd)][1], self._help_rev)
        except KeyError:
            return None


class ClassCodeGen(object):
    """
    Generates a Python module with a SCPI command class structure for the instrument.
    """
    def __init__(self, class_name, cmd_tree, io_obj, source, webhelp=Webhelp()):
        """

        :param class_name: The name of the Python module to be generated.
        :param cmd_tree: A SCPI command tree structure, obtained from a CmdListParser.
        :param io_obj: An IO object to output the module to.
        :param source: String comment indicating the source file of the SCPI command structure.
        """
        self.cmd_tree = cmd_tree
        self._output = io_obj
        self._indent = 0
        self.class_name = class_name
        self.source = source
        self.hlp = webhelp

        self._cmd_cnt = 0
        self._cmd_help_cnd = 0
    
    def _preamble(self):
        import time
        self._out("# -*- coding: utf-8 -*-")
        self._out("# Generated from " + self.source + " on " + time.strftime("%Y-%m-%d %H:%M"))
        self._out("from SCPI_gen_support import Instrument, SCPINode, SCPINodeN, SCPIQuery, SCPISet")
        self._out("class " + self.class_name + "(Instrument):")
        self._indent += 1
        
    def gen(self):
        self._preamble()
        self._gen(self.cmd_tree, [])
        self._out("# END OF " + self.class_name)
        c1 = self._cmd_cnt
        c2 = self._cmd_help_cnd
        c3 = str(int(float(c2)/c1 * 100))
        print self.class_name + ":", c1, "commands,", c2, "have a help URL (" + c3 + "%)."

    def _out(self, str_):
        self._output.write(" "*(self._indent*4) + str_ + "\n")

    def _make_docstr(self, cmd):
        self._cmd_cnt += 1
        self._out('"""')
        cmd_str = ":".join(cmd)
        url = self.hlp.get_help_url(cmd)
        if url is not None:
            self._out("`"+cmd_str)
            self._out("<" + url + ">`_")
            self._cmd_help_cnd += 1
        else:
            self._out(cmd_str)
        self._out('"""')

    def _gen(self, cmd_tree, parents):
        for cmd_str in sorted(cmd_tree):
            cmd = cmd_tree[cmd_str]

            base_class = "SCPINode"
            if cmd.is_countable:
                base_class = "SCPINodeN"
            if cmd.has_query:
                base_class += ", SCPIQuery"
            if cmd.has_set:
                base_class += ", SCPISet"

            name = cmd_str
            if name[0] == '*' or name[0] == '@':
                name = name[1:]            
            self._out("class " + name+"(" + base_class + "):")
            self._indent += 1
            self._make_docstr(parents + [cmd_str])
            self._out('_cmd = "' + cmd_str + '"')
            self._out("")
            self._gen(cmd, parents + [cmd_str])
            self._indent -= 1


def generate_SCPI_class(input_file, module_name, webhelp=Webhelp()):
    """

    :param input_file: filename of the command list from GPIB Explorer
    :param module_name: Name of the module to be generated
    :param webhelp: Webhelp subclass instance, generating help URLs
    :return: None
    """
    parser = CmdListParser('SCPI_cmd_lists/' + input_file)

    path = "RSSscpi/gen/" + module_name + ".py"
    fd = open(path, 'wb')
    g = ClassCodeGen(module_name, parser.cmd_tree, fd, source=input_file, webhelp=webhelp)
    g.gen()
    fd.close()

    import importlib
    importlib.import_module("RSSscpi.gen." + module_name)  # Test that the module can be loaded
    print "Generated " + path


if __name__ == '__main__':
    import os
    os.chdir("..")
    generate_SCPI_class("ZVA_commands_3_60.inp", "ZVA_gen", RohdeZVAWebhelp())
    generate_SCPI_class("ZNB_commands_2_70.inp", "ZNB_gen", RohdeZNBWebhelp())
    print "All good :)"

