# Copyright (C) 2015 Sebastian Pipping <sebastian@pipping.org>
# Licensed under AGPL v3 or later

from __future__ import print_function

import datetime
import errno
import os
import re
import shutil
import subprocess
import tempfile

from pkg_resources import resource_filename
from tarfile import TarFile

import directory_bootstrap.resources.gentoo as resources
from directory_bootstrap.distros.base import DirectoryBootstrapper, date_argparse_type
from directory_bootstrap.shared.commands import COMMAND_GPG, COMMAND_MD5SUM, \
        COMMAND_SHA512SUM, COMMAND_TAR, COMMAND_UNXZ


_DEFAULT_MIRROR = 'http://distfiles.gentoo.org/'
_GPG_DISPLAY_KEY_FORMAT = '0xlong'

_year = '([2-9][0-9]{3})'
_month = '(0[1-9]|1[12])'
_day = '(0[1-9]|[12][0-9]|3[01])'

_stage3_folder_date_matcher = re.compile('^%s%s%s' % (_year, _month, _day))
_snapshot_date_matcher = re.compile('%s%s%s' % (_year, _month, _day))


class _ChecksumVerifiationFailed(Exception):
    def __init__(self, algorithm, filename):
        super(_ChecksumVerifiationFailed, self).__init__(
                'File "%s" failed %s verification' \
                % (filename, algorithm))


class _NotFreshEnoughException(Exception):
    def __init__(self, (year, month, day), max_age_days):
        super(_NotFreshEnoughException, self).__init__(
                '%04d-%02d-%02d was more than %d days ago, rejecting as too old' \
                % (year, month, day, max_age_days))


