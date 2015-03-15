# Copyright (C) 2015 Sebastian Pipping <sebastian@pipping.org>
# Licensed under AGPL v3 or later

from __future__ import print_function

import re
import sys

from colorama import Fore, Style

from image_bootstrap.metadata import GITHUB_HOME_URL, VERSION_STR, RELEASE_DATE_STR


_NEEDS_ESCAPING = re.compile('([!`"\'$ \\\\{}()?*&<>;])')

BANNER = """\
     _                          __             __      __               
    (_)_ _  ___ ____ ____  ___ / /  ___  ___  / /____ / /________ ____  
   / /  ' \/ _ `/ _ `/ -_)/__// _ \/ _ \/ _ \/ __(_-</ __/ __/ _ `/ _ \ 
  /_/_/_/_/\_,_/\_, /\__/    /_.__/\___/\___/\__/___/\__/_/  \_,_/ .__/ 
               /___/                    %(3456789_123456789_)s  /_/     

Software libre licensed under AGPL v3 or later.
Brought to you by Sebastian Pipping <sebastian@pipping.org>.
Please report bugs at %(github_home)s.  Thank you!\
""" % {
    '3456789_123456789_': '%*s' \
        % (len('%(3456789_123456789_)s'),
        'v%s :: %s' % (VERSION_STR, RELEASE_DATE_STR)),
    'github_home': GITHUB_HOME_URL,
}


class Messenger(object):
    def __init__(self, verbose, colorize):
        self._verbose = verbose
        self._colorize = colorize

    def colorize(self, text, fore=None, style=None):
        if not self._colorize:
            return text

        chunks = []
        if fore:
            chunks.append(fore)
        if style:
            chunks.append(style)
        chunks.append(text)
        if fore or style:
            chunks.append(Style.RESET_ALL)
        return ''.join(chunks)

    def banner(self):
        if not self._verbose:
            return

        print(BANNER)
        print()

    def escape_shell(self, text):
        return _NEEDS_ESCAPING.sub('\\\\\\1', text)

    def announce_command(self, argv):
        if not self._verbose:
            return
        text = '# %s' % ' '.join((self.escape_shell(e) for e in argv))

        sys.stderr.flush()
        print(self.colorize(text, Fore.CYAN))
        sys.stdout.flush()

    def info(self, text):
        if not self._verbose:
            return
        print(self.colorize(text, Fore.GREEN))

    def error(self, text):
        print(self.colorize('Error: ' + text, Fore.RED, Style.BRIGHT), file=sys.stderr)

    def info_gap(self):
        if not self._verbose:
            return
        print()

    def encourage_bug_reports(self):
        print('If this looks like a bug to you, please file a report at %s.  Thank you!' \
                % GITHUB_HOME_URL, file=sys.stderr)
