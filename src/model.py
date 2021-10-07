import json
import string
import sys
import uuid
from datetime import datetime, date

#import now as now
'''
Draft model for python supporting manifest JSON object. 
Currently only using 1 Class Manifest. Stubbed out other classes for 
Database, Storage and Backup which may or may not work. 
'''
valid_manifest_status = ['init','vaild','error']
valid_location_types = ["s3"]
valid_database_types = ["ycql","ysql"]

now = datetime.now()

class Database():
    def __init__(self):
        name = string
        database_type = valid_database_types
        database_tables = []
        database_objects = []

class Storage():
    def __init__(self):
        pass

class Backup():
    def __init__(self):
        pass
#restore link is where is the file is ... not computed as current restore does
class Manifest():
    def __init__(self,manifest_id):
        self.manifest_id = ""
        self.manifest_name = ""
        self.manifest_type = ""
        self.manifest_universe_name = ""
        self.manifest_universe_id = ""
        self.manifest_create_date = ""
        self.manifest_status = ""
        self.manifest_diff_savepoint_number = ""
        self.database_name = ""
        self.database_type = ""
        self.database_tables = []
        self.database_objects = []
        self.storage_backup_location = ""
        self.storage_backup_location_type = ""
        self.storage_table_ids = []
        self.storage_tablet_ids = []
        self.storage_files = []
        self.storage_table_ids_dict = {}
        self.storage_tablet_ids_dict = {}
        self.storage_files_dict = {}
        self.backup_name = ""
        self.backup_id = ""
        self.backup_create_date = ""
        self.backup_start_time = ""
        self.backup_end_time = ""
        self .backup_messages = []
        self.backup_errors = []

    def to_json_dict(self):
        manifest_json = { "manifest": {
            "manifest_id": self.manifest_id,
            "create_date": self.create_date,
            "status": self.status
            }, "components": {  "database": {
            "name": self.database_name, "type": self.database_type, "database_tables": self.database_tables,"database_objects": self.database_objects
        }, "storage": {
            "backup_location": self.storage_backup_location,
            "backup_location_type": self.storage_backup_location_type,
            "table_id": self.storage_table_ids,
            "tablet_id": self.storage_tablet_ids,
            "files": self.storage_files
        }, "backup": {
            "name": self.backup_name,
            "create_date": self.create_date,
            "start_time": self.backup_start_time,
            "end_time": self.backup_end_time,
            "message": self.backup_messages,
            "error": self.backup_errors
        }
        }
        }

        return manifest_json

    def json_out(self):
        json_dict = self.to_json_dict()
        json_object = json.dumps(json_dict, indent=4)
        return json_object

def main():
    manifest_id = uuid.uuid1()
    test_class = Manifest(manifest_id)
    test_class.manifest_id = str(manifest_id)
    test_class.status = "init"
    test_class.database = "ycql"
    test_class.create_date = str(now.strftime("%d/%m/%Y %H:%M:%S"))

    json_out_dict = test_class.to_json_dict()
    print("Dict of manifest: ",json_out_dict)
    #json_object = json.dumps(json_out, indent=4)
    print("JSON of manifest: ")
    json_out = test_class.json_out()
    print(type(json_out))
    print(json_out)


if __name__ == "__main__":
   #main(sys.argv[1:])
    main()