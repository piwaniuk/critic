import argparse
import json
import os

import psycopg2


PREFERENCES_PATH = '/usr/share/critic/data/preferences.json'


def add_preference(db, item, data):
    relevance = data.get("relevance", {})
    print("ADD PREFERENCE", item)
    print("relevance", relevance)

    cursor = db.cursor()

    # create preferences entry
    cursor.execute(
        ('INSERT INTO preferences ' +
            '(item, type, description, per_system, per_user, per_repository, per_filter) ' +
            'VALUES (%s, %s, %s, %s, %s, %s, %s)'),
        (item, data["type"], data["description"],
            relevance.get("system", True), relevance.get("user", True),
            relevance.get("repository", False), relevance.get("filter", False)))

    # set default value
    if data["type"] == "string":
        cursor.execute(
            'INSERT INTO userpreferences (item, string) VALUES (%s, %s)',
            (item, data["default"]))
    else:
        cursor.execute(
            'INSERT INTO userpreferences (item, integer) VALUES (%s, %s)',
            (item, int(data["default"])))


def load_db_preferences(db):
    cursor = db.cursor()
    cursor.execute(
        ("SELECT preferences.item, type, integer, string, description " +
            "FROM preferences JOIN userpreferences USING (item) " +
            "WHERE uid IS NULL AND repository IS NULL AND filter is NULL"))
    preferences = {}
    for item, item_type, default_integer, default_string, description in cursor:
        data = { "type": item_type,
                 "description": description }
        if item_type == "string":
            data["default"] = default_string
        elif item_type == "boolean":
            data["default"] = bool(default_integer)
        else:
            data["default"] = default_integer
        preferences[item] = data
    return preferences


def load_json_preferences():
    with open(PREFERENCES_PATH) as preferences_file:
        new_preferences = json.load(preferences_file)
    return new_preferences


def do_migrate(db):
    # load preferences from json
    json_preferences = load_json_preferences()
    # load preferences from db
    db_preferences = load_db_preferences(db)

    for item in json_preferences.keys():
        if item not in db_preferences:
            add_preference(db, item, json_preferences[item])

    db.commit()
    db.close()


def runtime_migrate():
    import dbaccess
    do_migrate(dbaccess.connect())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)

    arguments = parser.parse_args()

    if arguments.runtime:
        runtime_migrate()
    else:
        os.setgid(arguments.gid)
        os.setuid(arguments.uid)

        db = psycopg2.connect(database="critic")
        do_migrate(db)


if __name__ == '__main__':
    main()