class GentooBootstrapper(DirectoryBootstrapper):
    DISTRO_KEY = 'gentoo'

    def __init__(self, messenger, executor, abs_target_dir, abs_cache_dir,
                architecture, mirror_url, max_age_days,
                stage3_date_triple_or_none, repository_date_triple_or_none,
                abs_resolv_conf):
        self._messenger = messenger
        self._executor = executor
        self._abs_target_dir = abs_target_dir
        self._abs_cache_dir = abs_cache_dir
        self._architecture = architecture
        self._mirror_base_url = mirror_url.rstrip('/')
        self._max_age_days = max_age_days
        self._stage3_date_triple_or_none = stage3_date_triple_or_none
        self._repository_date_triple_or_none = repository_date_triple_or_none
        self._abs_resolv_conf = abs_resolv_conf

        self._gpg_supports_no_autostart = None

    @staticmethod
    def get_commands_to_check_for():
        return DirectoryBootstrapper.get_commands_to_check_for() + [
                COMMAND_GPG,
                COMMAND_MD5SUM,
                COMMAND_SHA512SUM,
                COMMAND_TAR,
                COMMAND_UNXZ,
                ]

    def _get_stage3_listing_url(self):
        return '%s/releases/%s/autobuilds/' % (self._mirror_base_url, self._architecture)

    def _get_portage_snapshot_listing_url(self):
        return '%s/releases/snapshots/current/' % self._mirror_base_url

    def _find_latest_stage3_date(self, stage3_listing):
        return self.extract_latest_date(stage3_listing, _stage3_folder_date_matcher)

    def _find_latest_snapshot_date(self, snapshot_listing):
        return self.extract_latest_date(snapshot_listing, _snapshot_date_matcher)

    def _download_stage3(self, stage3_date_str):
        res = [None, None]
        for target_index, basename in (
                (1, 'stage3-amd64-%s.tar.bz2.DIGESTS.asc' % stage3_date_str),
                (0, 'stage3-amd64-%s.tar.bz2' % stage3_date_str),
                ):
            filename = os.path.join(self._abs_cache_dir, basename)
            url = '%s/releases/%s/autobuilds/%s/%s' \
                    % (self._mirror_base_url, self._architecture, stage3_date_str, basename)
            self.download_url_to_file(url, filename)

            assert res[target_index] is None
            res[target_index] = filename

        return res

    def _download_snapshot(self, snapshot_date_str):
        res = [None, None, None, None]
        for target_index, basename in (
                (1, 'portage-%s.tar.xz.gpgsig' % snapshot_date_str),
                (2, 'portage-%s.tar.xz.md5sum' % snapshot_date_str),
                (3, 'portage-%s.tar.xz.umd5sum' % snapshot_date_str),
                (0, 'portage-%s.tar.xz' % snapshot_date_str),
                ):
            filename = os.path.join(self._abs_cache_dir, basename)
            url = '%s/releases/snapshots/current/%s' \
                    % (self._mirror_base_url, basename)
            self.download_url_to_file(url, filename)

            assert res[target_index] is None
            res[target_index] = filename

        return res

    def _verify_detachted_gpg_signature(self, testee_file, signature_file):
        raise NotImplementedError()

    def _verify_sha512_sum(self, testee_file, digests_file):
        self._messenger.info('Verifying SHA512 checksum of file "%s"...' \
                % testee_file)

        expected_sha512sum = None
        testee_file_basename = os.path.basename(testee_file)
        with open(digests_file, 'r') as f:
            upcoming_sha512 = False
            for l in f:
                line = l.rstrip()
                if upcoming_sha512:
                    sha512, basename = line.split('  ')
                    if basename == testee_file_basename:
                        if expected_sha512sum is None:
                            expected_sha512sum = sha512
                        else:
                            raise ValueError('File "%s" mentions "%s" multiple times' \
                    % (digests_file, testee_file_basename))

                upcoming_sha512 = line == '# SHA512 HASH'

        if expected_sha512sum is None:
            raise ValueError('File "%s" does not mention "%s"' \
                    % (digests_file, testee_file_basename))

        expected_sha512sum_output = '%s  %s\n' % (expected_sha512sum, testee_file)
        sha512sum_output = self._executor.check_output([
                COMMAND_SHA512SUM,
                testee_file,
                ])

        if sha512sum_output != expected_sha512sum_output:
            raise _ChecksumVerifiationFailed('SHA512', testee_file)

    def _verify_md5_sum(self, snapshot_tarball, snapshot_md5sum):
        self._messenger.info('Verifying MD5 checksum of file "%s"...' \
                % snapshot_tarball)

        needle = os.path.basename(snapshot_tarball) + '\n'
        with open(snapshot_md5sum, 'r') as f:
            if f.read().count(needle) != 1:
                raise ValueError('File "%s" does not mention "%s" exactly once' \
                        % (snapshot_md5sum, snapshot_tarball))

        cwd = os.path.dirname(snapshot_md5sum)
        self._executor.check_call([
                COMMAND_MD5SUM,
                '--strict',
                '--check',
                snapshot_md5sum,
                ], cwd=cwd)

    def _uncompress_tarball(self, tarball_filename):
        extension = '.xz'

        if not tarball_filename.endswith(extension):
            raise ValueError('Filename "%s" does not end with "%s"' % (tarball_filename, extension))

        uncompressed_tarball_filename = tarball_filename[:-len(extension)]

        if os.path.exists(uncompressed_tarball_filename):
            self._messenger.info('Re-using cache file "%s".' % uncompressed_tarball_filename)
        else:
            self._messenger.info('Uncompressing file "%s"...' % tarball_filename)
            self._executor.check_call([
                    COMMAND_UNXZ,
                    '--keep',
                    tarball_filename,
                    ])

            if not os.path.exists(uncompressed_tarball_filename):
                raise OSError(errno.ENOENT, 'File "%s" does not exists' % uncompressed_tarball_filename)

        return uncompressed_tarball_filename

    def _extract_tarball(self, tarball_filename, abs_target_root):
        self._messenger.info('Extracting file "%s" to "%s"...' % (tarball_filename, abs_target_root))
        self._executor.check_call([
                COMMAND_TAR,
                'xpf',
                tarball_filename,
            ], cwd=abs_target_root)

    def _require_fresh_enough(self, (year, month, day)):
        date_to_check = datetime.date(year, month, day)
        today = datetime.date.today()
        if (today - date_to_check).days > self._max_age_days:
            raise _NotFreshEnoughException((year, month, day), self._max_age_days)

    def _parse_stage3_listing_date(self, stage3_date_str):
        m = _stage3_folder_date_matcher.match(stage3_date_str)
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    def _parse_snapshot_listing_date(self, snapshot_date_str):
        m = _snapshot_date_matcher.match(snapshot_date_str)
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    def _get_gpg_argv_start(self, abs_gpg_home_dir):
        assert self._gpg_supports_no_autostart is not None

        res = [
                COMMAND_GPG,
                '--home', abs_gpg_home_dir,
                '--keyid-format', _GPG_DISPLAY_KEY_FORMAT,
                '--batch',
            ]

        if self._gpg_supports_no_autostart:
            res += [
                '--no-autostart',
                ]

        return res

    def _check_gpg_for_no_autostart_support(self, abs_gpg_home_dir):
        self._messenger.info('Checking if GnuPG understands the --no-autostart option...')
        cmd_prefix = [
            COMMAND_GPG,
            '--home', abs_gpg_home_dir,
            '--list-keys',
            ]

        try:
            self._executor.check_call(cmd_prefix + ['--no-autostart'])
        except subprocess.CalledProcessError:
            # Does it work without it, at least or is there some unrelated trouble?
            self._executor.check_call(cmd_prefix)

            self._gpg_supports_no_autostart = False
            self._messenger.info('No, it does not.')
        else:
            self._gpg_supports_no_autostart = True
            self._messenger.info('Yes, it does.')

    def _initialize_gpg_home(self, abs_temp_dir):
        abs_gpg_home_dir = os.path.join(abs_temp_dir, 'gpg_home')

        self._messenger.info('Initializing temporary GnuPG home at "%s"...' % abs_gpg_home_dir)
        os.mkdir(abs_gpg_home_dir, 0700)

        self._check_gpg_for_no_autostart_support(abs_gpg_home_dir)

        release_pubring_gpg = resource_filename(resources.__name__, 'pubring.gpg')
        cmd = self._get_gpg_argv_start(abs_gpg_home_dir) + [
                '--import', release_pubring_gpg,
            ]
        self._executor.check_call(cmd)

        return abs_gpg_home_dir

    def _verify_detachted_gpg_signature(self, candidate_filename, signature_filename, abs_gpg_home_dir):
        self._messenger.info('Verifying GnuPG signature of file "%s"...' % candidate_filename)
        cmd = self._get_gpg_argv_start(abs_gpg_home_dir) + [
                '--verify',
                signature_filename,
                candidate_filename,
            ]
        self._executor.check_call(cmd)

    def _verify_clearsigned_gpg_signature(self, clearsigned_filename, output_filename, abs_gpg_home_dir):
        self._messenger.info('Verifying GnuPG signature of file "%s", writing file "%s"...' \
                % (clearsigned_filename, output_filename))

        if os.path.exists(output_filename):
            raise OSError(errno.EEXIST, 'File "%s" exists' % output_filename)

        cmd = self._get_gpg_argv_start(abs_gpg_home_dir) + [
                '--output', output_filename,
                '--decrypt', clearsigned_filename,
                ]
        self._executor.check_call(cmd)

        if not os.path.exists(output_filename):
            raise OSError(errno.ENOENT, 'File "%s" does not exists' % output_filename)

    def run(self):
        abs_temp_dir = os.path.abspath(tempfile.mkdtemp())
        try:
            abs_gpg_home_dir = self._initialize_gpg_home(abs_temp_dir)

            if self._stage3_date_triple_or_none is None:
                self._messenger.info('Searching for available stage3 tarballs...')
                stage3_listing = self.get_url_content(self._get_stage3_listing_url())
                stage3_date_str = self._find_latest_stage3_date(stage3_listing)
                self._messenger.info('Found "%s" to be latest.' % stage3_date_str)
                self._require_fresh_enough(self._parse_stage3_listing_date(stage3_date_str))
            else:
                stage3_date_str = '%04d%02d%02d' % self._stage3_date_triple_or_none

            if self._repository_date_triple_or_none is None:
                self._messenger.info('Searching for available portage repository snapshots...')
                snapshot_listing = self.get_url_content(self._get_portage_snapshot_listing_url())
                snapshot_date_str = self._find_latest_snapshot_date(snapshot_listing)
                self._messenger.info('Found "%s" to be latest.' % snapshot_date_str)
                self._require_fresh_enough(self._parse_snapshot_listing_date(snapshot_date_str))
            else:
                snapshot_date_str = '%04d%02d%02d' % self._repository_date_triple_or_none

            self._messenger.info('Downloading portage repository snapshot...')
            snapshot_tarball, snapshot_gpgsig, snapshot_md5sum, snapshot_uncompressed_md5sum \
                    = self._download_snapshot(snapshot_date_str)
            self._verify_detachted_gpg_signature(snapshot_tarball, snapshot_gpgsig, abs_gpg_home_dir)
            self._verify_md5_sum(snapshot_tarball, snapshot_md5sum)

            self._messenger.info('Downloading stage3 tarball...')
            stage3_tarball, stage3_digests_asc \
                    = self._download_stage3(stage3_date_str)
            stage3_digests = os.path.join(abs_temp_dir, os.path.basename(stage3_digests_asc)[:-len('.asc')])
            self._verify_clearsigned_gpg_signature(stage3_digests_asc, stage3_digests, abs_gpg_home_dir)
            self._verify_sha512_sum(stage3_tarball, stage3_digests)

            snapshot_tarball_uncompressed = self._uncompress_tarball(snapshot_tarball)
            self._verify_md5_sum(snapshot_tarball_uncompressed, snapshot_uncompressed_md5sum)

            self._extract_tarball(stage3_tarball, self._abs_target_dir)
            self._extract_tarball(snapshot_tarball_uncompressed, os.path.join(self._abs_target_dir, 'usr'))
        finally:
            self._messenger.info('Cleaning up "%s"...' % abs_temp_dir)
            shutil.rmtree(abs_temp_dir)

    @classmethod
    def add_arguments_to(clazz, distro):
        distro.add_argument('--arch', dest='architecture', default='amd64',
                help='architecture (e.g. amd64)')
        distro.add_argument('--stage3-date', type=date_argparse_type, metavar='YYYY-MM-DD',
                help='date to use stage3 of (e.g. 2015-05-01, default: latest available)')
        distro.add_argument('--repository-date', type=date_argparse_type, metavar='YYYY-MM-DD',
                help='date to use portage repository snapshot of (e.g. 2015-05-01, default: latest available)')
        distro.add_argument('--max-age-days', type=int, metavar='DAYS', default=14,
                help='age in days to tolerate as recent enough (security feature, default: %(default)s days)')
        distro.add_argument('--mirror', dest='mirror_url', metavar='URL',
                default=_DEFAULT_MIRROR,
                help='mirror to use (default: %(default)s)')

    @classmethod
    def create(clazz, messenger, executor, options):
        return clazz(
                messenger,
                executor,
                os.path.abspath(options.target_dir),
                os.path.abspath(options.cache_dir),
                options.architecture,
                options.mirror_url,
                options.max_age_days,
                options.stage3_date,
                options.repository_date,
                os.path.abspath(options.resolv_conf),
                )
