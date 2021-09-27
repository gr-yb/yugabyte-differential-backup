# Differential Backups

The current distributed backup implementation meets the efficiency and consistency goals stated in the [design for a full, distributed backup](https://github.com/yugabyte/yugabyte-db/blob/master/architecture/design/distributed-backup-and-restore.md) for in-cluster backups. The snapshot directories are created quickly and files are stored efficiently by using hard links. Hence, no matter how many times a file is present in different snapshots, it only uses the storage of one file. However when each snapshot is copied off-cluster the files referred to by the hard links are copied. So if a file is present in 5 snapshots, there will be 5 full copies in off-cluster storage. As the database grows the time and storage to copy off-cluster increases and can become impractical.

Differential backups share the goals, recovery scenarios, and features of [Point In Time Recovery (PITR) and Incremental Backups
](https://github.com/yugabyte/yugabyte-db/blob/master/architecture/design/distributed-backup-point-in-time-recovery.md) but the major difference is that differential backups will only restore to the time when a snapshot is created while PITR and incremental backups can restore to specific points in time.

# Goals

* Reduce the storage size and time to backup a database to off-cluster storage.
* Minimize changes to existing backup process
* Maximize re-use of existing backup code
* Remove expired files from off-cluster storage 

# Process 
After an in-cluster snapshot backup is created, the differential backup feature conducts these steps:

### Step 1. Retrieve the previous snapshot manifest 

* A manifest or dictionary of all the files from the previous backup is used to determine which files are already stored off-cluster. 
* For the first or a full backup all files will be copied off-cluster along with the manifest.

### Step 2. Create the current manifest

* Create the manifest of all files in the snapshot. 
* Lookup each file in the previous snapshot manifest and if found, record the off-cluster storage location in the current manifest. 
* If the file is not found in previous manifest then mark the file to be copied. 

### Step 3. Copy new files to off-cluster storage

* Copy files off-cluster as indicated in the current manifest.
* Update the current manifest with the off-cluster location of the copied files.
* Copy the manifest off cluster

### Step 4. Remove expired files from off-cluster storage as needed.

* Based on the history cutoff timestamp remove files that do not exist in a snapshot. Effectively when a file drops off from the source snapshots it is removed from off-cluster storage 

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

  * Each manifest file will be persisted in off-cluster storage as is the SnapshotInfoPB and YSQLDump files (for SQL backups) 
  * Use a naming convention for the manifest file to determine which is the manifest for the previous backup. 
  * Load the previous' backup manifest and determine which files are new.

* Invoke primitives to copy and restore files instead of current directory based primitives.  
  
  * Iterate through the maifest dictionary and invoke the off-cluster file copy primitive.
   
* Determine what files to delete off-cluster based on manifest and snapshot files. Only when files are deleted from the tserver snapshots will they be removed from off-cluster storage. The example that follows illustrates how the removal of off-cluster files.

# Example

A walkthrough of the following directories of 9 snapshots from a postgres database created with the yb-sample-apps SqlInserts workload demonstrates how differential backups are intended to work. The sample app creates one table and one tablet with ids  000030ad000030008000000000004000 and 4b90c92c6a4b4a3aa03c6f941a8c7d1b respectively.

Below are the 9 snapshot directories created by a scheduled backup with a 2 minute frequency found in the following tablet directory:
```
~/var/data/yb-data/tserver/data/rocksdb/table-000030ad000030008000000000004000/tablet-4b90c92c6a4b4a3aa03c6f941a8c7d1b.snapshots
```


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

The MANIFEST and CURRENT files in each snapshot are always copied off-cluster.

The first snapshot will have all files copied to off-cluster storage but subsequent snapshots will only copy new files off-cluster.

```
./4160b771-2620-44f2-a482-3f94e796aefc:
total 512064
*** -rw-r--r--  5 gr  staff   143M Sep 24 01:24 000021.sst.sblock.0
*** -rw-r--r--  5 gr  staff   6.9M Sep 24 01:24 000021.sst
*** -rw-r--r--  3 gr  staff   317B Sep 24 01:33 000028.sst.sblock.0
*** -rw-r--r--  3 gr  staff    65K Sep 24 01:33 000028.sst
*** -rw-r--r--  5 gr  staff    78M Sep 24 01:33 000027.sst.sblock.0
*** -rw-r--r--  5 gr  staff   2.8M Sep 24 01:33 000027.sst
*** -rw-r--r--  3 gr  staff    15M Sep 24 01:35 000030.sst.sblock.0
*** -rw-r--r--  3 gr  staff   538K Sep 24 01:35 000030.sst
drwxr-xr-x  4 gr  staff   128B Sep 24 01:35 intents
*** -rw-r--r--  1 gr  staff    10K Sep 24 01:35 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:35 CURRENT
*** -rw-r--r--  1 gr  staff   2.3K Sep 24 01:35 MANIFEST-000032

./81b0ce71-21fc-402f-8af3-2dea4cc7a7a9:
total 552944
-rw-r--r--  5 gr  staff   143M Sep 24 01:24 000021.sst.sblock.0
-rw-r--r--  5 gr  staff   6.9M Sep 24 01:24 000021.sst
-rw-r--r--  3 gr  staff   317B Sep 24 01:33 000028.sst.sblock.0
-rw-r--r--  3 gr  staff    65K Sep 24 01:33 000028.sst
-rw-r--r--  5 gr  staff    78M Sep 24 01:33 000027.sst.sblock.0
-rw-r--r--  5 gr  staff   2.8M Sep 24 01:33 000027.sst
-rw-r--r--  3 gr  staff    15M Sep 24 01:35 000030.sst.sblock.0
-rw-r--r--  3 gr  staff   538K Sep 24 01:35 000030.sst
*** -rw-r--r--  2 gr  staff    19M Sep 24 01:37 000031.sst.sblock.0
*** -rw-r--r--  2 gr  staff   737K Sep 24 01:37 000031.sst
drwxr-xr-x  4 gr  staff   128B Sep 24 01:37 intents
*** -rw-r--r--  1 gr  staff    11K Sep 24 01:37 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:37 CURRENT
*** -rw-r--r--  1 gr  staff   2.7K Sep 24 01:37 MANIFEST-000033

./83a006ce-40e5-408e-8f03-fba2e1c5f546:
total 593696
-rw-r--r--  5 gr  staff   143M Sep 24 01:24 000021.sst.sblock.0
-rw-r--r--  5 gr  staff   6.9M Sep 24 01:24 000021.sst
-rw-r--r--  3 gr  staff   317B Sep 24 01:33 000028.sst.sblock.0
-rw-r--r--  3 gr  staff    65K Sep 24 01:33 000028.sst
-rw-r--r--  5 gr  staff    78M Sep 24 01:33 000027.sst.sblock.0
-rw-r--r--  5 gr  staff   2.8M Sep 24 01:33 000027.sst
-rw-r--r--  3 gr  staff    15M Sep 24 01:35 000030.sst.sblock.0
-rw-r--r--  3 gr  staff   538K Sep 24 01:35 000030.sst
-rw-r--r--  2 gr  staff    19M Sep 24 01:37 000031.sst.sblock.0
-rw-r--r--  2 gr  staff   737K Sep 24 01:37 000031.sst
*** -rw-r--r--  1 gr  staff    19M Sep 24 01:39 000032.sst.sblock.0
*** -rw-r--r--  1 gr  staff   672K Sep 24 01:39 000032.sst
drwxr-xr-x  4 gr  staff   128B Sep 24 01:39 intents
*** -rw-r--r--  1 gr  staff    11K Sep 24 01:39 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:39 CURRENT
*** -rw-r--r--  1 gr  staff   3.1K Sep 24 01:39 MANIFEST-000034

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
drwxr-xr-x  4 gr  staff   128B Sep 24 01:41 intents
*** -rw-r--r--  1 gr  staff    12K Sep 24 01:41 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:41 CURRENT
*** -rw-r--r--  1 gr  staff   2.3K Sep 24 01:41 MANIFEST-000036

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
drwxr-xr-x  4 gr  staff   128B Sep 24 01:43 intents
*** -rw-r--r--  1 gr  staff    13K Sep 24 01:43 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:43 CURRENT
*** -rw-r--r--  1 gr  staff   2.7K Sep 24 01:43 MANIFEST-000037

./1a92c67f-8a31-42e2-b45e-cae8a986334b:
total 621792
*** -rw-r--r--  4 gr  staff   266M Sep 24 01:44 000036.sst.sblock.0
*** -rw-r--r--  4 gr  staff    13M Sep 24 01:44 000036.sst
*** -rw-r--r--  4 gr  staff    17M Sep 24 01:45 000037.sst.sblock.0
*** -rw-r--r--  4 gr  staff   670K Sep 24 01:45 000037.sst
drwxr-xr-x  4 gr  staff   128B Sep 24 01:45 intents
*** -rw-r--r--  1 gr  staff    14K Sep 24 01:45 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:45 CURRENT
*** -rw-r--r--  1 gr  staff   1.5K Sep 24 01:45 MANIFEST-000039

./39c24b4f-a9db-4318-9e04-c7edac3a4fd1:
total 658464
-rw-r--r--  4 gr  staff   266M Sep 24 01:44 000036.sst.sblock.0
-rw-r--r--  4 gr  staff    13M Sep 24 01:44 000036.sst
-rw-r--r--  4 gr  staff    17M Sep 24 01:45 000037.sst.sblock.0
-rw-r--r--  4 gr  staff   670K Sep 24 01:45 000037.sst
*** -rw-r--r--  3 gr  staff    17M Sep 24 01:47 000038.sst.sblock.0
*** -rw-r--r--  3 gr  staff   670K Sep 24 01:47 000038.sst
drwxr-xr-x  4 gr  staff   128B Sep 24 01:47 intents
*** -rw-r--r--  1 gr  staff    15K Sep 24 01:47 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:47 CURRENT
*** -rw-r--r--  1 gr  staff   1.9K Sep 24 01:47 MANIFEST-000040

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
drwxr-xr-x  4 gr  staff   128B Sep 24 01:49 intents
*** -rw-r--r--  1 gr  staff    15K Sep 24 01:49 MANIFEST-000011
*** -rw-r--r--  1 gr  staff    16B Sep 24 01:49 CURRENT
*** -rw-r--r--  1 gr  staff   2.3K Sep 24 01:49 MANIFEST-000041
```
