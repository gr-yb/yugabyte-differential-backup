# Yb_backup.py



## Overview

ybbackup is at the table level for CQL only. 

```
"Back up for YSQL is only supported at the database level, "
"and not at the table level.")
```

## Main py execution

Main starts by calling a run command from ybbackup Class

```
YBBackup().run()
```



the run func is driven by the input arguement args.command with the following valid options from the command line. 

**Restore, create, restore_keys and delete**

```
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
```



![ybBackupDiagram.drawio](./resources/ybBackupDiagram.drawio.png)
