#!/usr/bin/env python
#
# Copyright 2021 YugaByte, Inc. and Contributors

# Licensed under the Polyform Free Trial License 1.0.0 (the "License"); you
# may not use this file except in compliance with the License. You
# may obtain a copy of the License at
#
# https://github.com/YugaByte/yugabyte-db/blob/master/licenses/POLYFORM-FREE-TRIAL-LICENSE-1.0.0.txt

from __future__ import print_function

import argparse
import atexit
import copy
import logging
import pipes
import random
import string
import subprocess
import time
import json
import uuid

from argparse import RawDescriptionHelpFormatter
from boto.utils import get_instance_metadata
from datetime import timedelta
from multiprocessing.pool import ThreadPool

import os
import re

TABLET_UUID_LEN = 32
UUID_RE_STR = '[0-9a-f-]{32,36}'
COLOCATED_UUID_SUFFIX = '.colocated.parent.uuid'
COLOCATED_NAME_SUFFIX = '.colocated.parent.tablename'
COLOCATED_UUID_RE_STR = UUID_RE_STR + COLOCATED_UUID_SUFFIX
UUID_ONLY_RE = re.compile('^' + UUID_RE_STR + '$')
NEW_OLD_UUID_RE = re.compile(UUID_RE_STR + '[ ]*\t' + UUID_RE_STR)
COLOCATED_NEW_OLD_UUID_RE = re.compile(COLOCATED_UUID_RE_STR + '[ ]*\t' + COLOCATED_UUID_RE_STR)
LEADING_UUID_RE = re.compile('^(' + UUID_RE_STR + r')\b')
FS_DATA_DIRS_ARG_NAME = '--fs_data_dirs'
FS_DATA_DIRS_ARG_PREFIX = FS_DATA_DIRS_ARG_NAME + '='
RPC_BIND_ADDRESSES_ARG_NAME = '--rpc_bind_addresses'
RPC_BIND_ADDRESSES_ARG_PREFIX = RPC_BIND_ADDRESSES_ARG_NAME + '='

# For differential backup, action to take with each file
# NOOP = no operation.
ACTION_COPY = "COPY"
ACTION_MOVE = "MOVE"
ACTION_NOOP = "NOOP"

IMPORTED_TABLE_RE = re.compile(r'(?:Colocated t|T)able being imported: ([^\.]*)\.(.*)')
RESTORATION_RE = re.compile('^Restoration id: (' + UUID_RE_STR + r')\b')

SNAPSHOT_KEYSPACE_RE = re.compile("^[ \t]*Keyspace:.* name='(.*)' type")
SNAPSHOT_TABLE_RE = re.compile("^[ \t]*Table:.* name='(.*)' type")
SNAPSHOT_INDEX_RE = re.compile("^[ \t]*Index:.* name='(.*)' type")

STARTED_SNAPSHOT_CREATION_RE = re.compile(r'[\S\s]*Started snapshot creation: (?P<uuid>.*)')
YSQL_CATALOG_VERSION_RE = re.compile(r'[\S\s]*Version: (?P<version>.*)')

ROCKSDB_PATH_PREFIX = '/yb-data/tserver/data/rocksdb'

SNAPSHOT_DIR_GLOB = '*' + ROCKSDB_PATH_PREFIX + '/table-*/tablet-*.snapshots/*'
DIFF_SNAPSHOT_DIR_GLOB = '*' + ROCKSDB_PATH_PREFIX + '/table-*/tablet-*.snapshots/'
SNAPSHOT_DIR_SUFFIX_RE = re.compile(
    '^.*/tablet-({})[.]snapshots/({})$'.format(UUID_RE_STR, UUID_RE_STR))

SNAPSHOT_FILES_DIR_MIN_DEPTH = 8
SNAPSHOT_FILES_DIR_MAX_DEPTH = 9

TABLE_PATH_PREFIX_TEMPLATE = ROCKSDB_PATH_PREFIX + '/table-{}'

TABLET_MASK = 'tablet-????????????????????????????????'
TABLET_DIR_GLOB = '*' + TABLE_PATH_PREFIX_TEMPLATE + '/' + TABLET_MASK
TABLET_DIR_DEPTH = 6

METADATA_FILE_NAME = 'SnapshotInfoPB'
SQL_DUMP_FILE_NAME = 'YSQLDump'
CREATE_METAFILES_MAX_RETRIES = 10
CLOUD_CFG_FILE_NAME = 'cloud_cfg'
CLOUD_CMD_MAX_RETRIES = 10

NET_ADDR_FILTER_VAL = 'ipv4_external,ipv4_all,ipv6_external,ipv6_non_link_local,ipv6_all'

MANIFEST = 'MANIFEST'
CREATE_SNAPSHOT_TIMEOUT_SEC = 60 * 60  # hour
RESTORE_SNAPSHOT_TIMEOUT_SEC = 24 * 60 * 60  # day
SHA_TOOL_PATH = '/usr/bin/sha256sum'
# Try to read home dir from environment variable, else assume it's /home/yugabyte.
YB_HOME_DIR = os.environ.get("YB_HOME_DIR", "/home/yugabyte")
TSERVER_CONF_PATH = os.path.join(YB_HOME_DIR, 'tserver/conf/server.conf')
K8S_DATA_DIRS = ["/mnt/disk0", "/mnt/disk1"]
DEFAULT_REMOTE_YB_ADMIN_PATH = os.path.join(YB_HOME_DIR, 'master/bin/yb-admin')
DEFAULT_REMOTE_YSQL_DUMP_PATH = os.path.join(YB_HOME_DIR, 'master/postgres/bin/ysql_dump')
DEFAULT_REMOTE_YSQL_SHELL_PATH = os.path.join(YB_HOME_DIR, 'master/bin/ysqlsh')
DEFAULT_YB_USER = 'yugabyte'
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
VERSION = '0.1'



DEFAULT_TS_WEB_PORT = 9000

class Manifest():
    def __init__(self,manifest_id_in):
        self.manifest_version = VERSION
        self.manifest_id = str(manifest_id_in)
        self.manifest_name = str('MANIFEST-'+VERSION+'-'+str(self.manifest_id))
        self.manifest_savepoint_number = 0
        self.manifest_type = "diff"
        self.manifest_universe_name = ""
        self.manifest_universe_id = ""
        self.manifest_create_date = ''
        self.manifest_location = ""
        self.manifest_previous = ""
        self.database_name = ""
        self.database_type = ""
        self.database_tables = dict()
        self.database_objects = dict()
        self.storage_backup_location = ""
        self.storage_backup_location_type = ""
        self.storage_keyspace = ""
        self.storage_table = ""
        self.storage_table_ids = dict()

        self.storage_tablet_ids = dict()

        self.storage_files = dict()
        self.storage_table_ids_dict = dict()
        self.storage_tablet_ids_dict = dict()
        self.storage_files_dict = dict()
        self.backup_name = ""
        self.backup_id = ""
        self.backup_snapshot_id = dict()
        self.backup_leaders = list()
        self.backup_create_date = ""
        self.backup_start_time = ""
        self.backup_end_time = ""
        self.backup_local_dirs = dict()
        self.backup_local_dir_set = []
        self.backup_local_dir_files = dict()
        self.backup_messages = dict()
        self.backup_errors = dict()

    def to_json_dict(self):
        manifest_json = {"manifest": {
            "metadata": {
            "manifest_version": self.manifest_version,
            "manifest_id": self.manifest_id,
            "manifest_name": self.manifest_name,
            "manifest_savepoint_number": self.manifest_savepoint_number,
            "manifest_type": self.manifest_type,
            "manifest_universe_name": self.manifest_universe_name,
            "manifest_universe_id": self.manifest_universe_id,
            "manifest_create_date": self.manifest_create_date,
            "manifest_location": self.manifest_location,
            "manifest_previous": self.manifest_previous,
            },
            "database": {
            "name": self.database_name, "type": self.database_type, "database_tables": str(self.database_tables),"database_objects": str(self.database_objects)
        },
            "storage": {
                "location": self.storage_backup_location,
                "location_type": self.storage_backup_location_type,
                "keyspace": self.storage_keyspace,
                "table": self.storage_table,
                "table_id": (self.storage_table_ids),
                "tablet_ids": self.storage_tablet_ids,
                "files": (self.storage_files)
            }
            , "backup": {
                "name": self.backup_name,
                "snapshot_id": (self.backup_snapshot_id),
                "tablet_leaders": (self.backup_leaders),
                "create_date": self.backup_create_date,
                "start_time": self.backup_start_time,
                "end_time": self.backup_end_time,
                "local_directories": (self.backup_local_dirs),
                "local_directories_set": (self.backup_local_dir_set),
                "local_backup_local_dir_files": (self.backup_local_dir_files),
                "message": (self.backup_messages),
                "error": (self.backup_errors)
            }
        }}
        return manifest_json

    def json_out(self):
        json_dict = self.to_json_dict()
        json_object = json.dumps(json_dict, indent=4)
        return json_object

    def update_storage_tablet_ids(self, manifest_dict):
        self.storage_tablet_ids = manifest_dict['manifest']['storage']['tablet_ids']
        if 'manifest_previous' in manifest_dict['manifest']['metadata']:
            self.manifest_previous = manifest_dict['manifest']['metadata']['manifest_previous']
        else:
            self.manifest_previous = ''


class BackupException(Exception):
    """A YugaByte backup exception."""
    pass


class CompatibilityException(BackupException):
    """Exception which can be ignored for compatibility."""
    pass


def split_by_tab(line):
    return [item.replace(' ', '') for item in line.split("\t")]

def split_by_space(line):
    items = []
    for item in line.split(" "):
        item = item.strip()
        if item:
            items.append(item)
    return items


def quote_cmd_line_for_bash(cmd_line):
    if not isinstance(cmd_line, list) and not isinstance(cmd_line, tuple):
        raise BackupException("Expected a list/tuple, got: [[ {} ]]".format(cmd_line))
    return ' '.join([pipes.quote(str(arg)) for arg in cmd_line])


class BackupTimer:
    def __init__(self):
        # Store the start time as phase 0.
        self.logged_times = [time.time()]
        self.phases = ["START"]
        self.num_phases = 0

    def log_new_phase(self, msg=""):
        self.logged_times.append(time.time())
        self.phases.append(msg)
        # Print completed time of last stage.
        if self.num_phases > 0:  # Don't print for phase 0 as that is just the start-up.
            time_taken = self.logged_times[self.num_phases] - self.logged_times[self.num_phases - 1]
            logging.info("Completed phase {}: {} [Time taken for phase: {}]".format(
                    self.num_phases,
                    self.phases[self.num_phases],
                    str(timedelta(seconds=time_taken))))
        self.num_phases += 1
        logging.info("Starting phase {}: {}".format(self.num_phases, msg))

    def print_summary(self):
        log_str = "Summary of run:\n"
        # Print info for each phase.
        for i in range(1, self.num_phases + 1):
            t = self.logged_times[i] - self.logged_times[i - 1]
            log_str += "{} : PHASE {} : {}\n".format(str(timedelta(seconds=t)), i, self.phases[i])
        # Also print info for total runtime.
        log_str += "Total runtime: {}".format(
                str(timedelta(seconds=time.time() - self.logged_times[0])))
        # Add [app] for YW platform filter.
        logging.info("[app] " + log_str)


class SingleArgParallelCmd:
    """
    Invokes a single-argument function on the given set of argument values in a parallel way
    using the given thread pool. Arguments are first deduplicated, so they have to be hashable.
    Example:
        SingleArgParallelCmd(fn, [a, b, c]).run(pool)
        -> run in parallel Thread-1: ->  fn(a)
                           Thread-2: -> fn(b)
                           Thread-3: -> fn(c)
    """
    def __init__(self, fn, args):
        self.fn = fn
        self.args = args

    def run(self, pool):
        fn_args = sorted(set(self.args))
        return self._run_internal(self.fn, fn_args, fn_args, pool)

    @staticmethod
    def _run_internal(internal_fn, internal_fn_srgs, fn_args, pool):
        values = pool.map(internal_fn, internal_fn_srgs)
        # Return a map from args to the command results.
        assert len(fn_args) == len(values)
        return dict(zip(fn_args, values))


class MultiArgParallelCmd(SingleArgParallelCmd):
    """
    Invokes a function that is allowed to have any number of arguments on the given
    set of tuples of arguments in a parallel way using the given thread pool.
    Arguments are first deduplicated, so they have to be hashable.
    Example:
        MultiArgParallelCmd p(fn)
        p.add_args(a1, a2)
        p.add_args(b1, b2)
        p.run(pool)
        -> run in parallel Thread-1: -> fn(a1, a2)
                           Thread-2: -> fn(b1, b2)
    """
    def __init__(self, fn):
        self.fn = fn
        self.args = []

    def add_args(self, *args_tuple):
        assert isinstance(args_tuple, tuple)
        self.args.append(args_tuple)

    def run(self, pool):
        def internal_fn(args_tuple):
            # One tuple - one function run.
            return self.fn(*args_tuple)

        fn_args = sorted(set(self.args))
        return self._run_internal(internal_fn, fn_args, fn_args, pool)


class SequencedCmdArgs:

    def __init__(self, args, index_to_return):
        self.args = args
        self.index_to_return = index_to_return


class SequencedParallelCmd(SingleArgParallelCmd):
    """
    Invokes commands in a parallel way using the given thread pool.
    Each command is a sequence of function calls with the provided arguments.
    Example:
        SequencedParallelCmd p(fn)
        p.start_command()  # Start sequence-1 of the function calls.
        p.add_args(a1, a2)
        p.add_args(b1, b2)
        p.start_command()  # Start sequence-2 of the function calls.
        p.add_args(c1, c2)
        p.add_args(d1, d2)
        p.run(pool)
        -> run in parallel Thread-1: -> fn(a1, a2); fn(b1, b2)
                           Thread-2: -> fn(c1, c2); fn(d1, d2)
    """
    def __init__(self, fn):
        self.fn = fn
        self.parallel_args = []
        """
        The index is used to return a function call result as the whole command result.
        For example:
            SequencedParallelCmd p(fn)
            p.start_command()
            p.add_args(a1, a2)
            p.add_args_and_save_result(b1, b2)
            p.add_args(c1, c2)
            p.run(pool)
            -> run -> fn(a1, a2); result = fn(b1, b2); fn(c1, c2); return result
        """
        self.result_fn_call_index = None
        self.saved_result_indices = []

    def start_command(self):
        # Start new set of argument tuples.
        self.parallel_args.append(SequencedCmdArgs(args=[], index_to_return=None))

    def add_args(self, *args_tuple):
        assert isinstance(args_tuple, tuple)
        assert len(self.parallel_args) > 0, 'Call start_command() before'
        self.parallel_args[-1].args.append(args_tuple)

    def add_args_and_save_result(self, *args_tuple):
        self.add_args(*args_tuple)
        current_args = self.parallel_args[-1]
        current_args.index_to_return = len(current_args.args) - 1

    def run(self, pool):
        def internal_fn(sequenced_cmd_args):
            assert isinstance(sequenced_cmd_args, SequencedCmdArgs)
            # A list of commands: do it one by one.
            results = []
            for cmd_args in sequenced_cmd_args.args:
                assert isinstance(cmd_args, tuple)
                results.append(self.fn(*cmd_args))

            if sequenced_cmd_args.index_to_return is None:
                return results
            else:
                return results[sequenced_cmd_args.index_to_return]

        fn_args = [str(sequenced_args.args) for sequenced_args in self.parallel_args]
        return self._run_internal(internal_fn, self.parallel_args, fn_args, pool)


