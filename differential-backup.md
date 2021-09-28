# Differential Backups

The current distributed backup implementation meets the efficiency and consistency goals stated in the [design for a full, distributed backup](https://github.com/yugabyte/yugabyte-db/blob/master/architecture/design/distributed-backup-and-restore.md) for in-cluster backups. The snapshot directories are created quickly and files are stored efficiently by using hard links. Hence no matter how many times a file is present in different snapshots it only uses the storage of one file. However when each snapshot is copied off-cluster the files referred to by the hard links are copied. So if a file is present in 5 snapshots, there will be 5 full copies in off-cluster storage. As the database grows the time and storage to copy off-cluster increases and can become impractical.

Differential backups share the goals, recovery scenarios, and features of [Point In Time Recovery (PITR) and Incremental Backups
](https://github.com/yugabyte/yugabyte-db/blob/master/architecture/design/distributed-backup-point-in-time-recovery.md) with the difference that differential backups will only restore to the time when a snapshot is created while PITR and incremental backups can restore to specific points in time.

## Goals

* Reduce the storage size and time to backup a database to off-cluster storage.
* Minimize changes to existing backup process
* Maximize re-use of existing backup code
* Remove expired files from off-cluster storage

## Process
After an in-cluster snapshot backup is created, the differential backup does these steps:

### Step 1. Retrieve the previous snapshot manifest

* A manifest or dictionary of all the files from the previous backup is used to determine which files are already stored off-cluster.
* For the first backup all files are copied off-cluster and the manifest with the files' off-cluster locations.

### Step 2. Create the current manifest

* Create the manifest of all files in the snapshot.
* Lookup each file in the previous snapshot manifest and if found, record the off-cluster storage location in the current manifest.
* If the file is not found in previous manifest then mark the file to be copied.

### Step 3. Copy new files to off-cluster storage

* Copy files off-cluster as indicated in the current manifest.
* Update the current manifest with the off-cluster location of the copied files.
* Copy the manifest off cluster

### Step 4. Remove expired files from off-cluster storage as needed.

* Based on the backup retention time, remove files from off-cluster storage that are not part of a snapshot that is still inside the retention time window.

# Design

Differential backups will be implemented within the [yb_backup.py](https://github.com/yugabyte/yugabyte-db/blob/master/managed/devops/bin/yb_backup.py) program as per the following:

* Create the manifest with the required meta-data to include table ids, tablet ids, and files in snapshots using  python dictionaries and persisted as JSON in files that are copied off-cluster.

   This is a sample of the python dictionary structure where location is the location of a file's off-cluster storage, time_t_value is epoch time in milliseconds, and version is the number of hard links for the file. Other meta-data may be added as needed.

```
import pprint
import json

pp = pprint.PrettyPrinter( indent= 4)

manifest = {}
manifest['table-id-1'] = {}
pp.pprint(manifest)
print("\n\n")

manifest['table-id-1']['tablet-a1']={}
pp.pprint(manifest)
print("\n\n")

manifest['table-id-1']['tablet-a1']['sst-file-1']={}
manifest['table-id-1']['tablet-a1']['sst-file-1']['location']='myuri'
manifest['table-id-1']['tablet-a1']['sst-file-1']['timestamp']='time_t_value'
manifest['table-id-1']['tablet-a1']['sst-file-1']['version']=1
pp.pprint(manifest)
print("\n\n")

manifest['table-id-1']['tablet-a2']={}
manifest['table-id-1']['tablet-a2']['sst-file-1']={}
manifest['table-id-1']['tablet-a2']['sst-file-1']['location']='myuri-a2'
manifest['table-id-1']['tablet-a2']['sst-file-1']['version']=1
manifest['table-id-1']['tablet-a2']['sst-file-1']['timestamp']='mytimestamp'
pp.pprint(manifest)
```

   Output

```
{'table-id-1': {}}



{'table-id-1': {'tablet-a1': {}}}



{   'table-id-1': {   'tablet-a1': {   'sst-file-1': {   'location': 'myuri',
                                                         'timestamp': 'time_t_value',
                                                         'version': 1}}}}



{   'table-id-1': {   'tablet-a1': {   'sst-file-1': {   'location': 'myuri',
                                                         'timestamp': 'time_t_value',
                                                         'version': 1}},
                      'tablet-a2': {   'sst-file-1': {   'location': 'myuri-a2',
                                                         'timestamp': 'mytimestamp',
                                                         'version': 1}}}}

```

* Calculate files to copy off-cluster by comparing with previous backup's manifest.

  * Each manifest file is persisted in off-cluster storage as is the SnapshotInfoPB and YSQLDump files (for SQL backups)
  * Use a naming convention for the manifest file to determine which is the manifest for the previous backup (or other mechanism) .
  * Load the previous' backup manifest and determine which files are new.

* Invoke primitives to copy and restore files instead of current directory based primitives.

  * Iterate through the manifest dictionary and invoke the off-cluster file copy primitive.

* Determine what files to delete off-cluster based on manifest and snapshot files. Files are deleted off-cluster when they exceed the time window for backup retentions and the number of restore points kept. The example that follows illustrates how the removal of files.

## Example

A walkthrough of 8 snapshots from a 2 minute interval backup schedule below demonstrate how differential backups work. The snapshots are of a ysql database created with the yb-sample-apps [SqlInserts](https://github.com/yugabyte/yb-sample-apps) workload which creates one table and one tablet. 

The sample app creates one table directory with id 000030ad000030008000000000004000, and has one tablet directory with id 4b90c92c6a4b4a3aa03c6f941a8c7d1b.

In the example the snapshots are stored in this directory for table id 000030ad000030008000000000004000 and tablet id 000030ad000030008000000000004000:
```
~/var/data/yb-data/tserver/data/rocksdb/table-000030ad000030008000000000004000/tablet-4b90c92c6a4b4a3aa03c6f941a8c7d1b.snapshots
```
These are the directories for each snapshot and below that are the contents of each directory 

```
drwxr-xr-x  14 gr  staff   448B Sep 24 01:35 4160b771-2620-44f2-a482-3f94e796aefc
drwxr-xr-x  16 gr  staff   512B Sep 24 01:37 81b0ce71-21fc-402f-8af3-2dea4cc7a7a9
drwxr-xr-x  18 gr  staff   576B Sep 24 01:39 83a006ce-40e5-408e-8f03-fba2e1c5f546
drwxr-xr-x  14 gr  staff   448B Sep 24 01:41 7f3c9719-69a6-4eb7-a86e-0ad368b6a322
drwxr-xr-x  16 gr  staff   512B Sep 24 01:43 24ebc93b-92a1-43cd-b177-699636f47287
drwxr-xr-x  10 gr  staff   320B Sep 24 01:45 1a92c67f-8a31-42e2-b45e-cae8a986334b
drwxr-xr-x  12 gr  staff   384B Sep 24 01:47 39c24b4f-a9db-4318-9e04-c7edac3a4fd1
drwxr-xr-x  14 gr  staff   448B Sep 24 01:49 ebe990cd-c5f2-4d91-bedc-b3252a4f5a75
```

Lines below that start with *** indicate files that are copied to off-cluster storage.

The intents directory and the MANIFEST and CURRENT files are always copied off-cluster.

### First snapshot directory

The first snapshot copies all files to off-cluster storage.

```
./4160b771-2620-44f2-a482-3f94e796aefc:
total 512064
*** -rw-r--r--  5 gr  staff   143M Sep 24 01:24 000021.sst.sblock.0
*** -rw-r--r--  5 gr  staff   6.9M Sep 24 01:24 000021.sst
*** -rw-r--r--  5 gr  staff    78M Sep 24 01:33 000027.sst.sblock.0
*** -rw-r--r--  5 gr  staff   2.8M Sep 24 01:33 000027.sst
*** -rw-r--r--  3 gr  staff   317B Sep 24 01:33 000028.sst.sblock.0
*** -rw-r--r--  3 gr  staff    65K Sep 24 01:33 000028.sst
*** -rw-r--r--  3 gr  staff    15M Sep 24 01:35 000030.sst.sblock.0
*** -rw-r--r--  3 gr  staff   538K Sep 24 01:35 000030.sst
*** drwxr-xr-x  4 gr  staff   128B Sep 24 01:35 intents
*** -rw-r--r--  1 gr  staff    10K Sep 24 01:35 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:35 CURRENT
*** -rw-r--r--  1 gr  staff   2.3K Sep 24 01:35 MANIFEST-000032
```

### Second snapshot directory

The second snapshot only copies the new "000031" sst files off-cluster.
All the other files in the directory are already off-cluster so they become entries in the manifest instead of being copied off-cluster.

```
./81b0ce71-21fc-402f-8af3-2dea4cc7a7a9:
total 552944
-rw-r--r--  5 gr  staff   143M Sep 24 01:24 000021.sst.sblock.0
-rw-r--r--  5 gr  staff   6.9M Sep 24 01:24 000021.sst
-rw-r--r--  5 gr  staff    78M Sep 24 01:33 000027.sst.sblock.0
-rw-r--r--  5 gr  staff   2.8M Sep 24 01:33 000027.sst
-rw-r--r--  3 gr  staff   317B Sep 24 01:33 000028.sst.sblock.0
-rw-r--r--  3 gr  staff    65K Sep 24 01:33 000028.sst
-rw-r--r--  3 gr  staff    15M Sep 24 01:35 000030.sst.sblock.0
-rw-r--r--  3 gr  staff   538K Sep 24 01:35 000030.sst
*** -rw-r--r--  2 gr  staff    19M Sep 24 01:37 000031.sst.sblock.0
*** -rw-r--r--  2 gr  staff   737K Sep 24 01:37 000031.sst
*** drwxr-xr-x  4 gr  staff   128B Sep 24 01:37 intents
*** -rw-r--r--  1 gr  staff    11K Sep 24 01:37 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:37 CURRENT
*** -rw-r--r--  1 gr  staff   2.7K Sep 24 01:37 MANIFEST-000033
```

### Third snapshot directory

The third  snapshot copies the new '32' sst files and adds their metadata to the manifest.

Again as in the second snapshots all previously copied files' storage locations are added to manifest for this snapshot

```
./83a006ce-40e5-408e-8f03-fba2e1c5f546:
total 593696
-rw-r--r--  5 gr  staff   143M Sep 24 01:24 000021.sst.sblock.0
-rw-r--r--  5 gr  staff   6.9M Sep 24 01:24 000021.sst
-rw-r--r--  5 gr  staff    78M Sep 24 01:33 000027.sst.sblock.0
-rw-r--r--  5 gr  staff   2.8M Sep 24 01:33 000027.sst
-rw-r--r--  3 gr  staff   317B Sep 24 01:33 000028.sst.sblock.0
-rw-r--r--  3 gr  staff    65K Sep 24 01:33 000028.sst
-rw-r--r--  3 gr  staff    15M Sep 24 01:35 000030.sst.sblock.0
-rw-r--r--  3 gr  staff   538K Sep 24 01:35 000030.sst
-rw-r--r--  2 gr  staff    19M Sep 24 01:37 000031.sst.sblock.0
-rw-r--r--  2 gr  staff   737K Sep 24 01:37 000031.sst
*** -rw-r--r--  1 gr  staff    19M Sep 24 01:39 000032.sst.sblock.0
*** -rw-r--r--  1 gr  staff   672K Sep 24 01:39 000032.sst
*** drwxr-xr-x  4 gr  staff   128B Sep 24 01:39 intents
*** -rw-r--r--  1 gr  staff    11K Sep 24 01:39 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:39 CURRENT
*** -rw-r--r--  1 gr  staff   3.1K Sep 24 01:39 MANIFEST-000034
```

### Fourth snapshot directory

The fourth snapshot copies the new '33' and '34' files off-cluster.

The manifest entry for this snapshot does not have  files 28, 30, 31, and 32 from the previous snapshot. These files have been compacted and replaced by files 33 and 34. 

Files 21 and 27 are still present in this snapshot. Only files 21 and 27  from the third snapshot.

```
./7f3c9719-69a6-4eb7-a86e-0ad368b6a322:
total 631264
-rw-r--r--  5 gr  staff   143M Sep 24 01:24 000021.sst.sblock.0
-rw-r--r--  5 gr  staff   6.9M Sep 24 01:24 000021.sst
-rw-r--r--  5 gr  staff    78M Sep 24 01:33 000027.sst.sblock.0
-rw-r--r--  5 gr  staff   2.8M Sep 24 01:33 000027.sst
*** -rw-r--r--  2 gr  staff    52M Sep 24 01:39 000033.sst.sblock.0
*** -rw-r--r--  2 gr  staff   1.8M Sep 24 01:39 000033.sst
*** -rw-r--r--  2 gr  staff    18M Sep 24 01:41 000034.sst.sblock.0
*** -rw-r--r--  2 gr  staff   671K Sep 24 01:41 000034.sst
*** drwxr-xr-x  4 gr  staff   128B Sep 24 01:41 intents
*** -rw-r--r--  1 gr  staff    12K Sep 24 01:41 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:41 CURRENT
*** -rw-r--r--  1 gr  staff   2.3K Sep 24 01:41 MANIFEST-000036
```

### Fifth snapshot directory

The fifth snapshot copies the new '35' files and updatest the manifest.

All files from the fourth snapshot are also here.

```
./24ebc93b-92a1-43cd-b177-699636f47287:
total 671488
-rw-r--r--  5 gr  staff   143M Sep 24 01:24 000021.sst.sblock.0
-rw-r--r--  5 gr  staff   6.9M Sep 24 01:24 000021.sst
-rw-r--r--  5 gr  staff    78M Sep 24 01:33 000027.sst.sblock.0
-rw-r--r--  5 gr  staff   2.8M Sep 24 01:33 000027.sst
-rw-r--r--  2 gr  staff    52M Sep 24 01:39 000033.sst.sblock.0
-rw-r--r--  2 gr  staff   1.8M Sep 24 01:39 000033.sst
-rw-r--r--  2 gr  staff    18M Sep 24 01:41 000034.sst.sblock.0
-rw-r--r--  2 gr  staff   671K Sep 24 01:41 000034.sst
*** -rw-r--r--  1 gr  staff    18M Sep 24 01:43 000035.sst.sblock.0
*** -rw-r--r--  1 gr  staff   672K Sep 24 01:43 000035.sst
*** drwxr-xr-x  4 gr  staff   128B Sep 24 01:43 intents
*** -rw-r--r--  1 gr  staff    13K Sep 24 01:43 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:43 CURRENT
*** -rw-r--r--  1 gr  staff   2.7K Sep 24 01:43 MANIFEST-000037
```

### Sixth snapshot directory

The sixth snapshot copies the new '38' files off-cluster and adds the storage locations of files 36 and 37 to the mainfest.
The sixth snapshot copies the new 36 and 37 sst files and adds their storage locations to the manifest.

All files from the the all previous snapshots have been replaced or compacted into the new files for this snapshot. The manifest for this snapshot will only have entries for files 36 and 37.

```
./1a92c67f-8a31-42e2-b45e-cae8a986334b:
total 621792
*** -rw-r--r--  4 gr  staff   266M Sep 24 01:44 000036.sst.sblock.0
*** -rw-r--r--  4 gr  staff    13M Sep 24 01:44 000036.sst
*** -rw-r--r--  4 gr  staff    17M Sep 24 01:45 000037.sst.sblock.0
*** -rw-r--r--  4 gr  staff   670K Sep 24 01:45 000037.sst
*** drwxr-xr-x  4 gr  staff   128B Sep 24 01:45 intents
*** -rw-r--r--  1 gr  staff    14K Sep 24 01:45 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:45 CURRENT
*** -rw-r--r--  1 gr  staff   1.5K Sep 24 01:45 MANIFEST-000039
```

### Seventh snapshot directory

The seventh snapshot copies the new '38' files off-cluster and adds the storage locations of files 36 and 37 to the mainfest.

```
./39c24b4f-a9db-4318-9e04-c7edac3a4fd1:
total 658464
-rw-r--r--  4 gr  staff   266M Sep 24 01:44 000036.sst.sblock.0
-rw-r--r--  4 gr  staff    13M Sep 24 01:44 000036.sst
-rw-r--r--  4 gr  staff    17M Sep 24 01:45 000037.sst.sblock.0
-rw-r--r--  4 gr  staff   670K Sep 24 01:45 000037.sst
*** -rw-r--r--  3 gr  staff    17M Sep 24 01:47 000038.sst.sblock.0
*** -rw-r--r--  3 gr  staff   670K Sep 24 01:47 000038.sst
*** drwxr-xr-x  4 gr  staff   128B Sep 24 01:47 intents
*** -rw-r--r--  1 gr  staff    15K Sep 24 01:47 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:47 CURRENT
*** -rw-r--r--  1 gr  staff   1.9K Sep 24 01:47 MANIFEST-000040
```

### Eigth snapshot directory

The eigth snapshot copies the '39' files off-cluster and adds the storage locations of files 36, 37, and 38 to the mainfest.

```
./ebe990cd-c5f2-4d91-bedc-b3252a4f5a75:
total 692576
-rw-r--r--  4 gr  staff   266M Sep 24 01:44 000036.sst.sblock.0
-rw-r--r--  4 gr  staff    13M Sep 24 01:44 000036.sst
-rw-r--r--  4 gr  staff    17M Sep 24 01:45 000037.sst.sblock.0
-rw-r--r--  4 gr  staff   670K Sep 24 01:45 000037.sst
*** -rw-r--r--  3 gr  staff    17M Sep 24 01:47 000038.sst.sblock.0
*** -rw-r--r--  3 gr  staff   670K Sep 24 01:47 000038.sst
*** -rw-r--r--  2 gr  staff    16M Sep 24 01:49 000039.sst.sblock.0
*** -rw-r--r--  2 gr  staff   604K Sep 24 01:49 000039.sst
*** drwxr-xr-x  4 gr  staff   128B Sep 24 01:49 intents
*** -rw-r--r--  1 gr  staff    15K Sep 24 01:49 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:49 CURRENT
*** -rw-r--r--  1 gr  staff   2.3K Sep 24 01:49 MANIFEST-000041
```

This diagram illustrates the files' lifecycle in differential backups

![image](https://user-images.githubusercontent.com/84997113/135130246-3a59e21d-1949-48f0-8862-7b62f9e72ada.png)

## Off-Cluster File Removals

From the example, for a backup retention window of 6 minutes the first files removed are files
28, 30, 31, and 32 files at the seventh snapshot because the third snapshot occurred more 8 minutes from the seventh snapshot

# Implementation

* Add yb_create_differential command option
* Yb_backup create_differential
   * Parameters:
      * Last_backup_location ←- where is my manifest?
      * Restore_points to retain ←- when do I expire?
      * Recopy_threshold ←- when do we recopy slowly changing files
      * Backup history retention time


# Restore points

In addition to snapshot recoveries base on time, restore points are a mechanism to restore files beyond the backup history retention up to a discrete number of retention points as set though configuration.

Files that would be removed by backup retention time would be moved to a location where restore points use to recover.
gr@mbPro ~/YB/differentialBackup %
