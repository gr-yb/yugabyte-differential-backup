import aiopg
import argparse
import asyncio
import concurrent.futures
import json
import unittest
import os.path
import random
import string

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


    async def connect(self, database=None):
        kwargs = {"database": database if database is not None else self.base_database,
                  "user": self.user,
                  "host": self.host,
                  "port": self.port
                  }
        if self.password is not None:
            kwargs['password'] = self.password
        return await aiopg.connect(**kwargs)


class BackupDiffTest(unittest.IsolatedAsyncioTestCase):
    backup_runner = None

    async def test_backup(self):
        location_suffix = random_suffix("test_backup_", 10)
        db_name = random_suffix("test_backup_source_", 10)
        source_keyspace = f"ysql.{db_name}"
        data = {1: "1",
                2: "2",
                3: "3"}

        await self.recreate_db(db_name)
        conn = await self.backup_runner.connector.connect(db_name)
        async with conn.cursor() as curr:
            await self.recreate_table(curr)
            await self.write_dict(curr, data)
            read_data = await self.read_dict(curr)
            self.assertEqual(data, read_data)
        await conn.close()

        await self.backup_runner.run_create(location_suffix, source_keyspace)
        destination_db = db_name + "_restored"
        target_keyspace = f"ysql.{destination_db}"
        await self.backup_runner.run_restore(location_suffix, target_keyspace)

        conn = await self.backup_runner.connector.connect(destination_db)
        async with conn.cursor() as curr:
            read_data = await self.read_dict(curr)
            self.assertEqual(data, read_data)
        await conn.close()


    async def test_diff_backup(self):
        full_location_suffix = random_suffix("test_diff_backup_", 10)
        db_name = random_suffix("test_diff_backup_source_", 10)
        source_keyspace = f"ysql.{db_name}"

        await self.recreate_db(db_name)
        conn = await self.backup_runner.connector.connect(db_name)
        async with conn.cursor() as curr:
            await self.recreate_table(curr)
            data = {k: str(k) for k in range(0, 10)}
            await self.write_dict(curr, data)
        await conn.close()

        await self.backup_runner.run_create(full_location_suffix, source_keyspace)
        conn = await self.backup_runner.connector.connect(db_name)
        async with conn.cursor() as curr:
            data = {k: str(k) for k in range(10, 20)}
            await self.write_dict(curr, data)
        await conn.close()

        diff_location_suffix = random_suffix("test_diff_backup_diff1_", 10)
        await self.backup_runner.run_create_diff(diff_location_suffix, source_keyspace, full_location_suffix)
        destination_db = db_name + "_restored"
        destination_keyspace = f"ysql.{destination_db}"
        await self.backup_runner.run_restore(diff_location_suffix, destination_keyspace)

        conn = await self.backup_runner.connector.connect(destination_db)
        async with conn.cursor() as curr:
            read_data = await self.read_dict(curr)
            self.assertEqual({k: str(k) for k in range(0, 20)},
                             read_data)
        await conn.close()


    async def recreate_db(self, dbname):
        conn = await self.backup_runner.connector.connect()
        async with conn.cursor() as curr:
            await curr.execute(f"""
            DROP DATABASE IF EXISTS {dbname}
            """)
            await curr.execute(f"""
            CREATE DATABASE {dbname}
            """)
        await conn.close()


    async def recreate_table(self, curr):
        await curr.execute(f"""
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
        with open(fpath, 'r') as fp:
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


    async def run_create_diff(self, location_suffix, keyspace, previous_location_suffix):
        base_backup_location = self.args['test_harness']['backup_location_base']
        previous_location_full = os.path.join(base_backup_location, previous_location_suffix)
        ybb = self.get_yb_backup(location_suffix,
                                 keyspace,
                                 'create_diff',
                                 self.args['create'],
                                 extra_kvs={"prev_manifest_source": previous_location_full})
        return await self.asyncify(ybb.run)


if __name__ == '__main__':
    import sys
    import logging

    parser = argparse.ArgumentParser()
    parser.add_argument('--args_file', required=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(filename)s %(lineno)d: %(message)s",
                        filename="test_run.log")

    test_harness_args, leftover_args = parser.parse_known_args()

    br = BackupRunner.initialize_from_file(test_harness_args.args_file)
    BackupDiffTest.backup_runner = br
    unittest.main(argv=[sys.argv[0]] + leftover_args)
