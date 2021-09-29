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

* A manifest or dictionary of all the files from the previous snapshot is used to determine which files are already stored off-cluster.
* For the first backup all files are copied off-cluster along with the manifest file with the files off-cluster locations.

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

## Design

Differential backups will be implemented within the [yb_backup.py](https://github.com/yugabyte/yugabyte-db/blob/master/managed/devops/bin/yb_backup.py) program. This approach leverages all the complexities yb_backup.py addresess such as distributed backups, replication factor, and so forth. These are the action implemented to accomplish a differential backup:

* Create the manifest with the required meta-data to include table ids, tablet ids, and files in snapshots using python dictionaries and persisted in JSON files that are copied off-cluster.

* Calculate files to copy off-cluster by comparing with previous backup's manifest.

  * Each manifest file is persisted in off-cluster storage as is the SnapshotInfoPB and YSQLDump files (for SQL backups)
  * Use a naming convention for the manifest file to determine which is the manifest for the previous backup (or other mechanism) .
  * Load the previous' backup manifest and determine which files are new.

* Invoke primitives to copy and restore files instead ofdirectory copy primitives in use by the current distributed backup.

  * Iterate through the manifest dictionary and invoke the off-cluster file copy primitive.

* Determine what files to delete off-cluster.

   *  Files are removed when they exist for longer than the backup retention period and the number of restore points. 
      *  Restore points are the number of successful backups completed before the backup retention period. The interaction between retention points and backup retention period is detailed [here](#restore-points-and-backup-retention-period).
    *  Iterate through the files in the manifest to remove as needed using the file delete primitive.

## Manifest file structure

This is the proposed JSON structure for the manifest file. Additional fields may be added during implementation:
```
{
    "table_id": {
        "tablet_id": {
            "sst_file": {
                "location": "URI",
                "file_timestamp": "epoch_time_value",
                "version": 1
            }
        }
    }
}
```

## Example

The yb-sample-apps [SqlInserts](https://github.com/yugabyte/yb-sample-apps) workload created the files for this example. The workload creates one table and one tablet. A snapshot schedule with a 2 minute interval ran for 16 minutes to create the snapshot directories.

The snapshot directories are under this table and tablet directory: 

```
~/var/data/yb-data/tserver/data/rocksdb/table-000030ad000030008000000000004000/tablet-4b90c92c6a4b4a3aa03c6f941a8c7d1b.snapshots
```

and these are the 8 snapshot directories:

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
Each of the snapshot directories has 3 types of files: MANIFEST, CURRENT, and sst.
The MANIFEST and CURRENT files are always copied in each snapshot but only the new sst files in each snapshot are copied off-cluster.

The following diagram shows the how the sst snapshot files are copied and added to the manifest. The boxes are when an sst file is copied and the arrows represent the file is in the manifest until the snapsshot the arrow ends.  For example, the sst file 000021 is copied in the 1st snapshot and is in the manifest from the 2nd to the 5th snapshot. 

#### Differential Backup Diagram

![image](https://user-images.githubusercontent.com/84997113/135130246-3a59e21d-1949-48f0-8862-7b62f9e72ada.png)

The next sections list the in-cluster files and the manifest file for each snapshot of table_id '000030ad000030008000000000004000' and the tablet_id '4b90c92c6a4b4a3aa03c6f941a8c7d1b'

### 1st Snapshot

The first snapshot copies all files to off-cluster storage.

#### Files

```
4160b771-2620-44f2-a482-3f94e796aefc:
-rw-r--r--  5 gr  staff    7212668 Sep 24 01:24 000021.sst
-rw-r--r--  5 gr  staff  150036596 Sep 24 01:24 000021.sst.sblock.0
-rw-r--r--  5 gr  staff    2886822 Sep 24 01:33 000027.sst
-rw-r--r--  5 gr  staff   81408353 Sep 24 01:33 000027.sst.sblock.0
-rw-r--r--  3 gr  staff      66314 Sep 24 01:33 000028.sst
-rw-r--r--  3 gr  staff        317 Sep 24 01:33 000028.sst.sblock.0
-rw-r--r--  3 gr  staff     551333 Sep 24 01:35 000030.sst
-rw-r--r--  3 gr  staff   15631584 Sep 24 01:35 000030.sst.sblock.0
-rw-r--r--  1 gr  staff         16 Sep 24 01:35 CURRENT
-rw-r--r--  1 gr  staff      10258 Sep 24 01:35 MANIFEST-000011
-rw-r--r--  1 gr  staff       2379 Sep 24 01:35 MANIFEST-000032
drwxr-xr-x  4 gr  staff        128 Sep 24 01:35 intents

4160b771-2620-44f2-a482-3f94e796aefc/intents:
-rw-r--r--  1 gr  staff   16 Sep 24 01:35 CURRENT
-rw-r--r--  1 gr  staff  704 Sep 24 01:35 MANIFEST-000010
```

#### Manifest

```
{
    "000030ad00003000800000000000400": {
        "4b90c92c6a4b4a3aa03c6f941a8c7d1b": {
            "000021.sst": {
                "location": "URI",
                "file_timestamp": "2021-09-24 01:24:28.555850452",
                "version": 1
            },
            "000021.sst.sblock.0": {
                "location": "URI",
                "file_timestamp": "2021-09-24 01:24:28.555487300",
                "version": 1
            },
            "000027.sst": {
                "location": "URI",
                "file_timestamp": "2021-09-24 01:33:54.949000435",
                "version": 1
            },
            "000027.sst.sblock.0": {
                "location": "URI",
                "file_timestamp": "2021-09-24 01:33:54.948762402",
                "version": 1
            },
            "000028.sst": {
                "location": "URI",
                "file_timestamp": "2021-09-24 01:33:46.858613591",
                "version": 1
            },
            "000028.sst.sblock.0": {
                "location": "URI",
                "file_timestamp": "2021-09-24 01:33:46.858338914",
                "version": 1
            },
            "000030.sst": {
                "location": "URI",
                "file_timestamp": "2021-09-24 01:35:17.452900637",
                "version": 1
            },
            "000030.sst.sblock.0": {
                "location": "URI",
                "file_timestamp": "2021-09-24 01:35:17.452429701",
                "version": 1
            }
        }
    }
}
```
### 2nd Snapshot

As shown in the [Differential Backup diagram](####-differential-backup-diagram), the second snapshot copies the new "000031" sst files.
All the other files in the directory have been copied off-cluster in the previous snapshot so they are entries in this snapshot's manifest. 

#### Files
```
81b0ce71-21fc-402f-8af3-2dea4cc7a7a9:
-rw-r--r--  5 gr  staff    7212668 Sep 24 01:24 000021.sst
-rw-r--r--  5 gr  staff  150036596 Sep 24 01:24 000021.sst.sblock.0
-rw-r--r--  5 gr  staff    2886822 Sep 24 01:33 000027.sst
-rw-r--r--  5 gr  staff   81408353 Sep 24 01:33 000027.sst.sblock.0
-rw-r--r--  3 gr  staff      66314 Sep 24 01:33 000028.sst
-rw-r--r--  3 gr  staff        317 Sep 24 01:33 000028.sst.sblock.0
-rw-r--r--  3 gr  staff     551333 Sep 24 01:35 000030.sst
-rw-r--r--  3 gr  staff   15631584 Sep 24 01:35 000030.sst.sblock.0
-rw-r--r--  2 gr  staff     754925 Sep 24 01:37 000031.sst
-rw-r--r--  2 gr  staff   20169409 Sep 24 01:37 000031.sst.sblock.0
-rw-r--r--  1 gr  staff         16 Sep 24 01:37 CURRENT
-rw-r--r--  1 gr  staff      10884 Sep 24 01:37 MANIFEST-000011
-rw-r--r--  1 gr  staff       2783 Sep 24 01:37 MANIFEST-000033
drwxr-xr-x  4 gr  staff        128 Sep 24 01:37 intents

./intents:
total 16
-rw-r--r--  1 gr  staff   16 Sep 24 01:37 CURRENT
-rw-r--r--  1 gr  staff  814 Sep 24 01:37 MANIFEST-000010
```
#### Manifest
```
{
    "000030ad00003000800000000000400": {
        "4b90c92c6a4b4a3aa03c6f941a8c7d1b": {
            "000021.sst": {
                "location": "URI_of_file_000021.sst",
                "file_timestamp": "2021-09-24 01:24:28.555850452",
                "version": 1
            },
            "000021.sst.sblock.0": {
                "location": "URI_of_file_000021.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:24:28.555487300",
                "version": 1
            },
            "000027.sst": {
                "location": "URI_of_file_000027.sst",
                "file_timestamp": "2021-09-24 01:33:54.949000435",
                "version": 1
            },
            "000027.sst.sblock.0": {
                "location": "URI_of_file_000027.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:33:54.948762402",
                "version": 1
            },
            "000028.sst": {
                "location": "URI_of_file_000028.sst",
                "file_timestamp": "2021-09-24 01:33:46.858613591",
                "version": 1
            },
            "000028.sst.sblock.0": {
                "location": "URI_of_file_000028.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:33:46.858338914",
                "version": 1
            },
            "000030.sst": {
                "location": "URI_of_file_000030.sst",
                "file_timestamp": "2021-09-24 01:35:17.452900637",
                "version": 1
            },
            "000030.sst.sblock.0": {
                "location": "URI_of_file_000030.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:35:17.452429701",
                "version": 1
            },
            "000031.sst": {
                "location": "URI_of_file_000031.sst",
                "file_timestamp": " 2021-09-24 01:37:22.778258342",
                "version": 1
            },
            "000031.sst.sblock.0": {
                "location": "URI_of_file_000031.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:37:22.777837785",
                "version": 1
            }
        }
    }
}
```

### 3rd snapshot

As shown in the [Differential Backup diagram](####-differential-backup-diagram), the 3rd snapshot copies the new "000032" sst files.
All the other files in the directory have been copied off-cluster so they become entries in this snapshots manifest. 

#### Files

```
83a006ce-40e5-408e-8f03-fba2e1c5f546:
-rw-r--r--  5 gr  staff    7212668 Sep 24 01:24 000021.sst
-rw-r--r--  5 gr  staff  150036596 Sep 24 01:24 000021.sst.sblock.0
-rw-r--r--  5 gr  staff    2886822 Sep 24 01:33 000027.sst
-rw-r--r--  5 gr  staff   81408353 Sep 24 01:33 000027.sst.sblock.0
-rw-r--r--  3 gr  staff      66314 Sep 24 01:33 000028.sst
-rw-r--r--  3 gr  staff        317 Sep 24 01:33 000028.sst.sblock.0
-rw-r--r--  3 gr  staff     551333 Sep 24 01:35 000030.sst
-rw-r--r--  3 gr  staff   15631584 Sep 24 01:35 000030.sst.sblock.0
-rw-r--r--  2 gr  staff     754925 Sep 24 01:37 000031.sst
-rw-r--r--  2 gr  staff   20169409 Sep 24 01:37 000031.sst.sblock.0
-rw-r--r--  1 gr  staff     688366 Sep 24 01:39 000032.sst
-rw-r--r--  1 gr  staff   19410778 Sep 24 01:39 000032.sst.sblock.0
-rw-r--r--  1 gr  staff         16 Sep 24 01:39 CURRENT
-rw-r--r--  1 gr  staff      11510 Sep 24 01:39 MANIFEST-000011
-rw-r--r--  1 gr  staff       3187 Sep 24 01:39 MANIFEST-000034
drwxr-xr-x  4 gr  staff        128 Sep 24 01:39 intents

./intents:
total 16
-rw-r--r--  1 gr  staff   16 Sep 24 01:39 CURRENT
-rw-r--r--  1 gr  staff  924 Sep 24 01:39 MANIFEST-000010
```

#### Manifest
```
{
    "000030ad00003000800000000000400": {
        "4b90c92c6a4b4a3aa03c6f941a8c7d1b": {
            "000021.sst": {
                "location": "URI_of_file_000021.sst",
                "file_timestamp": "2021-09-24 01:24:28.555850452",
                "version": 1
            },
            "000021.sst.sblock.0": {
                "location": "URI_of_file_000021.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:24:28.555487300",
                "version": 1
            },
            "000027.sst": {
                "location": "URI_of_file_000027.sst",
                "file_timestamp": "2021-09-24 01:33:54.949000435",
                "version": 1
            },
            "000027.sst.sblock.0": {
                "location": "URI_of_file_000027.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:33:54.948762402",
                "version": 1
            },
            "000028.sst": {
                "location": "URI_of_file_000028.sst",
                "file_timestamp": "2021-09-24 01:33:46.858613591",
                "version": 1
            },
            "000028.sst.sblock.0": {
                "location": "URI_of_file_000028.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:33:46.858338914",
                "version": 1
            },
            "000030.sst": {
                "location": "URI_of_file_000030.sst",
                "file_timestamp": "2021-09-24 01:35:17.452900637",
                "version": 1
            },
            "000030.sst.sblock.0": {
                "location": "URI_of_file_000030.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:35:17.452429701",
                "version": 1
            },
            "000031.sst": {
                "location": "URI_of_file_000031.sst",
                "file_timestamp": " 2021-09-24 01:37:22.778258342",
                "version": 1
            },
            "000031.sst.sblock.0": {
                "location": "URI_of_file_000031.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:37:22.777837785",
                "version": 1
            },
            "000032.sst": {
                "location": "URI_of_file_000032.sst",
                "file_timestamp": "2021-09-24 01:37:22.778258342",
                "version": 1
            },
            "000032.sst.sblock.0": {
                "location": "URI_of_file_000032.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:39:27.857307407",
                "version": 1
            }
        }
    }
}
```

### 4th Snapshot

The 4th snapshot copies the new '33' and '34'files and updates the manifest.

The manifest entry for this snapshot does not have files 28, 30, 31, and 32 sst files from the previous snapshot as these files have been compacted

Files 21 and 27 are present in this snapshot.

#### Files
```
./7f3c9719-69a6-4eb7-a86e-0ad368b6a322:
-rw-r--r--  5 gr  staff    7212668 Sep 24 01:24 000021.sst
-rw-r--r--  5 gr  staff  150036596 Sep 24 01:24 000021.sst.sblock.0
-rw-r--r--  5 gr  staff    2886822 Sep 24 01:33 000027.sst
-rw-r--r--  5 gr  staff   81408353 Sep 24 01:33 000027.sst.sblock.0
-rw-r--r--  2 gr  staff    1925418 Sep 24 01:39 000033.sst
-rw-r--r--  2 gr  staff   54738545 Sep 24 01:39 000033.sst.sblock.0
-rw-r--r--  2 gr  staff     687498 Sep 24 01:41 000034.sst
-rw-r--r--  2 gr  staff   18945951 Sep 24 01:41 000034.sst.sblock.0
-rw-r--r--  1 gr  staff         16 Sep 24 01:41 CURRENT
-rw-r--r--  1 gr  staff      12674 Sep 24 01:41 MANIFEST-000011
-rw-r--r--  1 gr  staff       2380 Sep 24 01:41 MANIFEST-000036
drwxr-xr-x  4 gr  staff        128 Sep 24 01:41 intents

./intents:
total 16
-rw-r--r--  1 gr  staff    16 Sep 24 01:41 CURRENT
-rw-r--r--  1 gr  staff  1034 Sep 24 01:41 MANIFEST-0000106
```
#### Manifest
```
{
    "000030ad00003000800000000000400": {
        "4b90c92c6a4b4a3aa03c6f941a8c7d1b": {
            "000021.sst": {
                "location": "URI_of_file_000021.sst",
                "file_timestamp": "2021-09-24 01:24:28.555850452",
                "version": 1
            },
            "000021.sst.sblock.0": {
                "location": "URI_of_file_000021.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:24:28.555487300",
                "version": 1
            },
            "000027.sst": {
                "location": "URI_of_file_000027.sst",
                "file_timestamp": "2021-09-24 01:33:54.949000435",
                "version": 1
            },
            "000027.sst.sblock.0": {
                "location": "URI_of_file_000027.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:33:54.948762402",
                "version": 1
            },
            "000033.sst": {
                "location": "URI_of_file_000033.sst",
                "file_timestamp": "2021-09-24 01:39:32.75494731",
                "version": 1
            },
            "000033.sst.sblock.0": {
                "location": "URI_of_file_000033.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:39:32.754947318",
                "version": 1
            },
            "000034.sst": {
                "location": "URI_of_file_000034.sst",
                "file_timestamp": "2021-09-24 01:41:32.882447921",
                "version": 1
            },
            "000034.sst.sblock.0": {
                "location": "URI_of_file_000034.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:41:32.882087113",
                "version": 1
            }
        }
    }
}
```
### 5th Snapshot

The 5th snapshot copies the new '35' files and updates the manifest.

All files from the 4th snapshot are present and become entries in this snapshot's manifest.

#### Files

```
./24ebc93b-92a1-43cd-b177-699636f47287:
-rw-r--r--  5 gr  staff    7212668 Sep 24 01:24 000021.sst
-rw-r--r--  5 gr  staff  150036596 Sep 24 01:24 000021.sst.sblock.0
-rw-r--r--  5 gr  staff    2886822 Sep 24 01:33 000027.sst
-rw-r--r--  5 gr  staff   81408353 Sep 24 01:33 000027.sst.sblock.0
-rw-r--r--  2 gr  staff    1925418 Sep 24 01:39 000033.sst
-rw-r--r--  2 gr  staff   54738545 Sep 24 01:39 000033.sst.sblock.0
-rw-r--r--  2 gr  staff     687498 Sep 24 01:41 000034.sst
-rw-r--r--  2 gr  staff   18945951 Sep 24 01:41 000034.sst.sblock.0
-rw-r--r--  1 gr  staff     688204 Sep 24 01:43 000035.sst
-rw-r--r--  1 gr  staff   19292590 Sep 24 01:43 000035.sst.sblock.0
-rw-r--r--  1 gr  staff         16 Sep 24 01:43 CURRENT
-rw-r--r--  1 gr  staff      13300 Sep 24 01:43 MANIFEST-000011
-rw-r--r--  1 gr  staff       2784 Sep 24 01:43 MANIFEST-000037
drwxr-xr-x  4 gr  staff        128 Sep 24 01:43 intents

./intents:
total 16
-rw-r--r--  1 gr  staff    16 Sep 24 01:43 CURRENT
-rw-r--r--  1 gr  staff  1144 Sep 24 01:43 MANIFEST-000010
```
#### Manifest

```
{
    "000030ad00003000800000000000400": {
        "4b90c92c6a4b4a3aa03c6f941a8c7d1b": {
            "000021.sst": {
                "location": "URI_of_file_000021.sst",
                "file_timestamp": "2021-09-24 01:24:28.555850452",
                "version": 1
            },
            "000021.sst.sblock.0": {
                "location": "URI_of_file_000021.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:24:28.555487300",
                "version": 1
            },
            "000027.sst": {
                "location": "URI_of_file_000027.sst",
                "file_timestamp": "2021-09-24 01:33:54.949000435",
                "version": 1
            },
            "000027.sst.sblock.0": {
                "location": "URI_of_file_000027.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:33:54.948762402",
                "version": 1
            },
            "000033.sst": {
                "location": "URI_of_file_000033.sst",
                "file_timestamp": "2021-09-24 01:39:32.75494731",
                "version": 1
            },
            "000033.sst.sblock.0": {
                "location": "URI_of_file_000033.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:39:32.754947318",
                "version": 1
            },
            "000034.sst": {
                "location": "URI_of_file_000034.sst",
                "file_timestamp": "2021-09-24 01:41:32.882447921",
                "version": 1
            },
            "000034.sst.sblock.0": {
                "location": "URI_of_file_000034.sst.sblock.0",
                "file_timestamp": "2021-09-24 01:41:32.882087113",
                "version": 1
            },
            "000035.sst": {
                "location": "URI_of_file_000035.sst",
                "file_timestamp": "epoch_timestamp_of_file_000035.sst",
                "version": 1
            },
            "000035.sst.sblock.0": {
                "location": "URI_of_file_000035.sst.sblock.0",
                "file_timestamp": "epoch_timestamp_of_file_000035.sst.sblock.0",
                "version": 1
            }
        }
    }
}
```
### 6th Snapshot

The 6th snapshot copies the new '38' files and updates the manifest.

All files from the 4th snapshot are present and become entries in this snapshot's manifest.

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


# Restore Points and Backup Retention Period

In addition to snapshot recoveries base on time, restore points are a mechanism to restore files beyond the backup history retention up to a discrete number of retention points as set though configuration.

Files that would be removed by backup retention time would be moved to a location where restore points use to recover.
gr@mbPro ~/YB/differentialBackup %