def check_arg_range(min_value, max_value):
    """
    Return a "checker" function that validates that an argument is within the given range. To be
    used with argparse.
    """
    def check_fn(value):
        value = int(value)
        if value < min_value or value > max_value:
            raise argparse.ArgumentTypeError("Expected a value between {} and {}, got {}".format(
                min_value, max_value, value))
        return value

    return check_fn


def check_uuid(uuid_str):
    """
    A UUID validator for use with argparse.
    """
    if not UUID_ONLY_RE.match(uuid_str):
        raise argparse.ArgumentTypeError("Expected a UUID, got {}".format(uuid_str))
    return uuid_str


def random_string(length):
    return ''.join(random.choice(string.ascii_lowercase) for i in range(length))


def strip_dir(dir_path):
    return dir_path.rstrip('/\\')


def checksum_path(file_path):
    return file_path + '.sha256'


def checksum_path_downloaded(file_path):
    return checksum_path(file_path) + '.downloaded'


# TODO: get rid of this sed / test program generation in favor of a more maintainable solution.
def key_and_file_filter(checksum_file):
    return "\" $( sed 's| .*/| |' {} ) \"".format(pipes.quote(checksum_file))


# TODO: get rid of this sed / test program generation in favor of a more maintainable solution.
def compare_checksums_cmd(checksum_file1, checksum_file2):
    return "test {} = {} && echo correct || echo invalid".format(
        key_and_file_filter(checksum_file1), key_and_file_filter(checksum_file2))


def get_db_name_cmd(dump_file):
    return "sed -n '/CREATE DATABASE/{s|CREATE DATABASE||;s|WITH.*||;p}' " + pipes.quote(dump_file)


def replace_db_name_cmd(dump_file, old_name, new_name):
    return "sed -i 's|DATABASE {}|DATABASE {}|;s|\\\\connect {}|\\\\connect {}|' {}".format(
        old_name, new_name, old_name, new_name, pipes.quote(dump_file))


def get_table_names_str(keyspaces, tables, delimeter, space):
    if len(keyspaces) != len(tables):
        raise BackupException(
            "Found {} --keyspace keys and {} --table keys. Number of these keys "
            "must be equal.".format(len(keyspaces), len(tables)))

    table_names = []
    for i in range(0, len(tables)):
        table_names.append(delimeter.join([keyspaces[i], tables[i]]))

    return space.join(table_names)


def keyspace_type(keyspace):
    return 'ysql' if ('.' in keyspace) and (keyspace.split('.')[0].lower() == 'ysql') else 'ycql'


def is_parent_colocated_table_name(table_name):
    return table_name.endswith(COLOCATED_NAME_SUFFIX)


def get_postgres_oid_from_table_id(table_id):
    return table_id[-4:]


def verify_colocated_table_ids(old_id, new_id):
    # Assert that the postgres oids are the same.
    if (get_postgres_oid_from_table_id(old_id) != get_postgres_oid_from_table_id(new_id)):
        raise BackupException('Colocated tables have different oids: Old oid: {}, New oid: {}'
                              .format(old_id, new_id))


def keyspace_name(keyspace):
    return keyspace.split('.')[1] if ('.' in keyspace) else keyspace

class BackupOptions:
    def __init__(self, args):
        self.args = args


class AbstractBackupStorage(object):
    def __init__(self, options):
        self.options = options

    @staticmethod
    def storage_type():
        raise BackupException("Unimplemented")

    def _command_list_prefix(self):
        return []


class AzBackupStorage(AbstractBackupStorage):
    def __init__(self, options):
        super(AzBackupStorage, self).__init__(options)

    @staticmethod
    def storage_type():
        return 'az'

    def _command_list_prefix(self):
        return "azcopy"

    def upload_file_cmd(self, src, dest):
        # azcopy requires quotes around the src and dest. This format is necessary to do so.
        src = "'{}'".format(src)
        dest = "'{}'".format(dest + os.getenv('AZURE_STORAGE_SAS_TOKEN'))
        return ["{} {} {} {}".format(self._command_list_prefix(), "cp", src, dest)]

    def download_file_cmd(self, src, dest):
        src = "'{}'".format(src + os.getenv('AZURE_STORAGE_SAS_TOKEN'))
        dest = "'{}'".format(dest)
        return ["{} {} {} {} {}".format(self._command_list_prefix(), "cp", src,
                dest, "--recursive")]

    def upload_dir_cmd(self, src, dest):
        # azcopy will download the top-level directory as well as the contents without "/*".
        src = "'{}'".format(os.path.join(src, '*'))
        dest = "'{}'".format(dest + os.getenv('AZURE_STORAGE_SAS_TOKEN'))
        return ["{} {} {} {} {}".format(self._command_list_prefix(), "cp", src,
                dest, "--recursive")]

    def download_dir_cmd(self, src, dest):
        src = "'{}'".format(os.path.join(src, '*') + os.getenv('AZURE_STORAGE_SAS_TOKEN'))
        dest = "'{}'".format(dest)
        return ["{} {} {} {} {}".format(self._command_list_prefix(), "cp", src,
                dest, "--recursive")]

    def delete_obj_cmd(self, dest):
        if dest is None or dest == '/' or dest == '':
            raise BackupException("Destination needs to be well formed.")
        dest = "'{}'".format(dest + os.getenv('AZURE_STORAGE_SAS_TOKEN'))
        return ["{} {} {} {}".format(self._command_list_prefix(), "rm", dest, "--recursive=true")]

    def move_obj_cmd(self, src, dest):
        src = "'{}'".format(src + os.getenv('AZURE_STORAGE_SAS_TOKEN'))
        dest = "'{}'".format(dest + os.getenv('AZURE_STORAGE_SAS_TOKEN'))
        return ["{} {} {} {} {} && {} {} {} {}".format(self._command_list_prefix(),
                                                       "cp", src, dest, "--recursive",
                                                       self._command_list_prefix(), "rm", src,
                                                       "--recursive=true")]

class GcsBackupStorage(AbstractBackupStorage):
    def __init__(self, options):
        super(GcsBackupStorage, self).__init__(options)

    @staticmethod
    def storage_type():
        return 'gcs'

    def _command_list_prefix(self):
        return ['gsutil', '-o',
                'Credentials:gs_service_key_file=%s' % self.options.cloud_cfg_file_path]

    def upload_file_cmd(self, src, dest):
        return self._command_list_prefix() + ["cp", src, dest]

    def download_file_cmd(self, src, dest):
        return self._command_list_prefix() + ["cp", src, dest]

    def upload_dir_cmd(self, src, dest):
        return self._command_list_prefix() + ["-m", "rsync", "-r", src, dest]

    def download_dir_cmd(self, src, dest):
        return self._command_list_prefix() + ["-m", "rsync", "-r", src, dest]

    def delete_obj_cmd(self, dest):
        if dest is None or dest == '/' or dest == '':
            raise BackupException("Destination needs to be well formed.")
        return self._command_list_prefix() + ["rm", "-r", dest]

    def move_obj_cmd(self, src, dest):
        return self._command_list_prefix() + ["mv", src, dest]

class S3BackupStorage(AbstractBackupStorage):
    def __init__(self, options):
        super(S3BackupStorage, self).__init__(options)

    @staticmethod
    def storage_type():
        return 's3'

    def _command_list_prefix(self):
        # If 's3cmd get' fails it creates zero-length file, '--force' is needed to
        # override this empty file on the next retry-step.
        return ['s3cmd', '--force', '--no-check-certificate', '--config=%s'
                % self.options.cloud_cfg_file_path]

    def upload_file_cmd(self, src, dest):
        cmd_list = ["put", src, dest]
        if self.options.args.sse:
            cmd_list.append("--server-side-encryption")
        return self._command_list_prefix() + cmd_list

    def download_file_cmd(self, src, dest):
        return self._command_list_prefix() + ["get", src, dest]

    def upload_dir_cmd(self, src, dest):
        cmd_list = ["sync", "--no-check-md5", src, dest]
        if self.options.args.sse:
            cmd_list.append("--server-side-encryption")
        return self._command_list_prefix() + cmd_list

    def download_dir_cmd(self, src, dest):
        return self._command_list_prefix() + ["sync", "--no-check-md5", src, dest]

    def delete_obj_cmd(self, dest):
        if dest is None or dest == '/' or dest == '':
            raise BackupException("Destination needs to be well formed.")
        return self._command_list_prefix() + ["del", "-r", dest]

    def move_obj_cmd(self, src, dest):
        cmd_list = ["mv", src, dest]
        return self._command_list_prefix() + cmd_list

class NfsBackupStorage(AbstractBackupStorage):
    def __init__(self, options):
        super(NfsBackupStorage, self).__init__(options)

    @staticmethod
    def storage_type():
        return 'nfs'

    def _command_list_prefix(self):
        result = ['rsync', '-avhW']
        if not self.options.args.mac:
            result.append('--no-compress')
        return result

    # This is a single string because that's what we need for doing `mkdir && rsync`.
    def upload_file_cmd(self, src, dest):
        return ["mkdir -p {} && {} {} {}".format(
            pipes.quote(os.path.dirname(dest)), " ".join(self._command_list_prefix()),
            pipes.quote(src), pipes.quote(dest))]

    def download_file_cmd(self, src, dest):
        return self._command_list_prefix() + [src, dest]

    # This is a list of single string, because a) we need a single string for executing
    # `mkdir && rsync` and b) we need a list of 1 element, as it goes through a tuple().
    def upload_dir_cmd(self, src, dest):
        return ["mkdir -p {} && {} {} {}".format(
            pipes.quote(dest), " ".join(self._command_list_prefix()),
            pipes.quote(src), pipes.quote(dest))]

    def download_dir_cmd(self, src, dest):
        return self._command_list_prefix() + [src, dest]

    def delete_obj_cmd(self, dest):
        if dest is None or dest == '/' or dest == '':
            raise BackupException("Destination needs to be well formed.")
        return ["rm", "-rf", pipes.quote(dest)]

    def move_obj_cmd(self, src, dest):
        return ["mkdir -p {} && mv {} {}".format(pipes.quote(dest),
                                                 pipes.quote(src),
                                                 pipes.quote(dest))]

BACKUP_STORAGE_ABSTRACTIONS = {
    S3BackupStorage.storage_type(): S3BackupStorage,
    NfsBackupStorage.storage_type(): NfsBackupStorage,
    GcsBackupStorage.storage_type(): GcsBackupStorage,
    AzBackupStorage.storage_type(): AzBackupStorage
}


class KubernetesDetails():
    def __init__(self, server_fqdn, config_map):
        self.namespace = server_fqdn.split('.')[2]
        self.pod_name = server_fqdn.split('.')[0]
        # The pod names are yb-master-n/yb-tserver-n where n is the pod number
        # and yb-master/yb-tserver are the container names.

        # TODO(bhavin192): need to change in case of multiple releases
        # in one namespace. Something like find the word 'master' in
        # the name.

        self.container = self.pod_name.rsplit('-', 1)[0]
        self.env_config = os.environ.copy()
        self.env_config["KUBECONFIG"] = config_map[self.namespace]


def get_instance_profile_credentials():
    result = ()
    iam_credentials_endpoint = 'meta-data/iam/security-credentials/'
    metadata = get_instance_metadata(timeout=1, num_retries=1, data=iam_credentials_endpoint)
    if metadata:
        instance_credentials = next(iter(metadata.values()))
        if isinstance(instance_credentials, dict):
            try:
                access_key = instance_credentials['AccessKeyId']
                secret_key = instance_credentials['SecretAccessKey']
                token = instance_credentials['Token']
                result = access_key, secret_key, token
            except KeyError as e:
                logging.info("Could not find {} in instance metadata".format(e))

    return result

def read_aws_credentials_from_file(credentials_file):
    d = {}
    with open(credentials_file, 'r') as fp:
        for line in fp:
            tokens = line.split('=')
            if len(tokens) > 1:
                key = tokens[0].strip()
                if key == 'aws_access_key_id':
                    d['access_key'] = tokens[1].strip()
                elif key == 'aws_secret_access_key':
                    d['secret_key'] = tokens[1].strip()
                elif key == 'aws_session_token':
                    d['access_token'] = tokens[1].strip()
            if len(d) == 3:
                return d
    return d

def write_s3_config_file(path, **kvs):
    def write_kv(f, k, v):
        f.write(k)
        f.write(' = ')
        f.write(v if v else '')
        f.write('\n')
    with open(path, 'w') as fp:
        fp.write('[default]\n')
        for k, v in kvs.items():
            write_kv(fp, k, v)


