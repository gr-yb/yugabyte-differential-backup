# Usage Examples

The differential backup script requires environmental configuration passed in through command line
arguments. More details on each argument can be found by passing the `--help` flag to the
`yb_backup_diff.py` program.

This is an example of creating a backup against a YSQL database. Make sure to replace the arguments
with the proper values for your environment:

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

Creating a differential backup based on the previously made full backup:
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

Restoring from the previously created differential backup:
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
