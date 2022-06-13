# Usage Examples

The differential backup script requires environmental configuration passed in through command line
arguments. More details on each argument can be found by passing the `--help` flag to the
`yb_backup_diff.py` program.

This is an example of creating a backup against a YSQL database. Make sure to replace the arguments
with the proper values for your environment. The important arguments are:
* `keyspace` specifies the YSQL database to back up
* `backup_location` the offsite location to store the backup files
* `storage_type` the kind of offsite storage used. S3 is the most heavily tested, NFS is poorly
  supported.

```
python yb_backup_diff.py \
    --no_auto_name \
    --disable_checksums \
    --verbose \
    --masters host1:port1,host2:port2 \
    --ysql_port 5433 \
    --ssh_port 22 \
    --ssh_key_path /path/to/private/key/for/sshing/into/server \
    --ssh_user user_to_ssh_into_server_as \
    --remote_user user_to_change_into_perform_running_commands \
    --remote_yb_admin_binary /path/to/ybadmin/on/server \
    --remote_ysql_dump_binary /path/to/ysql_dump/on/server \
    --remote_ysql_shell_binary /path/to/ysqlsh/on/server \
    --aws_credentials_file /path/to/aws/credentials/file \
    --storage_type s3 \
    --history_file backup_command_history.log \
    --keyspace ysql.db_name \
    --backup_location s3://bucket/full_backup_location \
    create
```

Creating a differential backup based on the previously created full backup. Important arguments:
* `backup_location` the offsite location to store the backup files
* `keyspace` the YSQL database to back up. Must match the keyspace argument to the previous `create` or `create_diff` command
* `prev_manifest_source` the `backup_location` passed to the previous `create` or `create_diff`
  command to use as the base for the differential backup
* `restore_points` the number of differential backups that can be restored from at any given
  time. For example, creating a chain of differential backups of length 10 where each was created
  with a `restore_points` value of 4 will only allow you to successfully restore from the previous 4
  differential backups. The older backups will be invalid.


```
python yb_backup_diff.py \
    --no_auto_name \
    --disable_checksums \
    --verbose \
    --masters host1:port1,host2:port2 \
    --ysql_port 5433 \
    --ssh_port 22 \
    --ssh_key_path /path/to/private/key/for/sshing/into/server \
    --ssh_user user_to_ssh_into_server_as \
    --remote_user user_to_change_into_perform_running_commands \
    --remote_yb_admin_binary /path/to/ybadmin/on/server \
    --remote_ysql_dump_binary /path/to/ysql_dump/on/server \
    --remote_ysql_shell_binary /path/to/ysqlsh/on/server \
    --aws_credentials_file /path/to/aws/credentials/file \
    --storage_type s3 \
    --history_file backup_command_history.log \
    --keyspace ysql.db_name \
    --prev_manifest_source s3://bucket/full_backup_location \
    --restore_points 4 \
    --backup_location s3://bucket/diff_backup_location \
    create_diff
```

Restoring from the previously created differential backup. Important arguments:
* `backup_location` the location to find the previously created backup to restore from
* `keyspace` the YSQL database to restore to. This database will be created if it does not exist.

```
python yb_backup_diff.py \
    --no_auto_name \
    --disable_checksums \
    --verbose \
    --masters host1:port1,host2:port2 \
    --ysql_port 5433 \
    --ssh_port 22 \
    --ssh_key_path /path/to/private/key/for/sshing/into/server \
    --ssh_user user_to_ssh_into_server_as \
    --remote_user user_to_change_into_perform_running_commands \
    --remote_yb_admin_binary /path/to/ybadmin/on/server \
    --remote_ysql_dump_binary /path/to/ysql_dump/on/server \
    --remote_ysql_shell_binary /path/to/ysqlsh/on/server \
    --aws_credentials_file /path/to/aws/credentials/file \
    --storage_type s3 \
    --history_file backup_command_history.log \
    --keyspace ysql.db_name_restored \
    --backup_location s3://bucket/full_backup_location \
    restore
```
