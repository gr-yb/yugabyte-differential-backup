import asyncio
import collections
import json
import logging
import os
import os.path
import random
import string
import unittest

import aiopg

import yb_backup_diff

def random_suffix(prefix, n):
    return prefix + ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

class PgConnector:
    def __init__(self, user, host, port, password=None, database=None):
        self.user = user
        self.host = host
        self.password = password
        self.port = port
        self.base_database = database if database is not None else "yugabyte"


    # returns an async context manager.
    def connect(self, database=None):
        kwargs = {"database": database if database is not None else self.base_database,
                  "user": self.user,
                  "host": self.host,
                  "port": self.port}
        if self.password is not None:
            kwargs['password'] = self.password
        return aiopg.connect(**kwargs)


BackupTestRun = collections.namedtuple('BackupTestRun', ['db_name', 'keyspace', 'full_location', 'diff_locations'])


class BackupDiffTest(unittest.IsolatedAsyncioTestCase):
    backup_runner = None
    ROWS_PER_BATCH = 10
    ARGS_FILE_PATH = "backup_args.json"
    LOG_FILE = "test_run.log"


    @classmethod
    def setUpClass(cls):
        cls.backup_runner = BackupRunner.initialize_from_file(cls.ARGS_FILE_PATH)
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(filename)s %(lineno)d: %(message)s",
                            filename=cls.LOG_FILE,
                            filemode='w')


    @classmethod
    def make_test_data(cls, num_batches):
        batches = []
        for i in range(num_batches):
            batches.append({j + i * cls.ROWS_PER_BATCH:
                           str(j + i * cls.ROWS_PER_BATCH) for j in range(cls.ROWS_PER_BATCH)})
        return batches[0], batches[1:]


    async def run_backups(self, test_name, initial_data, subsequent_data=None, restore_points=0):
        if bool(subsequent_data) != bool(restore_points):
            raise ValueError("Both or neither of subsequent_data and restore_points must be defined")

        db_name = random_suffix(f"{test_name}_", 10)
        await self.recreate_db(db_name)
        async with self.backup_runner.connector.connect(db_name) as conn:
            async with conn.cursor() as curr:
                await self.recreate_table(curr)
                await self.write_dict(curr, initial_data)
        source_keyspace = f"ysql.{db_name}"
        full_backup_location = f"{db_name}_full"
        await self.backup_runner.run_create(full_backup_location, source_keyspace)

        diff_locations = []
        for i, data_dict in enumerate(subsequent_data if subsequent_data else []):
            async with self.backup_runner.connector.connect(db_name) as conn:
                async with conn.cursor() as curr:
                    await self.write_dict(curr, data_dict)
            diff_location = f"{db_name}_diff_{i}"
            await self.backup_runner.run_create_diff(diff_location,
                                                     source_keyspace,
                                                     diff_locations[-1] if diff_locations else full_backup_location,
                                                     restore_points)
            diff_locations.append(diff_location)
        return BackupTestRun(db_name=db_name,
                             keyspace=source_keyspace,
                             full_location=full_backup_location,
                             diff_locations=diff_locations)


    async def read_data(self, db_name):
        async with self.backup_runner.connector.connect(db_name) as conn:
            data = None
            async with conn.cursor() as cur:
                data = await self.read_dict(cur)
        return data


    async def restore_db(self, source_db_name, backup_location):
        destination_db = f"{source_db_name}_restore"
        await self.backup_runner.run_restore(backup_location, f"ysql.{destination_db}")
        return destination_db


    async def test_backup(self):
        data, _ = self.make_test_data(1)
        print(f"running test function {self.test_backup.__name__}")
        run_results = await self.run_backups("test_backup", data)
        destination_db = await self.restore_db(run_results.db_name, run_results.full_location)
        print("reading data")
        self.assertEqual(await self.read_data(destination_db),
                         data)


    async def test_single_diff_backup(self):
        initial_data, later_data = self.make_test_data(2)
        print("creating data and backups for test_single_diff_backup")
        run_results = await self.run_backups("test_single_diff_backup", initial_data, later_data, restore_points=4)
        destination_db = await self.restore_db(run_results.db_name, run_results.diff_locations[0])
        expected_data = {}
        expected_data.update(initial_data)
        expected_data.update(later_data[0])
        self.assertEqual(await self.read_data(destination_db),
                         expected_data)


    async def test_multi_diff_backup_restore_second_last(self):
        # later differential backups can modify the manifest of previous backups depending on the
        # restore point parameter.
        # test that these earlier backups are still valid.
        initial_data, later_data = self.make_test_data(5)
        print("creating data and backups for test_multi_diff_backup_restore_second_last")
        run_results = await self.run_backups("test_multi_diff_backup_restore_second_last", initial_data, later_data, restore_points=2)
        destination_db = await self.restore_db(run_results.db_name, run_results.diff_locations[-2])
        expected_data = {}
        expected_data.update(initial_data)
        for later in later_data[:-1]:
            expected_data.update(later)
        self.assertEqual(await self.read_data(destination_db),
                         expected_data)


    async def test_multi_diff_backup_restore_last(self):
        initial_data, later_data = self.make_test_data(5)
        print("creating data and backups for test_multi_diff_backup_restore_last")
        run_results = await self.run_backups("test_multi_diff_backup_restore_last", initial_data, later_data, restore_points=2)
        destination_db = await self.restore_db(run_results.db_name, run_results.diff_locations[-1])
        expected_data = {}
        expected_data.update(initial_data)
        for chunk in later_data:
            expected_data.update(chunk)
        self.assertEqual(await self.read_data(destination_db),
                         expected_data)


    async def recreate_db(self, dbname):
        async with await self.backup_runner.connector.connect() as conn:
            async with conn.cursor() as curr:
                await curr.execute(f"""
                DROP DATABASE IF EXISTS {dbname}
                """)
                await curr.execute(f"""
                CREATE DATABASE {dbname}
                """)


    async def recreate_table(self, curr):
        await curr.execute("""
          DROP TABLE IF EXISTS test_table;
          CREATE TABLE test_table (id int PRIMARY KEY,
                     value varchar);
        """)


    async def write_dict(self, cursor, kvs):
        stmt_values = ",".join(["(%s, %s)" for _ in range(len(kvs))])
        value_list = []
        for k, v in kvs.items():
            value_list.append(k)
            value_list.append(v)
        stmt = "INSERT INTO test_table (id, value) VALUES " + stmt_values
        await cursor.execute(stmt, value_list)


    async def read_dict(self, cursor):
        await cursor.execute("SELECT * from test_table")
        ret = await cursor.fetchall()
        d = {}
        for row in ret:
            k, v = row
            d[k] = v
        return d


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


    async def asyncify(self, f):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, f)


    async def run_create(self, location_suffix, keyspace):
        ybb = self.get_yb_backup(location_suffix, keyspace, 'create', self.args['create'])
        return await self.asyncify(ybb.run)


    async def run_restore(self, location_suffix, keyspace):
        ybb = self.get_yb_backup(location_suffix, keyspace, 'restore', self.args['restore'])
        return await self.asyncify(ybb.run)


    async def run_create_diff(self, location_suffix, keyspace, previous_location_suffix, restore_points):
        base_backup_location = self.args['test_harness']['backup_location_base']
        previous_location_full = os.path.join(base_backup_location, previous_location_suffix)
        ybb = self.get_yb_backup(location_suffix,
                                 keyspace,
                                 'create_diff',
                                 self.args['create'],
                                 extra_kvs={"prev_manifest_source": previous_location_full,
                                            "restore_points": restore_points})
        return await self.asyncify(ybb.run)


if __name__ == '__main__':
    unittest.main()
