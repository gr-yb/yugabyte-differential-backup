# Copyright 2022 YugaByte, Inc. and Contributors

# Licensed under the Polyform Free Trial License 1.0.0 (the "License"); you
# may not use this file except in compliance with the License. You
# may obtain a copy of the License at
#
# https://github.com/YugaByte/yugabyte-db/blob/master/licenses/POLYFORM-FREE-TRIAL-LICENSE-1.0.0.txt

import abc
import collections
import json
import inspect
import logging
import os
import os.path
import random
import string
import unittest

import cassandra.cluster
import cassandra.query
import psycopg2

import yb_backup_diff

def random_suffix(prefix, n):
    return prefix + ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

def get_caller_function_name():
    return inspect.currentframe().f_back.f_code.co_name

class PgConnector:
    def __init__(self, user, host, port, password=None, database=None):
        self.user = user
        self.host = host
        self.password = password
        self.port = port
        self.base_database = database if database is not None else "yugabyte"


    def connect(self, database=None):
        kwargs = {"database": database if database is not None else self.base_database,
                  "user": self.user,
                  "host": self.host,
                  "port": self.port}
        if self.password is not None:
            kwargs['password'] = self.password
        return psycopg2.connect(**kwargs)


BackupTestRun = collections.namedtuple('BackupTestRun', ['db_name', 'keyspace', 'full_location', 'diff_locations'])


