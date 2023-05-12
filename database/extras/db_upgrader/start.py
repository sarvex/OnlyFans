import sys
import os


def start():
    print

try:
    if __name__ == "__main__":
        cwd = os.getcwd()
        cwd2 = os.path.dirname(__file__)
        x = os.path.realpath('../../../') if cwd == cwd2 else os.path.realpath('')
        sys.path.insert(0, x)
        while True:
            from helpers.db_helper import database_collection, run_revisions
            db_collection = database_collection()
            key_list = db_collection.__dict__.items()
            key_list = list(key_list)
            string = f""
            for count, (key, item) in enumerate(key_list):
                print
                string += f"{str(count)} = {key} | "
            print(string)
            x = input()
            # x = 0
            x = int(x)
            database_path = None
            if module := key_list[x][1]:
                api_type = os.path.basename(module.__file__)
                database_path = module.__file__
                filename = f"test_{api_type}"
                filename = filename.replace("py", "db")
                database_directory = os.path.dirname(database_path)
                final_database_path = os.path.join(database_directory, filename)
                alembic_directory = database_directory
                run_revisions(alembic_directory, final_database_path)
                print("DONE")
            else:
                print("Failed")
except Exception as e:
    input(e)