class YBBackup:
    def __init__(self, args):
        self.leader_master_ip = ''
        self.ysql_ip = ''
        self.live_tserver_ip = ''
        self.tmp_dir_name = ''
        self.server_ips_with_uploaded_cloud_cfg = {}
        self.k8s_namespace_to_cfg = {}
        self.timer = BackupTimer()
        self.tserver_ip_to_web_port = {}
        self.args = args

        self.prev_manifest_class = Manifest(uuid.uuid1())
        self.manifest_class = Manifest(uuid.uuid1())
        self.manifest_class_last_savepoint = ''
        self.pool = None


    def sleep_or_raise(self, num_retry, timeout, ex):
        if num_retry > 0:
            logging.info("Sleep {}... ({} retries left)".format(timeout, num_retry))
            time.sleep(timeout)
        else:
            raise ex

    def run_program(self, args, num_retry=1, timeout=10, env=None, **kwargs):
        """
        Runs the given program with the given set of arguments. Arguments are the same as in
        subprocess.check_output. Logs the program and the output in verbose mode. Also logs the
        command line in case of failure.
        """
        cmd_as_str = quote_cmd_line_for_bash(args)
        if self.args.verbose:
            logging.info("Running command{}: {}".format(
                "" if num_retry == 1 else " ({} retries)".format(num_retry), cmd_as_str))

        while num_retry > 0:
            num_retry = num_retry - 1

            try:
                proc_env = os.environ.copy()
                proc_env.update(env if env is not None else {})

                subprocess_result = str(subprocess.check_output(
                                         args, stderr=subprocess.STDOUT,
                                         env=proc_env, **kwargs).decode('utf-8'))

                if self.args.verbose:
                    logging.info(
                        "Output from running command [[ {} ]]:\n{}\n[[ END OF OUTPUT ]]".format(
                            cmd_as_str, subprocess_result))
                return subprocess_result
            except subprocess.CalledProcessError as e:
                logging.error("Failed to run command [[ {} ]]: code={} output={}".format(
                    cmd_as_str, e.returncode, str(e.output.decode('utf-8'))))
                self.sleep_or_raise(num_retry, timeout, e)
            except Exception as ex:
                logging.error("Failed to run command [[ {} ]]: {}".format(cmd_as_str, ex))
                self.sleep_or_raise(num_retry, timeout, ex)


    @staticmethod
    def get_upload_file_path(backup_location, tablet_id, filename):
        return os.path.join(backup_location, f'tablet-{tablet_id}', filename)


    @staticmethod
    def create(args_list=None):
        parser = argparse.ArgumentParser(
            description='Backup/restore YB table',
            epilog="Use the following environment variables to provide AWS access and secret "
                   "keys for S3:\n"
                   "    export AWS_ACCESS_KEY_ID=<your_aws_access_key>\n"
                   "    export AWS_SECRET_ACCESS_KEY=<your_aws_secret_key>\n"
                   "For GCS:\n"
                   "    export GCS_CREDENTIALS_JSON=<contents_of_gcp_credentials>\n"
                   "For YCQL tables:\n"
                   "    Keys --keyspace, --table and --table_uuid can be repeated several times.\n"
                   "    Recommended order for creating backup: --keyspace ks1 --table tbl1 "
                   "    --table_uuid uuid1 --keyspace ks2 --table tbl2 --table_uuid uuid2 ...\n"
                   "    Recommended order for restoring backup: --keyspace target_ks --table tbl1 "
                   "    --table tbl2 ...\n"
                   "For YSQL DB:\n"
                   "    Only one key --keyspace is supported. The script processes the whole DB.\n"
                   "    For creating backup: --keyspace ysql.db1\n"
                   "    For restoring backup: --keyspace ysql.db1_copy\n"
                   "For YCQL DIFFERENTIAL BACKUP:\n"
                   "Keys --keyspace, --table and --table_uuid can be repeated several times."
                   " only supports ycql as script processes the whole DB",
            formatter_class=RawDescriptionHelpFormatter)

        parser.add_argument(
            '--masters', required=True,
                help="Comma separated list of masters for the cluster")
        parser.add_argument(
            '--ts_web_hosts_ports', help="Custom TS HTTP hosts and ports. "
                                         "In form: <IP>:<Port>,<IP>:<Port>")
        parser.add_argument(
            '--k8s_config', required=False,
            help="Namespace to use for kubectl in case of kubernetes deployment")
        parser.add_argument(
            '--keyspace', action='append', help="Repeatable keyspace of the tables to backup, "
                                                "or a target keyspace for the backup restoring")
        parser.add_argument(
            '--table', action='append',
            help="Repeatable name of the tables to backup or restore")
        parser.add_argument(
            '--table_uuid', action='append',
            help="Repeatable UUID of the tables to backup.")
        parser.add_argument(
            '--local_yb_admin_binary', help="Path to the local yb-admin binary; "
                                            "by default remote yb-admin tool is used")
        parser.add_argument(
            '--remote_yb_admin_binary', default=DEFAULT_REMOTE_YB_ADMIN_PATH,
            help="Path to the remote yb-admin binary")
        parser.add_argument(
            '--local_ysql_dump_binary', help="Path to the local ysql_dump binary; "
                                             "by default remote ysql_dump tool is used")
        parser.add_argument(
            '--remote_ysql_dump_binary', default=DEFAULT_REMOTE_YSQL_DUMP_PATH,
            help="Path to the remote ysql_dump binary")
        parser.add_argument(
            '--local_ysql_shell_binary', help="Path to the local ysql shell binary; "
                                              "by default remote ysql shell tool is used")
        parser.add_argument(
            '--remote_ysql_shell_binary', default=DEFAULT_REMOTE_YSQL_SHELL_PATH,
            help="Path to the remote ysql shell binary")
        parser.add_argument(
            '--ssh_key_path', required=False, help="Path to the ssh key file")
        parser.add_argument(
            '--ssh_user', default=DEFAULT_YB_USER, help="Username to use for the ssh connection.")
        parser.add_argument(
            '--remote_user', default=DEFAULT_YB_USER, help="User that will perform backup tasks.")
        parser.add_argument(
            '--ssh_port', default='54422', help="Port to use for the ssh connection.")
        parser.add_argument(
            '--no_ssh', action='store_true', default=False, help="Don't use SSH to run commands")
        parser.add_argument(
            '--mac', action='store_true', default=False, help="Use MacOS tooling")
        parser.add_argument(
            '--ysql_port', help="Custom YSQL process port. "
                                "Default port is used if not specified.")
        parser.add_argument(
            '--ysql_host', help="Custom YSQL process host. "
                                "First alive TS host is used if not specified.")
        parser.add_argument(
            '--ysql_enable_auth', action='store_true',
            help="Whether ysql authentication is required. If specified, will connect using local "
                 "UNIX socket as the host. Overrides --local_ysql_dump_binary to always "
                 "use remote binary.")
        parser.add_argument(
            '--disable_checksums', action='store_true',
            help="Whether checksums will be created and checked. If specified, will skip using "
                 "checksums.")

        backup_location_group = parser.add_mutually_exclusive_group(required=True)
        backup_location_group.add_argument(
            '--backup_location',
            help="Directory/bucket under which the snapshots should be created or "
                 "an exact snapshot directory in case of snapshot restoring.")
        # Deprecated flag for backwards compatibility.
        backup_location_group.add_argument('--s3bucket', required=False, help=argparse.SUPPRESS)
        parser.add_argument(
            '--no_auto_name',
            action='store_true',
            help="Disable automatic generation of a name under the given backup location. If this "
                 "is specified, the backup location will be the exact path of the directory "
                 "storing the snapshot.")
        parser.add_argument(
            '--no_snapshot_deleting',
            action='store_true',
            help="Disable automatic snapshot deleting after the backup creating or restoring.")
        parser.add_argument(
            '--snapshot_id', type=check_uuid,
            help="Use the existing snapshot ID instead of creating a new one.")
        parser.add_argument(
            '--verbose', required=False, action='store_true', help='Verbose mode')
        parser.add_argument(
            '--restore_points', type=check_arg_range(1, 100), default=1,
            help='Number or restore points for differential backups.')
        parser.add_argument(
            '-j', '--parallelism', type=check_arg_range(1, 100), default=8,
            help='Maximum number of parallel commands to launch. '
                 'This also affects the amount of outgoing s3cmd sync traffic when copying a '
                 'backup to S3.')
        parser.add_argument(
            '--storage_type', choices=list(BACKUP_STORAGE_ABSTRACTIONS.keys()),
            default=S3BackupStorage.storage_type(),
            help="Storage backing for backups, eg: s3, nfs, gcs, ..")
        parser.add_argument(
        'command', choices=['create', 'restore', 'restore_keys', 'delete', 'create_diff'],
        help='Create, restore or delete the backup from the provided backup location.')
        parser.add_argument(
            '--certs_dir', required=False,
            help="The directory containing the certs for secure connections.")
        parser.add_argument(
            '--sse', required=False, action='store_true',
            help='Enable server side encryption on storage')
        parser.add_argument(
            '--backup_keys_source', required=False,
            help="Location of universe encryption keys backup file to upload to backup location"
        )
        parser.add_argument(
            '--restore_keys_destination', required=False,
            help="Location to download universe encryption keys backup file to"
        )
        parser.add_argument(
            '--nfs_storage_path', required=False, help="NFS storage mount path")
        parser.add_argument(
            '--restore_time', required=False,
            help='The Unix microsecond timestamp to which to restore the snapshot.')
        parser.add_argument(
            '--prev_manifest_source',required=False,
            help='The location of last differential backup savepoint, include manifest name. allowed :s3 bucket url')
        parser.add_argument(
            '--prev_manifest_source_type',required=False,
            help='The type of location of last differential backup savepoint. Allowed: s3 ')
        parser.add_argument(
            '--savepoint_number',required=False,
            help='The type of location of last differential backup savepoint. Allowed: s3 ')
        parser.add_argument(
            '--aws_credentials_file', required=False,
            help='Path to aws credentials file.')
        parser.add_argument(
            '--history_file', required=False,
            help='Path to history file of executed backup commands.')
        return YBBackup(parser.parse_args(args_list))


    def post_process_arguments(self):
        if self.args.verbose:
            logging.info("Parsed arguments: {}".format(vars(self.args)))

        if self.args.storage_type == 'nfs':
            logging.info('Checking whether NFS backup storage path mounted on TServers or not')
            tablets_by_leader_ip = []

            output = self.run_yb_admin(['list_all_tablet_servers'])
            for line in output.splitlines():
                if LEADING_UUID_RE.match(line):
                    fields = split_by_space(line)
                    ip_port = fields[1]
                    state = fields[3]
                    (ip, port) = ip_port.split(':')
                    if state == 'ALIVE':
                        tablets_by_leader_ip.append(ip)
            tserver_ips = list(tablets_by_leader_ip)
            SingleArgParallelCmd(self.find_nfs_storage, tserver_ips).run(self.pool)

        self.args.backup_location = self.args.backup_location or self.args.s3bucket
        options = BackupOptions(self.args)
        self.cloud_cfg_file_path = os.path.join(self.get_tmp_dir(), CLOUD_CFG_FILE_NAME)
        if self.is_s3():
            if self.args.aws_credentials_file:
                creds = read_aws_credentials_from_file(self.args.aws_credentials_file)
                if not creds.get('access_key') or not creds.get('secret_key') or not creds.get('access_token'):
                    raise BackupException("Missing required credentials in {}, found: {}".format(self.args.aws_credentials_file, creds))
                write_s3_config_file(self.cloud_cfg_file_path, **creds)
            elif not os.getenv('AWS_SECRET_ACCESS_KEY') and not os.getenv('AWS_ACCESS_KEY_ID'):
                metadata = get_instance_profile_credentials()
                creds = {}
                creds['access_key'] = metadata[0] if metadata else ''
                creds['secret_key'] = metadata[1] if metadata else ''
                creds['access_token'] = metadata[2] if metadata else ''
                write_s3_config_file(self.cloud_cfg_file_path, **creds)
            elif os.getenv('AWS_SECRET_ACCESS_KEY') and os.getenv('AWS_ACCESS_KEY_ID'):
                host_base = os.getenv('AWS_HOST_BASE')
                if host_base:
                    host_base_cfg = 'host_base = {0}\n' \
                                    'host_bucket = {1}.{0}\n'.format(
                                        host_base, self.args.backup_location)
                else:
                    host_base_cfg = ''
                with open(self.cloud_cfg_file_path, 'w') as s3_cfg:
                    s3_cfg.write('[default]\n' +
                                 'access_key = ' + os.environ['AWS_ACCESS_KEY_ID'] + '\n' +
                                 'secret_key = ' + os.environ['AWS_SECRET_ACCESS_KEY'] + '\n' +
                                 host_base_cfg)
            else:
                raise BackupException(
                    "Missing either AWS access key or secret key for S3 "
                    "in AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY environment variables.")

            os.chmod(self.cloud_cfg_file_path, 0o400)
            options.cloud_cfg_file_path = self.cloud_cfg_file_path
        elif self.is_gcs():
            credentials = os.getenv('GCS_CREDENTIALS_JSON')
            if not credentials:
                raise BackupException(
                    "Set GCP credential file for GCS in GCS_CREDENTIALS_JSON "
                    "environment variable.")
            with open(self.cloud_cfg_file_path, 'w') as cloud_cfg:
                cloud_cfg.write(credentials)
            options.cloud_cfg_file_path = self.cloud_cfg_file_path
        elif self.is_az():
            sas_token = os.getenv('AZURE_STORAGE_SAS_TOKEN')
            if not sas_token:
                raise BackupException(
                    "Set SAS for Azure Storage in AZURE_STORAGE_SAS_TOKEN environment variable.")
            if '?sv' not in sas_token:
                raise BackupException(
                    "SAS tokens must begin with '?sv'.")

        self.storage = BACKUP_STORAGE_ABSTRACTIONS[self.args.storage_type](options)

        if self.is_k8s():
            self.k8s_namespace_to_cfg = json.loads(self.args.k8s_config)
            if self.k8s_namespace_to_cfg is None:
                raise BackupException("Couldn't load k8s configs")

        if self.args.ts_web_hosts_ports:
            logging.info('TS Web hosts/ports: %s' % (self.args.ts_web_hosts_ports))
            for host_port in self.args.ts_web_hosts_ports.split(','):
                (host, port) = host_port.split(':')
                self.tserver_ip_to_web_port[host] = port

    def table_names_str(self, delimeter='.', space=' '):
        return get_table_names_str(self.args.keyspace, self.args.table, delimeter, space)

    def is_s3(self):
        return self.args.storage_type == S3BackupStorage.storage_type()

    def is_gcs(self):
        return self.args.storage_type == GcsBackupStorage.storage_type()

    def is_az(self):
        return self.args.storage_type == AzBackupStorage.storage_type()

    def is_nfs(self):
        return self.args.storage_type == NfsBackupStorage.storage_type()

    def is_k8s(self):
        return self.args.k8s_config is not None

    def is_cloud(self):
        return self.args.storage_type != NfsBackupStorage.storage_type()

    def has_cfg_file(self):
        return self.args.storage_type in [
            GcsBackupStorage.storage_type(), S3BackupStorage.storage_type()]

    def is_ysql_keyspace(self):
        return self.args.keyspace and keyspace_type(self.args.keyspace[0]) == 'ysql'

    def is_differential_backup(self):
        return self.args.command == 'create_diff'

    def needs_change_user(self):
        return self.args.ssh_user != self.args.remote_user

    def get_main_host_ip(self):
        if self.is_k8s():
            return self.get_live_tserver_ip()
        else:
            return self.get_leader_master_ip()

    def get_leader_master_ip(self):
        if not self.leader_master_ip:
            all_masters = self.args.masters.split(",")
            # Use first Master's ip in list to get list of all masters.
            # self.leader_master_ip, port) = all_masters[0].split(':')[0]
            self.leader_master_ip = all_masters[0].split(':')[0]

            # Get LEADER ip, if it's ALIVE, else any alive master ip.
            output = self.run_yb_admin(['list_all_masters'])
            for line in output.splitlines():
                if LEADING_UUID_RE.match(line):
                    _, ip_port, state, role = split_by_tab(line)
                    ip, _ = ip_port.split(':')
                    if state == 'ALIVE':
                        alive_master_ip = ip
                    if role == 'LEADER':
                        break
            self.leader_master_ip = alive_master_ip

        return self.leader_master_ip

    def get_live_tserver_ip(self):
        if not self.live_tserver_ip:
            output = self.run_yb_admin(['list_all_tablet_servers'])
            for line in output.splitlines():
                if LEADING_UUID_RE.match(line):
                    fields = split_by_space(line)
                    ip_port = fields[1]
                    state = fields[3]
                    ip, _ = ip_port.split(':')
                    if state == 'ALIVE':
                        self.live_tserver_ip = ip
                        break

        if not self.live_tserver_ip:
            raise BackupException("Cannot get alive TS:\n{}".format(output))

        return self.live_tserver_ip

    def get_ysql_ip(self):
        if not self.ysql_ip:
            output = ""
            if self.args.ysql_enable_auth:
                # Note that this requires YSQL commands to be run on the master leader.
                # In case of k8s, we get live tserver, since master pod does not have
                # pgsql unix socket.
                socket_fds = self.run_ssh_cmd(
                        "ls /tmp/.yb.*/.s.PGSQL.*", self.get_main_host_ip()).strip().split()
                if len(socket_fds):
                    self.ysql_ip = os.path.dirname(socket_fds[0])
                else:
                    output = "Failed to find local socket."
            elif self.args.ysql_host:
                self.ysql_ip = self.args.ysql_host
            else:
                # Get first ALIVE TS.
                self.ysql_ip = self.get_live_tserver_ip()

            if not self.ysql_ip:
                raise BackupException("Cannot get alive TS:\n{}".format(output))

        return self.ysql_ip

    def run_tool(self, local_tool, remote_tool, std_args, cmd_line_args, run_ip=None, env_vars=None):
        """
        Runs the utility from the configured location.
        :param cmd_line_args: command-line arguments to the tool
        :return: the standard output of the tool
        """

        # Use local tool if it's specified.
        if local_tool:
            if not os.path.exists(local_tool):
                raise BackupException("Tool binary not found at {}".format(local_tool))

            return self.run_program([local_tool] + std_args + cmd_line_args,
                                    env=env_vars, num_retry=10)
        else:
            if run_ip:
                run_at_location = run_ip
            else:
                run_at_location = self.get_leader_master_ip()
            # Using remote tool binary on leader master server.
            return self.run_ssh_cmd(
                [remote_tool] + std_args + cmd_line_args,
                run_at_location,
                num_ssh_retry=10,
                env_vars=env_vars)

    def run_yb_admin(self, cmd_line_args, run_ip=None):
        """
        Runs the yb-admin utility from the configured location.
        :param cmd_line_args: command-line arguments to yb-admin
        :return: the standard output of yb-admin
        """

        cmd_line_args += [
            '--undefok', 'net_address_filter',
            '--net_address_filter', NET_ADDR_FILTER_VAL
        ]

        # Specify cert file in case TLS is enabled.
        cert_flag = []
        if self.args.certs_dir:
            cert_flag = ["--certs_dir_name", self.args.certs_dir]
            cmd_line_args = cert_flag + cmd_line_args

        return self.run_tool(self.args.local_yb_admin_binary, self.args.remote_yb_admin_binary,
                             ['--master_addresses', self.args.masters],
                             cmd_line_args, run_ip=run_ip)

    def get_ysql_dump_std_args(self):
        args = ['--host=' + self.get_ysql_ip()]
        if self.args.ysql_port:
            args += ['--port=' + self.args.ysql_port]
        return args

    def run_ysql_dump(self, cmd_line_args):
        """
        Runs the ysql_dump utility from the configured location.
        :param cmd_line_args: command-line arguments to ysql_dump
        :return: the standard output of ysql_dump
        """

        certs_env = {}
        if self.args.certs_dir:
            certs_env = {
                            'FLAGS_certs_dir': self.args.certs_dir,
                            'FLAGS_use_node_to_node_encryption': 'true',
                            'FLAGS_use_node_hostname_for_local_tserver': 'true',
                            'FLAGS_net_address_filter': NET_ADDR_FILTER_VAL
            }

        run_at_ip = None
        if self.is_k8s():
            run_at_ip = self.get_live_tserver_ip()
        # If --ysql_enable_auth is passed, connect with ysql through the remote socket.
        local_binary = None if self.args.ysql_enable_auth else self.args.local_ysql_dump_binary

        return self.run_tool(local_binary, self.args.remote_ysql_dump_binary,
                             self.get_ysql_dump_std_args() + ['--masters=' + self.args.masters],
                             cmd_line_args, run_ip=run_at_ip, env_vars=certs_env)

    def run_ysql_shell(self, cmd_line_args):
        """
        Runs the ysql shell utility from the configured location.
        :param cmd_line_args: command-line arguments to ysql shell
        :return: the standard output of ysql shell
        """
        run_at_ip = None
        if self.is_k8s():
            run_at_ip = self.get_live_tserver_ip()

        return self.run_tool(self.args.local_ysql_shell_binary, self.args.remote_ysql_shell_binary,
                             self.get_ysql_dump_std_args(), cmd_line_args, run_ip=run_at_ip)

    def create_snapshot(self):
        """
        Creates a new snapshot of the configured table.
        :return: snapshot id
        """
        if self.args.table:
            yb_admin_args = ['create_snapshot'] + self.table_names_str(' ').split(' ')
        elif self.is_ysql_keyspace():
            yb_admin_args = ['create_database_snapshot', self.args.keyspace[0]]
        else:
            yb_admin_args = ['create_keyspace_snapshot', self.args.keyspace[0]]

        output = self.run_yb_admin(yb_admin_args)
        # Ignores any string before and after the creation string + uuid.
        # \S\s matches every character including newlines.
        matched = STARTED_SNAPSHOT_CREATION_RE.match(output)
        if not matched:
            raise BackupException(
                    "Couldn't parse create snapshot output! Expected "
                    "'Started snapshot creation: <id>' in the end: {}".format(output))
        snapshot_id = matched.group('uuid')
        if not UUID_ONLY_RE.match(snapshot_id):
            raise BackupException("Did not get a valid snapshot id out of yb-admin output:\n" +
                                  output)
        return snapshot_id

    def wait_for_snapshot(self, snapshot_id, op, timeout_sec, update_table_list,
                          complete_state='COMPLETE'):
        """
        Waits for the given snapshot to finish being created or restored.
        """
        start_time = time.time()
        snapshot_done = False
        snapshot_tables = []
        snapshot_keyspaces = []
        snapshot_table_uuids = []
        failed_state = 'FAILED'

        yb_admin_args = ['list_snapshots']
        if update_table_list:
            yb_admin_args += ['SHOW_DETAILS']

        while time.time() - start_time < timeout_sec and not snapshot_done:
            output = self.run_yb_admin(yb_admin_args)
            # Expected format:
            # Snapshot UUID                         State
            # 0436035d-c4c5-40c6-b45b-19538849b0d9  COMPLETE
            #   {"type":"NAMESPACE","id":"e4c5591446db417f83a52c679de03118","data":{"name":"a",...}}
            #   {"type":"TABLE","id":"d9603c2cab0b48ec807936496ac0e70e","data":{"name":"t2",...}}
            #   {"type":"TABLE","id":"28b5cebe9b0c4cdaa70ce9ceab31b1e5","data":{\
            #       "name":"t2idx","indexed_table_id":"d9603c2cab0b48ec807936496ac0e70e",...}}
            # c1ad61bf-a42b-4bbb-94f9-28516985c2c5  COMPLETE
            #   ...
            keyspaces = {}
            for line in output.splitlines():
                if not snapshot_done:
                    if line.find(snapshot_id) == 0:
                        (found_snapshot_id, state) = line.split()
                        if found_snapshot_id == snapshot_id:
                            if state == complete_state:
                                snapshot_done = True
                                if not update_table_list:
                                    break
                            elif state == failed_state:
                                raise BackupException(
                                    'Snapshot id %s, %s failed!' % (snapshot_id, op))
                elif update_table_list:
                    if line[0] != ' ':
                        break
                    loaded_json = json.loads(line)
                    object_type = loaded_json['type']
                    object_id = loaded_json['id']
                    data = loaded_json['data']
                    if object_type == 'NAMESPACE' and object_id not in keyspaces:
                        keyspace_prefix = 'ysql.' \
                            if data['database_type'] == 'YQL_DATABASE_PGSQL' else ''
                        keyspaces[object_id] = keyspace_prefix + data['name']
                    elif object_type == 'TABLE':
                        snapshot_keyspaces.append(keyspaces[data['namespace_id']])
                        snapshot_tables.append(data['name'])
                        snapshot_table_uuids.append(object_id)

            if not snapshot_done:
                logging.info('Waiting for snapshot %s to complete...' % (op))
                time.sleep(5)

        if not snapshot_done:
            raise BackupException('Timed out waiting for snapshot!')

        if update_table_list:
            if len(snapshot_tables) == 0:
                raise CompatibilityException("Created snapshot does not have tables.")

            if len(snapshot_keyspaces) != len(snapshot_tables):
                raise BackupException(
                    "In the snapshot found {} keyspaces and {} tables. The numbers must be equal.".
                    format(len(snapshot_keyspaces), len(snapshot_tables)))

            self.args.keyspace = snapshot_keyspaces
            self.args.table = snapshot_tables
            self.args.table_uuid = snapshot_table_uuids
            logging.info('Updated list of processing tables: ' + self.table_names_str())

        logging.info('Snapshot id %s %s completed successfully' % (snapshot_id, op))

    def find_tablet_leaders(self):
        """
        Lists all tablets and their leaders for the table of interest.
        :return: a list of (tablet id, leader host) tuples
        """
        tablet_leaders = []

        for i in range(0, len(self.args.table)):
            # Don't call list_tablets on a parent colocated table.
            if is_parent_colocated_table_name(self.args.table[i]):
                continue

            if self.args.table_uuid:
                yb_admin_args = ['list_tablets', 'tableid.' + self.args.table_uuid[i], '0']
            else:
                yb_admin_args = ['list_tablets', self.args.keyspace[i], self.args.table[i], '0']

            output = self.run_yb_admin(yb_admin_args)
            for line in output.splitlines():
                if LEADING_UUID_RE.match(line):
                    fields = split_by_tab(line)
                    tablet_id = fields[0]
                    tablet_leader_host_port = fields[2]
                    ts_host, _ = tablet_leader_host_port.split(":")
                    tablet_leaders.append((tablet_id, ts_host))
        return tablet_leaders

    def create_remote_tmp_dir(self, server_ip):
        if self.args.verbose:
            logging.info("Creating {} on server {}".format(self.get_tmp_dir(), server_ip))

        atexit.register(self.cleanup_remote_temporary_directory, server_ip, self.get_tmp_dir())

        return self.run_ssh_cmd(['mkdir', '-p', self.get_tmp_dir()],
                                server_ip, upload_cloud_cfg=False)

    def upload_cloud_config(self, server_ip):
        if server_ip not in self.server_ips_with_uploaded_cloud_cfg:
            if self.args.verbose:
                logging.info(
                    "Uploading {} to server {}".format(self.cloud_cfg_file_path, server_ip))

            output = self.create_remote_tmp_dir(server_ip)
            if self.is_k8s():
                k8s_details = KubernetesDetails(server_ip, self.k8s_namespace_to_cfg)
                output += self.run_program([
                    'kubectl',
                    'cp',
                    self.cloud_cfg_file_path,
                    '{}/{}:{}'.format(
                        k8s_details.namespace, k8s_details.pod_name, self.get_tmp_dir()),
                    '-c',
                    k8s_details.container,
                    '--no-preserve=true'
                ], env=k8s_details.env_config)
            elif not self.args.no_ssh:
                if self.needs_change_user():
                    # TODO: Currently ssh_wrapper_with_sudo.sh will only change users to yugabyte,
                    # not args.remote_user.
                    ssh_wrapper_path = os.path.join(SCRIPT_DIR, 'ssh_wrapper_with_sudo.sh')
                    output += self.run_program(
                        ['scp',
                         '-S', ssh_wrapper_path,
                         '-o', 'StrictHostKeyChecking=no',
                         '-o', 'UserKnownHostsFile=/dev/null',
                         '-i', self.args.ssh_key_path,
                         '-P', self.args.ssh_port,
                         '-q',
                         self.cloud_cfg_file_path,
                         '%s@%s:%s' % (self.args.ssh_user, server_ip, self.get_tmp_dir())])
                else:
                    output += self.run_program(
                        ['scp',
                         '-o', 'StrictHostKeyChecking=no',
                         '-o', 'UserKnownHostsFile=/dev/null',
                         '-i', self.args.ssh_key_path,
                         '-P', self.args.ssh_port,
                         '-q',
                         self.cloud_cfg_file_path,
                         '%s@%s:%s' % (self.args.ssh_user, server_ip, self.get_tmp_dir())])

            self.server_ips_with_uploaded_cloud_cfg[server_ip] = output

            if self.args.verbose:
                logging.info("Uploading {} to server {} done: {}".format(
                    self.cloud_cfg_file_path, server_ip, output))

    def run_ssh_cmd(self, cmd, server_ip, upload_cloud_cfg=True, num_ssh_retry=3, env_vars=None):
        """
        Runs the given command on the given remote server over SSH.
        :param cmd: either a string, or a list of arguments. In the latter case, each argument
                    is properly escaped before being passed to ssh.
        :param server_ip: IP address or host name of the server to SSH into.
        :return: the standard output of the SSH command
        """
        if upload_cloud_cfg and self.has_cfg_file():
            self.upload_cloud_config(server_ip)

        if self.args.verbose:
            logging.info("Running command {} on server {}".format(cmd, server_ip))

        if not isinstance(cmd, str):
            if len(cmd) == 1:
                cmd = cmd[0]
            else:
                cmd = quote_cmd_line_for_bash(cmd)

        num_retries = CLOUD_CMD_MAX_RETRIES if self.is_cloud() else num_ssh_retry

        if env_vars is not None:
            # Add env vars to the front of the cmd shell-style like "FOO=bar ls -l"
            bash_env_args = " ".join(["{}={}".format(env_name, pipes.quote(env_val)) for
                                     (env_name, env_val) in env_vars.items()])
            cmd = "{} {}".format(bash_env_args, cmd)
        if self.is_k8s():
            k8s_details = KubernetesDetails(server_ip, self.k8s_namespace_to_cfg)
            return self.run_program([
                'kubectl',
                'exec',
                '-t',
                '-n={}'.format(k8s_details.namespace),
                # For k8s, pick the first qualified name, if given a CNAME.
                k8s_details.pod_name,
                '-c',
                k8s_details.container,
                '--',
                'bash',
                '-c',
                cmd],
                num_retry=num_retries,
                env=k8s_details.env_config)
        elif not self.args.no_ssh:
            change_user_cmd = 'sudo -u %s' % (self.args.remote_user) \
                if self.needs_change_user() else ''
            return self.run_program([
                'ssh',
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'UserKnownHostsFile=/dev/null',
                '-i', self.args.ssh_key_path,
                '-p', self.args.ssh_port,
                '-q',
                '%s@%s' % (self.args.ssh_user, server_ip),
                'cd / && %s bash -c ' % (change_user_cmd) + pipes.quote(cmd)],
                num_retry=num_retries)
        else:
            return self.run_program(['bash', '-c', cmd])

    def find_data_dirs(self, tserver_ip):
        """
        Finds the data directories on the given tserver. This just reads a config file on the target
        server.
        :param tserver_ip: tablet server ip
        :return: a list of top-level YB data directories
        """
        web_port = (self.tserver_ip_to_web_port[tserver_ip]
                    if tserver_ip in self.tserver_ip_to_web_port else DEFAULT_TS_WEB_PORT)
        output = self.run_program(['curl', "{}:{}/varz".format(tserver_ip, web_port)], num_retry=10)
        data_dirs = []
        for line in output.split('\n'):
            if line.startswith(FS_DATA_DIRS_ARG_PREFIX):
                for data_dir in line[len(FS_DATA_DIRS_ARG_PREFIX):].split(','):
                    data_dir = data_dir.strip()
                    if data_dir:
                        data_dirs.append(data_dir)
                break

        if not data_dirs:
            raise BackupException(
                ("Did not find any data directories in tserver by querying /varz endpoint"
                 " on tserver '{}:{}'. Was looking for '{}', got this: [[ {} ]]").format(
                     tserver_ip, web_port, FS_DATA_DIRS_ARG_PREFIX, output))
        elif self.args.verbose:
            logging.info("Found data directories on tablet server '{}': {}".format(
                tserver_ip, data_dirs))

        return data_dirs

    def generate_snapshot_dirs(self, data_dir_by_tserver, snapshot_id,
                               tablets_by_tserver_ip, table_ids):
        """
        Generate snapshot directories under the given data directory for the given snapshot id
        on the given tservers.
        :param data_dir_by_tserver: data directory on tservers
        :param snapshot_id: snapshot UUID
        :param tablets_by_tserver_ip: a map from tserver ip address to all tablets of our table
            that it is responsible for.
        :param table_ids: new table UUIDs for all tables
        :return: a three-level map: tablet server ip address to a tablet id to all snapshot
            directories for that tablet id that we found.
        """
        tserver_ip_to_tablet_id_to_snapshot_dirs = {}
        deleted_tablets_by_tserver_ip = {}

        tserver_ip_to_tablet_dirs = {}
        for tserver_ip in tablets_by_tserver_ip:
            tserver_ip_to_tablet_dirs.setdefault(tserver_ip, [])

        for table_id in table_ids:
            for tserver_ip in tablets_by_tserver_ip:
                data_dirs = data_dir_by_tserver[tserver_ip]
                tablet_dirs = tserver_ip_to_tablet_dirs[tserver_ip]

                for data_dir in data_dirs:
                    # Find all tablets for this table on this TS in this data_dir:
                    output = self.run_ssh_cmd(
                      ['find', data_dir,
                       '!', '-readable', '-prune', '-o',
                       '-name', TABLET_MASK,
                       '-and',
                       '-wholename', TABLET_DIR_GLOB.format(table_id),
                       '-print'],
                      tserver_ip)
                    tablet_dirs += [line.strip() for line in output.split("\n") if line.strip()]

                if self.args.verbose:
                    msg = "Found tablet directories for table '{}' on  tablet server '{}': {}"
                    logging.info(msg.format(table_id, tserver_ip, tablet_dirs))

                if not tablet_dirs:
                    logging.error("No tablet directory found for table '{}' on "
                                  "tablet server '{}'.".format(table_id, tserver_ip))

                    raise BackupException("Tablets for table " + table_id
                                          + " not found on tablet server " + tserver_ip)

        for tserver_ip in tablets_by_tserver_ip:
            tablets = tablets_by_tserver_ip[tserver_ip]
            tablet_dirs = tserver_ip_to_tablet_dirs[tserver_ip]
            tablet_id_to_snapshot_dirs = \
                tserver_ip_to_tablet_id_to_snapshot_dirs.setdefault(tserver_ip, {})
            deleted_tablets = deleted_tablets_by_tserver_ip.setdefault(tserver_ip, set())
            tablet_dir_by_id = {}

            for tablet_dir in tablet_dirs:
                tablet_dir_by_id[tablet_dir[-TABLET_UUID_LEN:]] = tablet_dir

            for tablet_id in tablets:
                if tablet_id in tablet_dir_by_id:
                    # Tablet was found in a data dir - use this path.
                    snapshot_dir = tablet_dir_by_id[tablet_id] + '.snapshots/' + snapshot_id
                    tablet_id_to_snapshot_dirs.setdefault(tablet_id, set()).add(snapshot_dir)
                else:
                    # Tablet was not found. That means that the tablet was deleted from this TS.
                    # Let's ignore the tablet and allow retry-loop to find and process new
                    # tablet location on the next downloading round.
                    deleted_tablets.add(tablet_id)
                    if self.args.verbose:
                        logging.info("Tablet '{}' directory was not found on "
                                     "tablet server '{}'.".format(tablet_id, tserver_ip))

            if self.args.verbose:
                logging.info("Downloading list for tablet server '{}': {}".format(
                    tserver_ip, tablet_id_to_snapshot_dirs))

            if deleted_tablets:
                logging.info("No snapshot directories generated on tablet server '{}' "
                             "for tablet ids: '{}'".format(tserver_ip, deleted_tablets))

        return (tserver_ip_to_tablet_id_to_snapshot_dirs, deleted_tablets_by_tserver_ip)

    def find_snapshot_files(self, data_dir, snapshot_id, tserver_ip):
        """
        Find snapshot directories under the given data directory for the given snapshot id on the
        given tserver.
        :param data_dir: top-level data directory
        :param snapshot_id: snapshot UUID
        :param tserver_ip: tablet server IP or host name
        :return: a list of absolute paths of remote snapshot directories for the given snapshot
        """
        #files plus directories need find command like one below..
        #look in manifest of last save point and then compare to current
        output = self.run_ssh_cmd(
            ['find', data_dir,
             '-mindepth', SNAPSHOT_FILES_DIR_MIN_DEPTH,
             '-maxdepth', SNAPSHOT_FILES_DIR_MAX_DEPTH,
             '-name', "*", '-and',
             '-wholename', DIFF_SNAPSHOT_DIR_GLOB + snapshot_id + "*", '-type', 'f'],
            tserver_ip)
        return [line.strip() for line in output.split("\n") if line.strip()]

    def get_filelist(self, tablet_leaders, snapshot_id):
        """
         Uploads snapshot directories from all tablet servers hosting our table to subdirectories
         of the given target backup directory.
         :param tablet_leaders: a list of (tablet_id, tserver_ip) pairs
         :param snapshot_id: self-explanatory
         :param snapshot_filepath: the top-level directory under which to upload the data directories
         """
        tablets_by_leader_ip = {}
        for (tablet_id, leader_ip) in tablet_leaders:
            tablets_by_leader_ip.setdefault(leader_ip, set()).add(tablet_id)

        tserver_ips = sorted(tablets_by_leader_ip.keys())
        if self.args.verbose:
            logging.info('Tablets_by_leader_ip {} {}'.format(type(tablets_by_leader_ip), tablets_by_leader_ip))
        data_dir_by_tserver = SingleArgParallelCmd(self.find_data_dirs, tserver_ips).run(self.pool)
        if self.args.verbose:
            logging.info('\nData_dir_by_tserver\n{}\n{}\n'.format( type(data_dir_by_tserver), data_dir_by_tserver))

        for tserver_ip in tserver_ips:
            data_dir_by_tserver[tserver_ip] = copy.deepcopy(data_dir_by_tserver[tserver_ip])

        parallel_find_files = MultiArgParallelCmd(self.find_snapshot_files)
        if self.args.verbose:
            logging.info('Parallel_find_files {} '.format(parallel_find_files))

        tservers_processed = []
        while len(tserver_ips) > len(tservers_processed):
            for tserver_ip in list(tserver_ips):
                if tserver_ip not in tservers_processed:
                    data_dirs = data_dir_by_tserver[tserver_ip]
                    if len(data_dirs) > 0:
                        data_dir = data_dirs[0]
                        parallel_find_files.add_args(data_dir, snapshot_id, tserver_ip)
                        data_dirs.remove(data_dir)

                        if len(data_dirs) == 0:
                            tservers_processed += [tserver_ip]
                    else:
                        tservers_processed += [tserver_ip]

        find_snapshot_file_results = parallel_find_files.run(self.pool)
        if self.args.verbose:
            logging.info('Find_snapshot_file_results type {} \n{}'.format(type(find_snapshot_file_results), find_snapshot_file_results))

        return find_snapshot_file_results

    def find_snapshot_directories(self, data_dir, snapshot_id, tserver_ip):
        """
        Find snapshot directories under the given data directory for the given snapshot id on the
        given tserver.
        :param data_dir: top-level data directory
        :param snapshot_id: snapshot UUID
        :param tserver_ip: tablet server IP or host name
        :return: a list of absolute paths of remote snapshot directories for the given snapshot
        """
        output = self.run_ssh_cmd(
            ['find', data_dir,
             '!', '-readable', '-prune', '-o',
             '-name', snapshot_id, '-and',
             '-wholename', SNAPSHOT_DIR_GLOB,
             '-print'],
            tserver_ip)
        return [line.strip() for line in output.split("\n") if line.strip()]

    def upload_snapshot_directories(self, tablet_leaders, snapshot_id, snapshot_filepath):
        """
        Uploads snapshot directories from all tablet servers hosting our table to subdirectories
        of the given target backup directory.
        :param tablet_leaders: a list of (tablet_id, tserver_ip) pairs
        :param snapshot_id: self-explanatory
        :param snapshot_filepath: the top-level directory under which to upload the data directories
        """
        tablets_by_leader_ip = {}
        for (tablet_id, leader_ip) in tablet_leaders:
            tablets_by_leader_ip.setdefault(leader_ip, set()).add(tablet_id)

        tserver_ips = sorted(tablets_by_leader_ip.keys())
        data_dir_by_tserver = SingleArgParallelCmd(self.find_data_dirs, tserver_ips).run(self.pool)

        for tserver_ip in tserver_ips:
            data_dir_by_tserver[tserver_ip] = copy.deepcopy(data_dir_by_tserver[tserver_ip])

        parallel_find_snapshots = MultiArgParallelCmd(self.find_snapshot_directories)
        tservers_processed = []
        while len(tserver_ips) > len(tservers_processed):
            for tserver_ip in list(tserver_ips):
                if tserver_ip not in tservers_processed:
                    data_dirs = data_dir_by_tserver[tserver_ip]
                    if len(data_dirs) > 0:
                        data_dir = data_dirs[0]
                        parallel_find_snapshots.add_args(data_dir, snapshot_id, tserver_ip)
                        data_dirs.remove(data_dir)

                        if len(data_dirs) == 0:
                            tservers_processed += [tserver_ip]
                    else:
                        tservers_processed += [tserver_ip]

        find_snapshot_dir_results = parallel_find_snapshots.run(self.pool)

        leader_ip_to_tablet_id_to_snapshot_dirs = self.rearrange_snapshot_dirs(
            find_snapshot_dir_results, snapshot_id, tablets_by_leader_ip)

        parallel_uploads = SequencedParallelCmd(self.run_ssh_cmd)
        self.prepare_cloud_ssh_cmds(
             parallel_uploads, leader_ip_to_tablet_id_to_snapshot_dirs, snapshot_filepath,
             snapshot_id, tablets_by_leader_ip, upload=True, snapshot_metadata=None)

        # Run a sequence of steps for each tablet, handling different tablets in parallel.
        parallel_uploads.run(self.pool)

    def rearrange_snapshot_dirs(
            self, find_snapshot_dir_results, snapshot_id, tablets_by_tserver_ip):
        """
        :param find_snapshot_dir_results: a map from (data_dir, snapshot_id, tserver_ip)
            tuples to the list of snapshot directories under that data directory on that tserver.
            (snapshot_id here is always the single snapshot_id we're dealing with.)
        :param snapshot_id: the snapshot id!
        :param tablets_by_tserver_ip: a map from tserver ip address to all tablets of our table
            that it is responsible for.
        :return: a three-level map: tablet server ip address to a tablet id to all snapshot
            directories for that tablet id that we found.
        """
        tserver_ip_to_tablet_id_to_snapshot_dirs = {}
        for key in find_snapshot_dir_results:
            _, snapshot_id_unused, tserver_ip = key
            snapshot_dirs = find_snapshot_dir_results[key]
            assert snapshot_id_unused == snapshot_id
            tablet_id_to_snapshot_dirs =\
                tserver_ip_to_tablet_id_to_snapshot_dirs.setdefault(tserver_ip, {})

            for snapshot_dir in snapshot_dirs:
                suffix_match = SNAPSHOT_DIR_SUFFIX_RE.match(snapshot_dir)
                if not suffix_match:
                    raise BackupException(
                        ("Could not parse tablet id and snapshot id out of snapshot "
                         "directory: '{}'").format(snapshot_dir))
                if snapshot_id != suffix_match.group(2):
                    raise BackupException(
                        "Snapshot directory does not end with snapshot id: '{}'".format(
                            snapshot_dir))

                tablet_id = suffix_match.group(1)
                # During CREATE BACKUP only the LEADER tablet replicas are needed.
                # So, ignore the following warning for FOLLOWERS. It's expected because
                # FOLLOWERS replicas are not in the 'tablets_by_tserver_ip' list
                # (the list 'tablets_by_tserver_ip' contains only the LEADER replicas).
                if tablet_id not in tablets_by_tserver_ip[tserver_ip]:
                    logging.warning(
                        ("Found a snapshot directory '{}' on tablet server '{}' that is not "
                         "present in the list of tablets we are interested in that have this "
                         "tserver hosting it ({}), skipping.").format(
                            snapshot_dir, tserver_ip,
                            ", ".join(sorted(tablets_by_tserver_ip[tserver_ip]))))
                    continue

                tablet_id_to_snapshot_dirs.setdefault(tablet_id, set()).add(snapshot_dir)

        return tserver_ip_to_tablet_id_to_snapshot_dirs

    def create_checksum_cmd_not_quoted(self, file_path, checksum_file_path, run_local=False):
        prefix = '/usr/bin/shasum -a 256' if (self.args.mac and run_local) else pipes.quote(SHA_TOOL_PATH)
        return "{} {} > {}".format(prefix, file_path, checksum_file_path)

    def create_checksum_cmd(self, file_path, checksum_file_path, run_local=False):
        return self.create_checksum_cmd_not_quoted(
            pipes.quote(file_path), pipes.quote(checksum_file_path), run_local=run_local)

    def create_checksum_cmd_for_dir(self, dir_path, run_local=False):
        return self.create_checksum_cmd_not_quoted(
            os.path.join(pipes.quote(strip_dir(dir_path)), '[!i]*'),
            pipes.quote(checksum_path(strip_dir(dir_path))), run_local=run_local)

    def prepare_upload_command(self, parallel_commands, snapshot_filepath, tablet_id,
                               tserver_ip, snapshot_dir):
        """
        Prepares the command to upload the backup files to backup location from the tservers.

        :param parallel_commands: result parallel commands to run.
        :param snapshot_filepath: Filepath/cloud url where the backup must be stored.
        :param tablet_id: tablet_id for the tablet whose data we would like to upload.
        :param tserver_ip: tserver ip from which the data needs to be uploaded.
        :param snapshot_dir: The snapshot directory on the tserver from which we need to upload.
        """
        target_tablet_filepath = os.path.join(snapshot_filepath, 'tablet-%s' % (tablet_id))
        if not self.args.disable_checksums:
            logging.info('Creating check-sum for %s on tablet server %s' % (
                         snapshot_dir, tserver_ip))
            create_checksum_cmd = self.create_checksum_cmd_for_dir(snapshot_dir, run_local=False)

            target_checksum_filepath = checksum_path(target_tablet_filepath)
            snapshot_dir_checksum = checksum_path(strip_dir(snapshot_dir))
            logging.info('Uploading %s from tablet server %s to %s URL %s' % (
                         snapshot_dir_checksum, tserver_ip, self.args.storage_type,
                         target_checksum_filepath))
            upload_checksum_cmd = self.storage.upload_file_cmd(
                snapshot_dir_checksum, target_checksum_filepath)

        target_filepath = target_tablet_filepath + '/'
        logging.info('Uploading %s from tablet server %s to %s URL %s' % (
                     snapshot_dir, tserver_ip, self.args.storage_type, target_filepath))

        # Commands to be run on TSes over ssh for uploading the tablet backup.
        if not self.args.disable_checksums:
            # 1. Create check-sum file (via sha256sum tool).
            parallel_commands.add_args(create_checksum_cmd, tserver_ip)
            # 2. Upload check-sum file.
            parallel_commands.add_args(tuple(upload_checksum_cmd), tserver_ip)

        if not 'DIRECTORY' in self.manifest_class.storage_tablet_ids[tablet_id].keys():
            if self.args.verbose:
                logging.info("\n\nUploading files\n\n")
        # **********************************
        # Actions: move or copy or ignore
        # 1 - copy  2 - move 3 - noop.
        # **********************************
            for file in self.manifest_class.storage_tablet_ids[tablet_id]:
                target_filename = os.path.join(target_filepath, file)
                if self.manifest_class.storage_tablet_ids[tablet_id][file]["action"] == ACTION_NOOP:
                    pass
                else:
                    if self.manifest_class.storage_tablet_ids[tablet_id][file]["action"] == ACTION_COPY:
                        upload_file_cmd = self.storage.upload_file_cmd(self.manifest_class.storage_tablet_ids[tablet_id][file]["src_location"], target_filepath)
                        parallel_commands.add_args(tuple(upload_file_cmd), tserver_ip)
                    else:
                        if self.manifest_class.storage_tablet_ids[tablet_id][file]["action"] == ACTION_MOVE:
                            upload_file_cmd = self.storage.move_obj_cmd(self.manifest_class.storage_tablet_ids[tablet_id][file]["src_location"], target_filepath)
                    parallel_commands.add_args(tuple(upload_file_cmd), tserver_ip)
                    self.manifest_class.storage_tablet_ids[tablet_id][file]["src_location"] = copy.deepcopy(target_filename)
        else:
            # 3. Upload tablet folder.
            if self.args.verbose:
                logging.info("\n\nUploading directories\n\n")
            upload_tablet_cmd = self.storage.upload_dir_cmd(snapshot_dir, target_filepath)
            parallel_commands.add_args(tuple(upload_tablet_cmd), tserver_ip)
            for file in self.manifest_class.storage_tablet_ids[tablet_id]:
                target_filename = os.path.join(target_filepath, file)
                self.manifest_class.storage_tablet_ids[tablet_id][file]["src_location"]= copy.deepcopy(target_filename)
            del self.manifest_class.storage_tablet_ids[tablet_id]['DIRECTORY']


    def prepare_download_command(self, parallel_commands, snapshot_filepath, tablet_id,
                                 tserver_ip, snapshot_dir, snapshot_metadata, restore_mode_file):
        """
        Prepares the command to download the backup files to the tservers.

        :param parallel_commands: result parallel commands to run.
        :param snapshot_filepath: Filepath/cloud url where the backup is stored.
        :param tablet_id: tablet_id for the tablet whose data we would like to download.
        :param tserver_ip: tserver ip from which the data needs to be downloaded.
        :param snapshot_dir: The snapshot directory on the tserver to which we need to download.
        """
        if tablet_id not in snapshot_metadata['tablet']:
            raise BackupException('Could not find metadata for tablet id {}'.format(tablet_id))

        old_tablet_id = snapshot_metadata['tablet'][tablet_id]
        source_filepath = os.path.join(snapshot_filepath, 'tablet-%s/' % (old_tablet_id))
        snapshot_dir_tmp = strip_dir(snapshot_dir) + '.tmp/'

        source_checksum_filepath = checksum_path(
            os.path.join(snapshot_filepath, 'tablet-%s' % (old_tablet_id)))
        snapshot_dir_checksum = checksum_path_downloaded(strip_dir(snapshot_dir))
        cmd_checksum = self.storage.download_file_cmd(
            source_checksum_filepath, snapshot_dir_checksum)

        create_checksum_cmd = self.create_checksum_cmd_for_dir(snapshot_dir_tmp)
        check_checksum_cmd = compare_checksums_cmd(
            snapshot_dir_checksum, checksum_path(strip_dir(snapshot_dir_tmp)))

        rmcmd = ['rm', '-rf', snapshot_dir]
        mkdircmd = ['mkdir', '-p', snapshot_dir_tmp]
        mvcmd = ['mv', snapshot_dir_tmp, snapshot_dir]

        # Commands to be run over ssh for downloading the tablet backup.
        # 1. Clean-up: delete target tablet folder.
        parallel_commands.add_args(tuple(rmcmd), tserver_ip)
        # 2. Create temporary snapshot dir.
        parallel_commands.add_args(tuple(mkdircmd), tserver_ip)
        if restore_mode_file:
            for file in self.prev_manifest_class.storage_tablet_ids[old_tablet_id]:
                target_filename = os.path.join(snapshot_dir_tmp, file)
                download_file_cmd = self.storage.download_file_cmd(self.prev_manifest_class.storage_tablet_ids[old_tablet_id][file]['src_location'], 
                                                                   target_filename)
                parallel_commands.add_args(tuple(download_file_cmd), tserver_ip)
        else:
            logging.info('Downloading %s from %s to %s on tablet server %s' % (source_filepath,
                     self.args.storage_type, snapshot_dir_tmp, tserver_ip))
            # Download the data to a tmp directory and then move it in place.
            cmd = self.storage.download_dir_cmd(source_filepath, snapshot_dir_tmp)
            # 3. Download tablet folder.
            parallel_commands.add_args(tuple(cmd), tserver_ip)
        if not self.args.disable_checksums:
            # 4. Download check-sum file.
            parallel_commands.add_args(tuple(cmd_checksum), tserver_ip)
            # 5. Create new check-sum file.
            parallel_commands.add_args(create_checksum_cmd, tserver_ip)
            # 6. Compare check-sum files.
            parallel_commands.add_args_and_save_result(check_checksum_cmd, tserver_ip)
        # 7. Move the backup in place.
        parallel_commands.add_args(tuple(mvcmd), tserver_ip)

    def prepare_cloud_ssh_cmds(
            self, parallel_commands, tserver_ip_to_tablet_id_to_snapshot_dirs, snapshot_filepath,
            snapshot_id, tablets_by_tserver_ip, upload, snapshot_metadata, restore_mode_file=False):
        """
        Prepares cloud_command-over-ssh command lines for uploading the snapshot.

        :param parallel_commands: result parallel commands to run.
        :param tserver_ip_to_tablet_id_to_snapshot_dirs: the three-level map as returned by
            rearrange_snapshot_dirs.
        :param snapshot_filepath: the top-level cloud URL to create snapshot directories under
        :param snapshot_id: the snapshot id we're dealing with
        :param tablets_by_tserver_ip: a map from tserver ip to all tablet ids that tserver is the
            responsible for.
        :param upload: True if we are uploading files to cloud, false if we are downloading files
            from cloud.
        :param snapshot_metadata: In case of downloading files from cloud to restore a backup,
            this is the snapshot metadata stored in cloud for the backup.
        """
        tserver_ip_to_tablet_ids_with_data_dirs = {}
        for tserver_ip in tserver_ip_to_tablet_id_to_snapshot_dirs:
            tserver_ip_to_tablet_ids_with_data_dirs.setdefault(tserver_ip, set())

        tservers_processed = []
        while len(tserver_ip_to_tablet_id_to_snapshot_dirs) > len(tservers_processed):
            for tserver_ip in list(tserver_ip_to_tablet_id_to_snapshot_dirs):
                if tserver_ip not in tservers_processed:
                    tablet_id_to_snapshot_dirs =\
                        tserver_ip_to_tablet_id_to_snapshot_dirs[tserver_ip]
                    tablet_ids_with_data_dirs = tserver_ip_to_tablet_ids_with_data_dirs[tserver_ip]
                    if len(tablet_id_to_snapshot_dirs) > 0:
                        tablet_id = list(tablet_id_to_snapshot_dirs)[0]
                        snapshot_dirs = tablet_id_to_snapshot_dirs[tablet_id]

                        if len(snapshot_dirs) > 1:
                            raise BackupException(
                                ('Found multiple snapshot directories on tserver {} for snapshot '
                                 'id {}: {}').format(tserver_ip, snapshot_id, snapshot_dirs))

                        assert len(snapshot_dirs) == 1
                        snapshot_dir = list(snapshot_dirs)[0] + '/'
                        parallel_commands.start_command()

                        if upload:
                            self.prepare_upload_command(
                                parallel_commands, snapshot_filepath, tablet_id, tserver_ip,
                                snapshot_dir)
                        else:
                            self.prepare_download_command(
                                parallel_commands, snapshot_filepath, tablet_id, tserver_ip,
                                snapshot_dir, snapshot_metadata, restore_mode_file)

                        tablet_ids_with_data_dirs.add(tablet_id)
                        tablet_id_to_snapshot_dirs.pop(tablet_id)

                        if len(tablet_id_to_snapshot_dirs) == 0:
                            tservers_processed += [tserver_ip]

                            if tablet_ids_with_data_dirs != tablets_by_tserver_ip[tserver_ip]:
                                for possible_tablet_id in tablets_by_tserver_ip[tserver_ip]:
                                    if possible_tablet_id not in tablet_ids_with_data_dirs:
                                        logging.error(
                                            ("No snapshot directory found for tablet id '{}' on "
                                                "tablet server '{}'.").format(
                                                    possible_tablet_id, tserver_ip))
                                raise BackupException("Did not find snapshot directories for some "
                                                      + "tablets on tablet server " + tserver_ip)
                    else:
                        tservers_processed += [tserver_ip]


    def get_tmp_dir(self):
        if not self.tmp_dir_name:
            tmp_dir = '/tmp/yb_backup_' + random_string(16)
            atexit.register(self.cleanup_temporary_directory, tmp_dir)
            self.run_program(['mkdir', '-p', tmp_dir])
            self.tmp_dir_name = tmp_dir

        return self.tmp_dir_name

    def upload_encryption_key_file(self):
        key_file = os.path.basename(self.args.backup_keys_source)
        key_file_dest = os.path.join("/".join(self.args.backup_location.split("/")[:-1]), key_file)
        self.run_program(self.storage.upload_file_cmd(self.args.backup_keys_source, key_file_dest))
        self.run_program(["rm", self.args.backup_keys_source])

    def download_encryption_key_file(self):
        key_file = os.path.basename(self.args.restore_keys_destination)
        key_file_src = os.path.join("/".join(self.args.backup_location.split("/")[:-1]), key_file)
        self.run_program(
            self.storage.download_file_cmd(key_file_src, self.args.restore_keys_destination)
        )

    def delete_bucket_obj(self):
        del_cmd = self.storage.delete_obj_cmd(self.args.backup_location)
        if self.is_nfs:
            self.run_ssh_cmd(del_cmd, self.get_leader_master_ip())
        else:
            self.run_program(del_cmd)

    def find_nfs_storage(self, tserver_ip):
        """
        Finds the NFS storage path mounted on the given tserver.
        if we don't find storage path mounted on given tserver IP we
        raise exception
        :param tserver_ip: tablet server ip
        """
        try:
            self.run_ssh_cmd(['ls', self.args.nfs_storage_path], tserver_ip)
        except Exception:
            raise BackupException(
                ('Did not find nfs backup storage path: %s mounted on tablet server %s'
                 % (self.args.nfs_storage_path, tserver_ip)))

    def upload_file(self,src_path, dest_path):
        self.upload_metadata_and_checksum(src_path, dest_path)

    def upload_metadata_and_checksum(self, src_path, dest_path, run_local=False):
        """
        Upload metadata file and checksum file to the target backup location.
        :param src_path: local metadata file path
        :param dest_path: destination metadata file path
        """
        src_checksum_path = checksum_path(src_path)
        dest_checksum_path = checksum_path(dest_path)

        if self.args.local_yb_admin_binary or run_local:
            if not os.path.exists(src_path):
                raise BackupException(
                    "Could not find metadata file at '{}'".format(src_path))

            if not self.args.disable_checksums:
                logging.info('Creating check-sum for %s' % (src_path))
                self.run_program([ 'bash','-c',
                    self.create_checksum_cmd(src_path, src_checksum_path, run_local=run_local)])

                logging.info('Uploading %s to %s' % (src_checksum_path, dest_checksum_path))
                self.run_program(
                    self.storage.upload_file_cmd(src_checksum_path, dest_checksum_path))

            logging.info('Uploading %s to %s' % (src_path, dest_path))
            self.run_program(
                self.storage.upload_file_cmd(src_path, dest_path))
        else:
            server_ip = self.get_main_host_ip()

            if not self.args.disable_checksums:
                logging.info('Creating check-sum for %s on tablet server %s' % (
                             src_path, server_ip))
                self.run_ssh_cmd(
                    self.create_checksum_cmd(src_path, src_checksum_path),
                    server_ip)

                logging.info('Uploading %s from tablet server %s to %s URL %s' % (
                             src_checksum_path, server_ip,
                             self.args.storage_type, dest_checksum_path))
                self.run_ssh_cmd(
                    self.storage.upload_file_cmd(src_checksum_path, dest_checksum_path),
                    server_ip)

            logging.info('Uploading %s from tablet server %s to %s URL %s' % (
                         src_path, server_ip, self.args.storage_type, dest_path))
            self.run_ssh_cmd(
                self.storage.upload_file_cmd(src_path, dest_path),
                server_ip)

    def get_ysql_catalog_version(self):
        """
        Get current YSQL Catalog version.
        :return: YSQL Catalog version
        """
        output = self.run_yb_admin(['ysql_catalog_version'])
        matched = YSQL_CATALOG_VERSION_RE.match(output)
        if not matched:
            raise BackupException(
                    "Couldn't parse ysql_catalog_version output! Expected "
                    "'Version: <number>' in the end: {}".format(output))
        return matched.group('version')

    def create_and_upload_metadata_files(self, snapshot_filepath):
        """
        Generates and uploads metadata files describing the given snapshot to the target
        backup location.
        :param snapshot_filepath: Backup directory under which to create a path
        :return: snapshot id
        """
        if self.args.snapshot_id:
            logging.info("Using existing snapshot ID: '{}'".format(self.args.snapshot_id))
            snapshot_id = self.args.snapshot_id

        if self.args.local_yb_admin_binary:
            self.run_program(['mkdir', '-p', self.get_tmp_dir()])
        else:
            self.create_remote_tmp_dir(self.get_main_host_ip())

        is_ysql = self.is_ysql_keyspace()
        if is_ysql:
            sql_dump_path = os.path.join(self.get_tmp_dir(), SQL_DUMP_FILE_NAME)
            db_name = keyspace_name(self.args.keyspace[0])
            start_version = self.get_ysql_catalog_version()

        stored_keyspaces = self.args.keyspace
        stored_tables = self.args.table
        stored_table_uuids = self.args.table_uuid
        num_retry = CREATE_METAFILES_MAX_RETRIES

        while num_retry > 0:
            num_retry = num_retry - 1

            if not self.args.snapshot_id:
                snapshot_id = self.create_snapshot()
                logging.info("Snapshot started with id: %s" % snapshot_id)
                # TODO: Remove the following try-catch for compatibility to un-relax the code, after
                #       we ensure nobody uses versions < v2.1.4 (after all move to >= v2.1.8).
                try:
                    # With 'update_table_list=True' it runs: 'yb-admin list_snapshots SHOW_DETAILS'
                    # to get updated list of backed up namespaces and tables. Note that the last
                    # argument 'SHOW_DETAILS' is not supported in old YB versions (< v2.1.4).
                    self.wait_for_snapshot(snapshot_id, 'creating', CREATE_SNAPSHOT_TIMEOUT_SEC,
                                           update_table_list=True)
                except CompatibilityException as ex:
                    logging.info("Ignoring the exception in the compatibility mode: {}".format(ex))
                    # In the compatibility mode repeat the command in old style
                    # (without the new command line argument 'SHOW_DETAILS').
                    # With 'update_table_list=False' it runs: 'yb-admin list_snapshots'.
                    self.wait_for_snapshot(snapshot_id, 'creating', CREATE_SNAPSHOT_TIMEOUT_SEC,
                                           update_table_list=False)

                if not self.args.no_snapshot_deleting:
                    logging.info("Snapshot %s will be deleted at exit...", snapshot_id)
                    atexit.register(self.delete_created_snapshot, snapshot_id)

            if is_ysql:
                logging.info("[app] Creating ysql dump for DB '{}' to {}".format(
                             db_name, sql_dump_path))
                self.run_ysql_dump(['--include-yb-metadata', '--serializable-deferrable',
                                    '--create', '--schema-only',
                                    '--dbname=' + db_name, '--file=' + sql_dump_path])

                final_version = self.get_ysql_catalog_version()
                logging.info('[app] YSQL catalog versions: {} - {}'.format(
                             start_version, final_version))
                if final_version == start_version:
                    break  # Ok. No table schema changes during meta data creating.
                else:
                    # wait_for_snapshot() can update the variables - restore them back.
                    self.args.keyspace = stored_keyspaces
                    self.args.table = stored_tables
                    self.args.table_uuid = stored_table_uuids

                    start_version = final_version
                    logging.info('[app] Retry creating metafiles ({} retries left)'.format(
                                 num_retry))
            else:
                break  # Ok. No need to retry for YCQL.

        if num_retry == 0:
            raise BackupException("Couldn't create metafiles due to catalog changes")

        metadata_path = os.path.join(self.get_tmp_dir(), METADATA_FILE_NAME)
        logging.info('[app] Exporting snapshot {} to {}'.format(snapshot_id, metadata_path))
        self.run_yb_admin(['export_snapshot', snapshot_id, metadata_path],
                          run_ip=self.get_main_host_ip())
        self.upload_metadata_and_checksum(metadata_path,
                                          os.path.join(snapshot_filepath, METADATA_FILE_NAME))

        if is_ysql:
            self.upload_metadata_and_checksum(sql_dump_path,
                                              os.path.join(snapshot_filepath, SQL_DUMP_FILE_NAME))

        return snapshot_id


    def try_download_metadata(self, src, dest, raise_exception, run_local=False):
        try:
            self.download_file(src, dest, run_local=run_local)
        except subprocess.CalledProcessError as ex:
            if raise_exception:
                raise ex
            else:
                logging.info("Ignoring the exception in downloading of {}: {}".
                             format(src, ex))
                dest = None
        return dest

    def create_manifest(self, current_manifest, files, tablet_leaders):
        compare_set_curr = set()
        copy_set_curr = set()
        curr_manifest = dict()
        tablet_from_leader = set()
        for key, value in files.items():
            tserver = key[2]
            tablet_from_leader.clear()
            for leader_tablet in tablet_leaders:
                if leader_tablet[1] == tserver:
                    tablet_from_leader.add(leader_tablet[0])
            for filename in value:
                fields = filename.split("/")
                generation = 1
                tablet = fields[-3].split("-")[1].split(".")[0]
                # todo(zdrudi): this comparison is brittle. find a better way to do this.
                if not tablet in tablet_from_leader:
                    continue
                file = fields[-1]
                dict_key =(tablet + "/" + file)
                if not current_manifest.storage_tablet_ids:
                    current_manifest.storage_tablet_ids = dict()
                curr_manifest[dict_key] = \
                    {"filename": file, "generation": generation, "src_location": filename}
                if file.find(".sst") != -1:
                    compare_set_curr.add(dict_key)
                else:
                    copy_set_curr.add(dict_key)
        return (curr_manifest, compare_set_curr, copy_set_curr)

    def get_manifest(self, dest_path, manifest_file, manifest):
        prev_disable_checksums = self.args.disable_checksums
        self.args.disable_checksums = True
        try:
            self.download_file(manifest_file, dest_path, run_local=True)
            with open(dest_path, 'r') as fp:
                json_dict = json.load(fp)
            manifest.storage_tablet_ids = json_dict['manifest']['storage']['tablet_ids']
            if 'manifest_previous' in json_dict['manifest']['metadata']:
                manifest.manifest_previous = json_dict['manifest']['metadata']['manifest_previous']
            else:
                manifest.manifest_previous = ''
            result = True
        except Exception:
             result =  False
             logging.info("Failed to get manifest {}".format(manifest_file))
        finally:
            self.args.disable_checksums = prev_disable_checksums
        return result

    def update_manifest_from_local_file(self, manifest_file, manifest):
        with open(manifest_file, 'r') as fp:
            manifest_dict = json.load(fp)
            manifest.update_storage_tablet_ids(manifest_dict)

    def write_manifest(self, manifest_file, manifest_class):
        with open(manifest_file, 'w') as fp:
            json.dump(manifest_class.to_json_dict(), fp)

    def backup_table(self):
        """
        Creates a backup of the given table by creating a snapshot and uploading it to the provided
        backup location.
        """
        if not self.args.keyspace:
            raise BackupException('Need to specify --keyspace')

        if self.args.table:
            if self.is_ysql_keyspace():
                raise BackupException(
                    "Back up for YSQL is only supported at the database level, "
                    "and not at the table level.")

            logging.info('[app] Backing up tables: {} to {}'.format(
                         self.table_names_str(), self.args.backup_location))
        else:
            if len(self.args.keyspace) != 1:
                raise BackupException(
                    "Only one keyspace supported. Found {} --keyspace keys.".
                    format(len(self.args.keyspace)))

            logging.info('[app] Backing up keyspace: {} to {}'.format(
                         self.args.keyspace[0], self.args.backup_location))

        if self.args.no_auto_name:
            snapshot_filepath = self.args.backup_location
        else:
            if self.args.table:
                snapshot_bucket = 'table-{}'.format(self.table_names_str('.', '-'))
            else:
                snapshot_bucket = 'keyspace-{}'.format(self.args.keyspace[0])

            if self.args.table_uuid:
                if len(self.args.table) != len(self.args.table_uuid):
                    raise BackupException(
                        "Found {} --table_uuid keys and {} --table keys. Number of these keys "
                        "must be equal.".format(len(self.args.table_uuid), len(self.args.table)))

                snapshot_bucket = '{}-{}'.format(snapshot_bucket, '-'.join(self.args.table_uuid))

            snapshot_filepath = os.path.join(self.args.backup_location, snapshot_bucket)

        self.timer.log_new_phase("Create and upload snapshot metadata")
        snapshot_id = self.create_and_upload_metadata_files(snapshot_filepath)
        self.timer.log_new_phase("Find tablet leaders")
        tablet_leaders = self.find_tablet_leaders()

        self.timer.log_new_phase("Get snapshot files and create manifest")
        # Get a list of all the files to be differentially backed up
        files = self.get_filelist(tablet_leaders, snapshot_id)

        # Build the current manifest
        (curr_manifest, compare_set_curr, copy_set_curr) = self.create_manifest(self.manifest_class, files, tablet_leaders)
        self.manifest_class.manifest_location = self.args.backup_location
        if self.args.verbose:
            logging.info("Current manifest {}:  ".format(
                curr_manifest))

        self.timer.log_new_phase("Get the previous manifest for differential backup")
        is_differential_backup = self.is_differential_backup()
        if self.args.prev_manifest_source and is_differential_backup:
            prev_manifestfile = os.path.join(self.args.prev_manifest_source, MANIFEST)
            dest_path = os.path.join(self.get_tmp_dir(), MANIFEST)
            if self.get_manifest(dest_path, prev_manifestfile, self.prev_manifest_class):
                is_differential_backup = True
                self.manifest_class.manifest_previous = self.args.prev_manifest_source
            else:
                is_differential_backup = False
                logging.info("Previous manifest " + dest_path + "  not found or could not be loaded, proceeding with full backup.")

        for key in tablet_leaders:
            self.manifest_class.storage_tablet_ids.setdefault(key[0], {})

        final_manifest = dict()
        write_previous_manifests = False
        if is_differential_backup:
            self.timer.log_new_phase("Run differential backup")
            compare_set_prev = set()

            restore_point_manifests = dict()
            restore_point_manifests[0] = copy.deepcopy(self.prev_manifest_class)
            restore_point_manifests[0].manifest_location = self.args.prev_manifest_source
            # load number of restore points previous_manifests
            for num_manifests in range(1, self.args.restore_points):
                restore_point_manifests[num_manifests] = Manifest(uuid.uuid1())
                manifest_location = restore_point_manifests[num_manifests - 1].manifest_previous
                if not manifest_location:
                    break
                prev_manifestfile = os.path.join(manifest_location, MANIFEST)
                if self.get_manifest(dest_path, prev_manifestfile, restore_point_manifests[num_manifests]):
                    restore_point_manifests[num_manifests].manifest_location = manifest_location

            prev_manifest = dict()
            for tablet in self.prev_manifest_class.storage_tablet_ids:
                for file in self.prev_manifest_class.storage_tablet_ids[tablet]:
                    prev_manifest[tablet + "/" + file] = self.prev_manifest_class.storage_tablet_ids[tablet][file]

            if self.args.verbose:
                logging.info('Previous manifest\n{}'.format(prev_manifest))

            # Create the set of files to copy and compare from the previous backup
            for key in prev_manifest.keys():
                if prev_manifest[key]["filename"].find(".sst") != -1:
                    compare_set_prev.add(key)

            files_in_both = compare_set_curr & compare_set_prev
            files_in_curr = compare_set_curr - compare_set_prev

            if self.args.verbose:
                files_in_prev = compare_set_prev - compare_set_curr
                logging.info(
                   "\n\n\nSets: \nfiles_in_both_backups {}\nfiles_in_prev {}\nfiles_in_curr {}\n\n\n".format(
                        files_in_both, files_in_prev, files_in_curr  ))

            # Copy new files off-cluster
            for key in files_in_curr:
                final_manifest[key] = curr_manifest[key]
                final_manifest[key]["action"] = ACTION_COPY
                tablet = key.split("/")[0]
                file = final_manifest[key]["filename"]
                self.manifest_class.storage_tablet_ids[tablet][file] = final_manifest[key]

            # Copy files that are always copied
            for key in copy_set_curr:
                final_manifest[key] = curr_manifest[key]
                final_manifest[key]["action"] = ACTION_COPY
                tablet = key.split("/")[0]
                file = final_manifest[key]["filename"]
                self.manifest_class.storage_tablet_ids[tablet][file] = final_manifest[key]

            # Update the current manifest with the previous information files
            for key in files_in_both:
                final_manifest[key] = prev_manifest[key]
                if self.args.restore_points <= prev_manifest[key]["generation"]:
                    write_previous_manifests = True
                    tablet = key.split("/")[0]
                    filename = key.split("/")[1]
                    final_manifest[key]["action"] = ACTION_MOVE
                    final_manifest[key]["generation"] = 1
                    # reset generation and location for restore_point manifests
                    for manifest in restore_point_manifests.values():
                        if (manifest.storage_tablet_ids.get(tablet) and manifest.storage_tablet_ids[tablet].get(filename)):
                            manifest.storage_tablet_ids[tablet][filename]['generation'] = self.args.restore_points - 1
                            manifest.storage_tablet_ids[tablet][filename]['src_location'] = self.get_upload_file_path(
                                self.args.backup_location, tablet, filename)
                else:
                    final_manifest[key]["action"] = ACTION_NOOP
                    final_manifest[key]["generation"] = prev_manifest[key]["generation"] + 1
                tablet = key.split("/")[0]
                file = final_manifest[key]["filename"]
                self.manifest_class.storage_tablet_ids[tablet][file] = final_manifest[key]

            for key in final_manifest:
                fields = key.split("/")
                tablet = fields[0]
                file = final_manifest[key]["filename"]
                self.manifest_class.storage_tablet_ids[tablet][file] = final_manifest[key]

            # If there are only new files, copy directories instead of individual files
            # if files_in_curr and not files_in_both:
            if files_in_curr and not files_in_both:
                for tablet in self.manifest_class.storage_tablet_ids:
                    self.manifest_class.storage_tablet_ids[tablet]['DIRECTORY'] = {}


            self.manifest_class.storage_tablet_ids = copy.deepcopy(self.manifest_class.storage_tablet_ids)


        else:
            self.timer.log_new_phase("Run full backup")
            for key in self.manifest_class.storage_tablet_ids:
                self.manifest_class.storage_tablet_ids[key]['DIRECTORY'] = {}
            for key in curr_manifest:
                fields = key.split("/")
                tablet = fields[0]
                file = curr_manifest[key]["filename"]
                self.manifest_class.storage_tablet_ids[tablet][file] = curr_manifest[key]
                self.manifest_class.storage_tablet_ids[tablet][file]["action"] = ACTION_COPY

            self.manifest_class.storage_tablet_ids = copy.deepcopy(self.manifest_class.storage_tablet_ids)

        self.timer.log_new_phase("Upload snapshot directories")
        self.upload_snapshot_directories(tablet_leaders, snapshot_id, snapshot_filepath)
        logging.info(
            '[app] Backed up tables %s to %s successfully!' %
            (self.table_names_str(), snapshot_filepath))

        if write_previous_manifests:
            manifestfile = os.path.join(self.get_tmp_dir(), MANIFEST)
            for index in restore_point_manifests.keys():
                self.write_manifest(manifestfile, restore_point_manifests[index])
                manifest_source = restore_point_manifests[index].manifest_location
                manifest_dest = os.path.join(manifest_source, MANIFEST)
                self.upload_metadata_and_checksum(manifestfile, manifest_dest, run_local=True)

        manifestfile = os.path.join(self.get_tmp_dir(), MANIFEST)
        manifest_dest = os.path.join(self.manifest_class.manifest_location, MANIFEST)
        self.write_manifest(manifestfile, self.manifest_class)
        self.upload_metadata_and_checksum(manifestfile, manifest_dest, run_local=True)

        if self.args.backup_keys_source:
            self.upload_encryption_key_file()

    def download_file(self, src_path, target_path, run_local=False):
        """
        Download the file from the external source to the local temporary folder.
        """
        if self.args.local_yb_admin_binary or run_local:
            if not self.args.disable_checksums:
                checksum_downloaded = checksum_path_downloaded(target_path)
                self.run_program(
                    self.storage.download_file_cmd(checksum_path(src_path), checksum_downloaded))
            self.run_program(
                self.storage.download_file_cmd(src_path, target_path))

            if not self.args.disable_checksums:
                self.run_program(['bash','-c',
                    self.create_checksum_cmd(target_path, checksum_path(target_path), run_local=run_local)])
                check_checksum_res = self.run_program(['bash', '-c',
                                      compare_checksums_cmd(checksum_downloaded,
                                          checksum_path(target_path))]).strip()
        else:
            server_ip = self.get_main_host_ip()

            if not self.args.disable_checksums:
                checksum_downloaded = checksum_path_downloaded(target_path)
                self.run_ssh_cmd(
                    self.storage.download_file_cmd(checksum_path(src_path), checksum_downloaded),
                    server_ip)
            self.run_ssh_cmd(
                self.storage.download_file_cmd(src_path, target_path),
                server_ip)

            if not self.args.disable_checksums:
                self.run_ssh_cmd(
                    self.create_checksum_cmd(target_path, checksum_path(target_path), run_local=run_local),
                    server_ip)
                check_checksum_res = self.run_ssh_cmd(
                    compare_checksums_cmd(checksum_downloaded, checksum_path(target_path)),
                    server_ip).strip()

        if (not self.args.disable_checksums) and check_checksum_res != 'correct':
            raise BackupException('Check-sum for {} is {}'.format(
                target_path, check_checksum_res))

        logging.info(
            'Downloaded metadata file %s from %s' % (target_path, src_path))

    def download_metadata_file(self, run_local=False):
        """
        Download the metadata file for a backup so as to perform a restore based on it.
        """

        if self.args.local_yb_admin_binary or run_local:
            self.run_program(['mkdir', '-p', self.get_tmp_dir()])
        else:
            self.create_remote_tmp_dir(self.get_main_host_ip())

        src_metadata_path = os.path.join(self.args.backup_location, METADATA_FILE_NAME)
        metadata_path = os.path.join(self.get_tmp_dir(), METADATA_FILE_NAME)
        self.download_file(src_metadata_path, metadata_path)
        if self.is_ysql_keyspace():
           src_sql_dump_path = os.path.join(self.args.backup_location, SQL_DUMP_FILE_NAME)
           sql_dump_path = os.path.join(self.get_tmp_dir(), SQL_DUMP_FILE_NAME)
           sql_dump_path = self.try_download_metadata(src_sql_dump_path, sql_dump_path, self.is_ysql_keyspace())
        else:
           sql_dump_path = None
        src_manifest_dump_path = os.path.join(self.args.backup_location, MANIFEST)
        manifest_dump_path = os.path.join(self.get_tmp_dir(), MANIFEST)
        manifest_dump_path = self.try_download_metadata(src_manifest_dump_path,
                                                        manifest_dump_path,
                                                        False,
                                                        run_local=True)
        return (metadata_path, sql_dump_path, manifest_dump_path)

    def import_ysql_dump(self, dump_file_path):
        """
        Import the YSQL dump using the provided file.
        """
        if self.args.keyspace:
            cmd = get_db_name_cmd(dump_file_path)

            if self.args.local_yb_admin_binary:
                old_db_name = self.run_program(cmd).strip()
            else:
                old_db_name = self.run_ssh_cmd(cmd, self.get_main_host_ip()).strip()

            new_db_name = keyspace_name(self.args.keyspace[0])
            logging.info("[app] Renaming YSQL DB from '{}' into '{}'".format(
                         old_db_name, new_db_name))
            cmd = replace_db_name_cmd(dump_file_path, old_db_name, new_db_name)

            if self.args.local_yb_admin_binary:
                self.run_program(cmd)
            else:
                self.run_ssh_cmd(cmd, self.get_main_host_ip())

        self.run_ysql_shell(['--echo-all', '--file=' + dump_file_path])

    def import_snapshot(self, metadata_file_path):
        """
        Import the snapshot metadata using the provided metadata file, process the metadata for
        the imported snapshot and return the snapshot metadata. The snapshot metadata returned is a
        map containing all the metadata for the snapshot and mappings from old ids to new ids for
        table, keyspace, tablets and snapshot.
        """
        yb_admin_args = ['import_snapshot', metadata_file_path]

        if self.args.keyspace:
            yb_admin_args += [self.args.keyspace[0]]

        if self.args.table:
            yb_admin_args += [' '.join(self.args.table)]

        output = self.run_yb_admin(yb_admin_args, run_ip=self.get_main_host_ip())

        snapshot_metadata = {}
        snapshot_metadata['keyspace_name'] = []
        snapshot_metadata['table_name'] = []
        snapshot_metadata['table'] = {}
        snapshot_metadata['tablet'] = {}
        snapshot_metadata['snapshot_id'] = {}
        for _, line in enumerate(output.splitlines()):
            table_match = IMPORTED_TABLE_RE.search(line)
            if table_match:
                snapshot_metadata['keyspace_name'].append(table_match.group(1))
                snapshot_metadata['table_name'].append(table_match.group(2))
                logging.info('Imported table: {}.{}'.format(table_match.group(1),
                                                            table_match.group(2)))
            elif NEW_OLD_UUID_RE.search(line):
                (entity, old_id, new_id) = split_by_tab(line)
                if entity == 'Table':
                    snapshot_metadata['table'][new_id] = old_id
                    logging.info('Imported table id was changed from {} to {}'.format(old_id,
                                                                                      new_id))
                elif entity.startswith('Tablet'):
                    snapshot_metadata['tablet'][new_id] = old_id
                elif entity == 'Snapshot':
                    snapshot_metadata['snapshot_id']['old'] = old_id
                    snapshot_metadata['snapshot_id']['new'] = new_id
            elif COLOCATED_NEW_OLD_UUID_RE.search(line):
                (entity, old_id, new_id) = split_by_tab(line)
                if entity == 'ParentColocatedTable':
                    verify_colocated_table_ids(old_id, new_id)
                    snapshot_metadata['table'][new_id] = old_id
                    logging.info('Imported colocated table id was changed from {} to {}'
                                 .format(old_id, new_id))
                elif entity == 'ColocatedTable':
                    # A colocated table's tablets are kept under its corresponding parent colocated
                    # table, so we just need to verify the table ids now.
                    verify_colocated_table_ids(old_id, new_id)
                    logging.info('Imported colocated table id was changed from {} to {}'
                                 .format(old_id, new_id))

        return snapshot_metadata

    def find_tablet_replicas(self, snapshot_metadata):
        """
        Finds the tablet replicas for tablets present in snapshot_metadata and returns a list of all
        tservers that need to be processed.
        """

        tablets_by_tserver_ip = {}
        for new_id in snapshot_metadata['tablet']:
            output = self.run_yb_admin(['list_tablet_servers', new_id])
            for line in output.splitlines():
                if LEADING_UUID_RE.match(line):
                    _, ts_ip_port, _ = split_by_tab(line)
                    ts_ip, _ = ts_ip_port.split(':')
                    tablets_by_tserver_ip.setdefault(ts_ip, set()).add(new_id)

        return tablets_by_tserver_ip

    def identify_new_tablet_replicas(self, tablets_by_tserver_ip_old, tablets_by_tserver_ip_new):
        """
        Compare old and new sets of tablets per every TServer, find and return difference.
        Returns union of the sets per TServer, and delta of the sets.
        """

        tablets_by_tserver_union = copy.deepcopy(tablets_by_tserver_ip_old)
        tablets_by_tserver_delta = {}

        for ip in tablets_by_tserver_ip_new:
            tablets = tablets_by_tserver_ip_new[ip]
            if ip in tablets_by_tserver_ip_old:
                if not (tablets_by_tserver_ip_old[ip] >= tablets):
                    tablets_by_tserver_union[ip].update(tablets)
                    tablets_by_tserver_delta[ip] = tablets - tablets_by_tserver_ip_old[ip]
            else:
                tablets_by_tserver_union[ip] = tablets
                tablets_by_tserver_delta[ip] = tablets

        return (tablets_by_tserver_union, tablets_by_tserver_delta)

    def download_snapshot_directories(self, snapshot_meta, tablets_by_tserver_to_download,
                                      snapshot_id, table_ids, restore_mode_file):

        self.timer.log_new_phase("Find all table/tablet data dirs on all tservers")
        tserver_ips = list(tablets_by_tserver_to_download.keys())
        data_dir_by_tserver = SingleArgParallelCmd(self.find_data_dirs, tserver_ips).run(self.pool)

        if self.args.verbose:
            logging.info('Found data directories: {}'.format(data_dir_by_tserver))

        (tserver_to_tablet_to_snapshot_dirs, tserver_to_deleted_tablets) =\
            self.generate_snapshot_dirs(
                data_dir_by_tserver, snapshot_id, tablets_by_tserver_to_download, table_ids)

        # Remove deleted tablets from the list of planned to be downloaded tablets.
        for tserver_ip in tserver_to_deleted_tablets:
            deleted_tablets = tserver_to_deleted_tablets[tserver_ip]
            tablets_by_tserver_to_download[tserver_ip] -= deleted_tablets

        self.timer.log_new_phase("Download data")
        parallel_downloads = SequencedParallelCmd(self.run_ssh_cmd)
        self.prepare_cloud_ssh_cmds(
            parallel_downloads, tserver_to_tablet_to_snapshot_dirs, self.args.backup_location,
            snapshot_id, tablets_by_tserver_to_download, upload=False,
            snapshot_metadata=snapshot_meta, restore_mode_file=restore_mode_file)

        # Run a sequence of steps for each tablet, handling different tablets in parallel.
        results = parallel_downloads.run(self.pool)

        if not self.args.disable_checksums:
            for k, v_raw in results.items():
                v = v_raw.strip()
                if v != 'correct':
                    raise BackupException('Check-sum for "{}" is {}'.format(k, v))

        return tserver_to_deleted_tablets


    def diff_project_tablets_from_manifest(self, src_location_dict, tserver_ip):
      if tserver_ip == '':
        tserver_ip = self.args.masters

      restore_tserver_tablets = {}
      list_files = []
      for tablet_id in src_location_dict['manifest']['storage']['tablet_ids'].keys():
        for src in src_location_dict['manifest']['storage']['tablet_ids'][tablet_id]:
          src_file_info = src_location_dict['manifest']['storage']['tablet_ids'][tablet_id][src]
          list_files.append(src_file_info)
        next_tablet = {'tablet-' + tablet_id: list_files}
        restore_tserver_tablets.update(next_tablet)

      tserver_tablets = {tserver_ip: restore_tserver_tablets}

      return (tserver_tablets)

    def restore_table(self):
        """
        Restore a table from the backup stored in the given backup path.
        """
        if self.args.keyspace:
            if len(self.args.keyspace) > 1:
                raise BackupException('Only one --keyspace expected for the restore mode.')
        elif self.args.table:
            raise BackupException('Need to specify --keyspace')

        # TODO (jhe): Perform verification for restore_time. Need to check for:
        #  - Verify that the timestamp given fits in the history retention window for the snapshot
        #  - Verify that we are restoring a keyspace/namespace (no individual tables for pitr)

        logging.info('Restoring backup from {}'.format(self.args.backup_location))

        (metadata_file_path, dump_file_path, manifest_dump_path) = self.download_metadata_file()

        if dump_file_path:
            self.timer.log_new_phase("Create tables via YSQLDump")
            self.import_ysql_dump(dump_file_path)

        self.timer.log_new_phase("Import snapshot")
        snapshot_metadata = self.import_snapshot(metadata_file_path)
        snapshot_id = snapshot_metadata['snapshot_id']['new']
        table_ids = list(snapshot_metadata['table'].keys())

        self.wait_for_snapshot(snapshot_id, 'importing', CREATE_SNAPSHOT_TIMEOUT_SEC, False)

        if not self.args.no_snapshot_deleting:
            logging.info("Snapshot %s will be deleted at exit...", snapshot_id)
            atexit.register(self.delete_created_snapshot, snapshot_id)

        self.timer.log_new_phase("Generate list of tservers for every tablet")
        all_tablets_by_tserver = self.find_tablet_replicas(snapshot_metadata)
        tablets_by_tserver_to_download = all_tablets_by_tserver

        #if diff Manifest setup for diff restore and structure for while loop
        restore_mode_file = False
        if manifest_dump_path:
            restore_mode_file = True
            self.update_manifest_from_local_file(manifest_dump_path, self.prev_manifest_class)

        # The loop must stop after a few rounds because the downloading list includes only new
        # tablets for downloading. The downloading list should become smaller with every round
        # and must become empty in the end.

        while tablets_by_tserver_to_download:
            logging.info(
                'Downloading tablets onto %d tservers...' % (len(tablets_by_tserver_to_download)))

            if self.args.verbose:
                logging.info('Downloading list: {}'.format(tablets_by_tserver_to_download))

            # Download tablets and get list of deleted tablets.
            tserver_to_deleted_tablets = self.download_snapshot_directories(
                snapshot_metadata, tablets_by_tserver_to_download, snapshot_id, table_ids, restore_mode_file)

            # Remove deleted tablets from the list of all tablets.
            for tserver_ip in tserver_to_deleted_tablets:
                deleted_tablets = tserver_to_deleted_tablets[tserver_ip]
                all_tablets_by_tserver[tserver_ip] -= deleted_tablets

            self.timer.log_new_phase("Regenerate list of tservers for every tablet")
            tablets_by_tserver_new = self.find_tablet_replicas(snapshot_metadata)
            # Calculate the new downloading list as a subtraction of sets:
            #     downloading_list = NEW_all_tablet_replicas - OLD_all_tablet_replicas
            # And extend the list of all tablets (as unioun of sets) for using it on the next
            # loop iteration:
            #     OLD_all_tablet_replicas = OLD_all_tablet_replicas + NEW_all_tablet_replicas
            #                             = OLD_all_tablet_replicas + downloading_list
            (all_tablets_by_tserver, tablets_by_tserver_to_download) =\
                self.identify_new_tablet_replicas(all_tablets_by_tserver, tablets_by_tserver_new)

        # Finally, restore the snapshot.
        logging.info('Downloading is finished. Restoring snapshot %s ...', snapshot_id)
        self.timer.log_new_phase("Restore the snapshot")

        restore_snapshot_args = ['restore_snapshot', snapshot_id]
        # Pass in the timestamp if provided.
        if self.args.restore_time:
            restore_snapshot_args.append(self.args.restore_time)

        output = self.run_yb_admin(restore_snapshot_args)

        # Transaction-aware snapshots use special restaration id with final state RESTORED,
        # while previous implementation uses snapshot id and it's state COMPLETE.
        restoration_id = snapshot_id
        complete_restoration_state = 'COMPLETE'
        for line in output.splitlines():
            restoration_match = RESTORATION_RE.match(line)
            if restoration_match:
                restoration_id = restoration_match.group(1)
                complete_restoration_state = 'RESTORED'
                logging.info('Found restoration id: ' + restoration_id)

        self.wait_for_snapshot(restoration_id, 'restoring', RESTORE_SNAPSHOT_TIMEOUT_SEC, False,
                               complete_restoration_state)

        logging.info('Restored backup successfully!')

    def delete_backup(self):
        """
        Delete the backup specified by the storage location.
        """
        if self.args.backup_location:
            self.delete_bucket_obj()
        logging.info('Deleted backup %s successfully!', self.args.backup_location)

    def restore_keys(self):
        """
        Restore universe keys from the backup stored in the given backup path.
        """
        if self.args.restore_keys_destination:
            self.download_encryption_key_file()

        logging.info('Restored backup universe keys successfully!')

    # At exit callbacks
    def cleanup_temporary_directory(self, tmp_dir):
        """
        Callback run on exit to clean up temporary directories.
        """
        if self.args.verbose:
            logging.info("Removing temporary directory '{}'".format(tmp_dir))

        self.run_program(['rm', '-rf', tmp_dir])

    def cleanup_remote_temporary_directory(self, server_ip, tmp_dir):
        """
        Callback run on exit to clean up temporary directories on remote host.
        """
        if self.args.verbose:
            logging.info("Removing remote temporary directory '{}' on {}".format(
                tmp_dir, server_ip))

        self.run_ssh_cmd(['rm', '-rf', tmp_dir], server_ip)

    def delete_created_snapshot(self, snapshot_id):
        """
        Callback run on exit to delete temporary newly created snapshot.
        """
        if self.args.verbose:
            logging.info("Deleting snapshot %s ...", snapshot_id)

        return self.run_yb_admin(['delete_snapshot', snapshot_id])


    def run(self):
        with ThreadPool(self.args.parallelism) as pool:
            self.pool = pool
            self.__run_internal()


    def update_history(self, run_exc=None):
        if not self.args.history_file:
            return
        try:
            with open(self.args.history_file, 'a', encoding='utf-8') as fp:
                payload = {"command": self.args.command,
                           "start_time": self.timer.logged_times[0],
                           "end_time": time.time(),
                           "status": "OK" if not run_exc else repr(run_exc),
                           "args": vars(self.args)}
                json.dump(payload, fp, indent=2)
        except Exception as ex:
            logging.error("Couldn't write to history file: %r", ex)


    def __run_internal(self):
        try:
            self.post_process_arguments()
            if self.args.command == 'restore':
                self.restore_table()
            elif self.args.command == 'create':
                self.backup_table()
            elif self.args.command == 'restore_keys':
                self.restore_keys()
            elif self.args.command == 'delete':
                self.delete_backup()
            elif self.args.command == 'create_diff':
                self.backup_table()
            else:
                msg = 'Command was not specified'
                logging.error(msg)
                raise BackupException(msg)
        except Exception as ex:
            self.update_history(ex)
            raise ex
        else:
            self.update_history()
        finally:
            self.timer.print_summary()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(filename)s %(lineno)d: %(message)s")
    YBBackup.create().run()
    print(json.dumps({"success": True}))