class BackupDiffTest(abc.ABC):
    backup_runner = None
    ROWS_PER_BATCH = 10
    ARGS_FILE_PATH = "backup_args.json"
    DEFAULT_TABLE_NAME = "test_table"

    @classmethod
    def setUpClass(cls):
        cls.backup_runner = BackupRunner.initialize_from_file(cls.ARGS_FILE_PATH)
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(filename)s %(lineno)d: %(message)s",
                            filename=cls.log_file_name(),
                            filemode='w')

    @classmethod
    def make_test_data(cls, num_batches):
        batches = []
        for i in range(num_batches):
            batches.append({j + i * cls.ROWS_PER_BATCH:
                           str(j + i * cls.ROWS_PER_BATCH) for j in range(cls.ROWS_PER_BATCH)})
        return batches[0], batches[1:]

    def run_backups(self, test_name, initial_data, subsequent_data=None, restore_points=0):
        if bool(subsequent_data) != bool(restore_points):
            raise ValueError("Both or neither of subsequent_data and restore_points must be defined")

        db_name = random_suffix(f"{test_name}_", 10)
        self.recreate_db(db_name)
        self.write_data(db_name, self.DEFAULT_TABLE_NAME, initial_data, recreate_table=True)

        source_keyspace = self.get_full_keyspace(db_name)
        full_backup_location = f"{db_name}_full"
        self.backup_runner.run_create(full_backup_location, source_keyspace)

        diff_locations = []
        for i, data_dict in enumerate(subsequent_data if subsequent_data else []):
            self.write_data(db_name, self.DEFAULT_TABLE_NAME, data_dict, recreate_table=False)
            diff_location = f"{db_name}_diff_{i}"
            self.backup_runner.run_create_diff(diff_location,
                                               source_keyspace,
                                               diff_locations[-1] if diff_locations else full_backup_location,
                                               restore_points)
            diff_locations.append(diff_location)
        return BackupTestRun(db_name=db_name,
                             keyspace=source_keyspace,
                             full_location=full_backup_location,
                             diff_locations=diff_locations)

    @classmethod
    @abc.abstractmethod
    def log_file_name(cls): pass

    @classmethod
    @abc.abstractmethod
    def get_full_keyspace(cls, dbname): pass

    @abc.abstractmethod
    def read_data(self, db_name, table_name): pass

    @abc.abstractmethod
    def write_data(self, db_name, table_name, data, recreate_table=False): pass

    @abc.abstractmethod
    def delete_data(self, db_name, table_name, keys): pass

    @abc.abstractmethod
    def recreate_db(self, db_name): pass

    @abc.abstractmethod
    def drop_table(self, db_name, table_name): pass

    @abc.abstractmethod
    def assert_missing_table(self, db_name, table_name): pass

    def assert_table_data(self, db_name, table_name, expected_data):
        self.assertEqual(self.read_data(db_name, table_name), expected_data) # pylint: disable=no-member

    def restore_db(self, source_db_name, backup_location, suffix=None):
        suffix_s = f"_{suffix}" if suffix is not None else ""
        destination_db = f"{source_db_name}_restore{suffix_s}"
        self.backup_runner.run_restore(backup_location, self.get_full_keyspace(destination_db))
        return destination_db


    def test_backup(self):
        data, _ = self.make_test_data(1)
        run_results = self.run_backups(get_caller_function_name(), data)
        destination_db = self.restore_db(run_results.db_name, run_results.full_location)
        self.assert_table_data(destination_db, self.DEFAULT_TABLE_NAME, data)

    def test_single_diff_backup(self):
        initial_data, later_data = self.make_test_data(2)
        run_results = self.run_backups(get_caller_function_name(),
                                       initial_data,
                                       later_data,
                                       restore_points=1)
        destination_db = self.restore_db(run_results.db_name, run_results.diff_locations[0])
        expected_data = {}
        expected_data.update(initial_data)
        expected_data.update(later_data[0])
        self.assert_table_data(destination_db, self.DEFAULT_TABLE_NAME, expected_data)

    def test_multi_diff_backup_restore_second_last(self):
        # later differential backups can modify the manifest of previous backups depending on the
        # restore point parameter.
        # test that these earlier backups are still valid.
        initial_data, later_data = self.make_test_data(5)
        run_results = self.run_backups(get_caller_function_name(),
                                       initial_data,
                                       later_data,
                                       restore_points=2)
        destination_db = self.restore_db(run_results.db_name, run_results.diff_locations[-2])
        expected_data = {}
        expected_data.update(initial_data)
        for later in later_data[:-1]:
            expected_data.update(later)
        self.assert_table_data(destination_db, self.DEFAULT_TABLE_NAME, expected_data)

    def test_multi_diff_backup_restore_last(self):
        initial_data, later_data = self.make_test_data(5)
        run_results = self.run_backups(get_caller_function_name(),
                                       initial_data,
                                       later_data,
                                       restore_points=2)
        destination_db = self.restore_db(run_results.db_name, run_results.diff_locations[-1])
        expected_data = {}
        expected_data.update(initial_data)
        for chunk in later_data:
            expected_data.update(chunk)
        self.assert_table_data(destination_db, self.DEFAULT_TABLE_NAME, expected_data)

    def test_multi_diff_backup_single_restore_point(self):
        initial_data, later_data = self.make_test_data(3)
        run_results = self.run_backups(get_caller_function_name(),
                                       initial_data,
                                       later_data,
                                       restore_points=1)
        destination_db = self.restore_db(run_results.db_name, run_results.diff_locations[-1])
        expected_data = {}
        expected_data.update(initial_data)
        for chunk in later_data:
            expected_data.update(chunk)
        self.assert_table_data(destination_db, self.DEFAULT_TABLE_NAME, expected_data)

    def test_drop_table(self):
        restore_points = 4
        data, _ = self.make_test_data(1)
        db_name = random_suffix(f"{get_caller_function_name()}_", 10)
        self.recreate_db(db_name)
        tables = ["test_table1", "test_table2"]
        for table in tables:
            self.write_data(db_name, table, data, recreate_table=True)

        source_keyspace = self.get_full_keyspace(db_name)
        full_backup_location = f"{db_name}_full"
        self.backup_runner.run_create(full_backup_location, source_keyspace)

        self.drop_table(db_name, tables[0])

        diff_location = f"{db_name}_diff_0"
        self.backup_runner.run_create_diff(diff_location,
                                           source_keyspace,
                                           full_backup_location,
                                           restore_points)

        destination_db_full = self.restore_db(db_name, full_backup_location, suffix="full")
        self.assert_table_data(destination_db_full, tables[0], data)
        self.assert_table_data(destination_db_full, tables[1], data)

        destination_db_diff = self.restore_db(db_name, diff_location, suffix="diff")
        self.assert_missing_table(destination_db_diff, tables[0])
        self.assert_table_data(destination_db_diff, tables[1], data)

    def test_delete_from_table(self):
        restore_points = 4
        data, _ = self.make_test_data(1)
        db_name = random_suffix(f"{get_caller_function_name()}_", 10)
        self.recreate_db(db_name)
        self.write_data(db_name, self.DEFAULT_TABLE_NAME, data, recreate_table=True)

        source_keyspace = self.get_full_keyspace(db_name)
        full_backup_location = f"{db_name}_full"
        self.backup_runner.run_create(full_backup_location, source_keyspace)

        all_keys = list(data.keys())
        deleted_keys = all_keys[:len(all_keys) // 2]
        remaining_keys = {k for k in all_keys[len(all_keys) // 2:]}
        remaining_data = {k: v for k, v in data.items() if k in remaining_keys}

        self.delete_data(db_name, self.DEFAULT_TABLE_NAME, deleted_keys)
        diff_location = f"{db_name}_diff_0"
        self.backup_runner.run_create_diff(diff_location,
                                           source_keyspace,
                                           full_backup_location,
                                           restore_points)

        destination_db_full = self.restore_db(db_name, full_backup_location, suffix="full")
        self.assert_table_data(destination_db_full, self.DEFAULT_TABLE_NAME, data)

        destination_db_diff = self.restore_db(db_name, diff_location, suffix="diff")
        self.assert_table_data(destination_db_diff, self.DEFAULT_TABLE_NAME, remaining_data)


class YSQLBackupDiffTest(BackupDiffTest, unittest.TestCase):


    @classmethod
    def get_full_keyspace(cls, dbname):
        return f"ysql.{dbname}"

    @classmethod
    def log_file_name(cls):
        return "YSQLBackupDiffTest.log"

    def read_data(self, db_name, table_name):
        conn = self.backup_runner.connector.connect(db_name)
        with conn:
            data = None
            with conn.cursor() as cur:
                data = self.read_dict(cur, table_name)
        conn.close()
        return data

    def write_data(self, db_name, table_name, data, recreate_table=False):
        conn = self.backup_runner.connector.connect(db_name)
        with conn:
            with conn.cursor() as curr:
                if recreate_table:
                    self.recreate_table(curr, table_name=table_name)
                self.write_dict(curr, data, table_name=table_name)
        conn.close()

    def write_dict(self, cursor, kvs, table_name):
        stmt_values = ",".join(["(%s, %s)" for _ in range(len(kvs))])
        value_list = []
        for k, v in kvs.items():
            value_list.append(k)
            value_list.append(v)
        stmt = f"INSERT INTO {table_name} (id, value) VALUES " + stmt_values
        cursor.execute(stmt, value_list)

    def read_dict(self, cursor, table_name):
        cursor.execute(f"SELECT * from {table_name}")
        ret = cursor.fetchall()
        d = {}
        for row in ret:
            k, v = row
            d[k] = v
        return d

    def delete_data(self, db_name, table_name, keys):
        values_to_delete = list(keys)
        stmt_values = ",".join("%s" for _ in values_to_delete)
        stmt = f"DELETE FROM {table_name} where id in ({stmt_values})"
        conn = self.backup_runner.connector.connect(db_name)
        with conn:
            with conn.cursor() as cur:
                cur.execute(stmt, values_to_delete)
        conn.close()

    def recreate_db(self, db_name):
        conn = self.backup_runner.connector.connect()
        conn.set_session(autocommit=True)
        with conn.cursor() as curr:
            curr.execute(f"DROP DATABASE IF EXISTS {db_name}")
            curr.execute(f"CREATE DATABASE {db_name}")
        conn.close()

    def recreate_table(self, curr, table_name="test_table"):
        stmts = [f"DROP TABLE IF EXISTS {table_name};"]
        stmts.append(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, value VARCHAR);")
        curr.execute("\n".join(stmts))

    def drop_table(self, db_name, table_name):
        conn = self.backup_runner.connector.connect(db_name)
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE {table_name};")
        conn.close()

    def assert_missing_table(self, db_name, table_name):
        with self.assertRaises(psycopg2.errors.UndefinedTable): # pylint: disable=no-member
            self.read_data(db_name, table_name)


class YCQLBackupDiffTest(BackupDiffTest, unittest.TestCase):

    @classmethod
    def get_full_keyspace(cls, dbname):
        return f"ycql.{dbname}"

    @classmethod
    def setUpClass(cls):
        # pylint: disable=c-extension-no-member
        super(YCQLBackupDiffTest, cls).setUpClass()
        cls.cluster = cassandra.cluster.Cluster([cls.backup_runner.connector.host])
        cls.session = cls.cluster.connect()

    @classmethod
    def log_file_name(cls):
        return "YCQLBackupDiffTest.log"

    def read_data(self, db_name, table_name):
        rows = self.session.execute(f'SELECT * from {db_name}.{table_name}')
        return {row.id: row.value for row in rows}

    def write_data(self, db_name, table_name, data, recreate_table=False):
        # pylint: disable=c-extension-no-member
        full_table_name = f"{db_name}.{table_name}"
        if recreate_table:
            self.session.execute(f"DROP TABLE IF EXISTS {full_table_name}")
            self.session.execute(f"CREATE TABLE {full_table_name} (id int PRIMARY KEY, value varchar)")
        batch = cassandra.query.BatchStatement()
        for k, v in data.items():
            batch.add(cassandra.query.SimpleStatement(f"INSERT INTO {full_table_name} (id, value) VALUES (%s, %s)"),
                                                      (k, v))
        self.session.execute(batch)

    def recreate_db(self, db_name):
        self.session.execute("DROP KEYSPACE IF EXISTS {}".format(db_name))
        self.session.execute("CREATE KEYSPACE IF NOT EXISTS {}".format(db_name))

    def drop_table(self, db_name, table_name):
        self.session.execute(f"DROP TABLE IF EXISTS {db_name}.{table_name}")

    def delete_data(self, db_name, table_name, keys):
        # pylint: disable=c-extension-no-member
        full_table_name = f"{db_name}.{table_name}"
        batch = cassandra.query.BatchStatement()
        for k in keys:
            batch.add(cassandra.query.SimpleStatement(f"DELETE FROM {full_table_name} WHERE id = %s"), (k,))
        self.session.execute(batch)

    def assert_missing_table(self, db_name, table_name):
        with self.assertRaises(cassandra.InvalidRequest):
            self.read_data(db_name, table_name)

class BackupRunner:
    def __init__(self, args):
        self.args = args
        harness_args = args["test_harness"]
        self.connector = PgConnector(harness_args["db_user"],
                                     harness_args["db_host"],
                                     harness_args["db_port"],
                                     database=harness_args.get("db_name"),
                                     password=harness_args.get("db_password"))


    @staticmethod
    def initialize_from_file(fpath):
        with open(fpath, 'r', encoding='utf-8') as fp:
            args = json.load(fp)
            return BackupRunner(args)


    @staticmethod
    def to_args_list(argsd):
        l = []
        for s in argsd['switches']:
            l.append(f"--{s}")
        for k, v in argsd['key_values'].items():
            l.append(f"--{k}")
            l.append(str(v))
        return l


    @classmethod
    def get_args_list(cls, base_argsd, command_argsd, command, extra_kvs=None, extra_switches=None):
        backup_args = cls.to_args_list(base_argsd)
        backup_args.extend(cls.to_args_list(command_argsd))
        if extra_kvs:
            for k, v in extra_kvs.items():
                backup_args.append(f"--{k}")
                backup_args.append(str(v))
        if extra_switches:
            for s in extra_switches:
                backup_args.append(f"--{s}")
        backup_args.append(command)
        return backup_args


    def get_yb_backup(self, location_suffix, keyspace, command, command_args, extra_kvs=None):
        full_location_path = os.path.join(self.args['test_harness']['backup_location_base'], location_suffix)
        internal_extra_kvs = {}
        if extra_kvs:
            internal_extra_kvs.update(extra_kvs)
        internal_extra_kvs["backup_location"] = full_location_path
        internal_extra_kvs["keyspace"] = keyspace
        backup_args = self.get_args_list(self.args['general'],
                                         command_args,
                                         command,
                                         extra_kvs=internal_extra_kvs)
        return yb_backup_diff.YBBackup.create(backup_args)


    def run_create(self, location_suffix, keyspace):
        ybb = self.get_yb_backup(location_suffix, keyspace, 'create', self.args['create'])
        return ybb.run()


    def run_restore(self, location_suffix, keyspace):
        ybb = self.get_yb_backup(location_suffix, keyspace, 'restore', self.args['restore'])
        return ybb.run()


    def run_create_diff(self, location_suffix, keyspace, previous_location_suffix, restore_points):
        base_backup_location = self.args['test_harness']['backup_location_base']
        previous_location_full = os.path.join(base_backup_location, previous_location_suffix)
        ybb = self.get_yb_backup(location_suffix,
                                 keyspace,
                                 'create_diff',
                                 self.args['create'],
                                 extra_kvs={"prev_manifest_source": previous_location_full,
                                            "restore_points": restore_points})
        return ybb.run()


if __name__ == '__main__':
    unittest.main()
