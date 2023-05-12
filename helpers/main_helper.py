from database.databases.user_data.models.api_table import api_table
from apis import api_helper
import asyncio
import copy
from aiohttp.client import ClientSession

from aiohttp_socks.connector import ProxyConnector
from database.databases.user_data.models.media_table import template_media_table
import json
import math
import os
import platform
import random
import re
import shutil
import string
import traceback
from datetime import datetime
from itertools import zip_longest
from multiprocessing.dummy import Pool as ThreadPool
from types import SimpleNamespace
from typing import Any, Optional, Tuple, Union

import classes.make_settings as make_settings
import classes.prepare_webhooks as prepare_webhooks
import psutil
import requests
import ujson
from aiohttp.client_exceptions import (
    ClientOSError,
    ClientPayloadError,
    ContentTypeError,
    ServerDisconnectedError,
)
from aiohttp.client_reqrep import ClientResponse
from apis.onlyfans import onlyfans as OnlyFans
from apis.onlyfans.classes import create_user
from apis.onlyfans.classes.create_auth import create_auth
from apis.onlyfans.classes.extras import content_types
from bs4 import BeautifulSoup
from classes.prepare_metadata import format_variables, prepare_reformat
from mergedeep import Strategy, merge
from sqlalchemy import inspect
from sqlalchemy.orm.session import Session
from tqdm import tqdm

import helpers.db_helper as db_helper

json_global_settings = {}
min_drive_space = 0
webhooks = {}
max_threads = -1
os_name = platform.system()
proxies = None
cert = None


def assign_vars(config):
    global json_global_settings, min_drive_space, webhooks, max_threads, proxies, cert

    json_config = config
    json_global_settings = json_config["settings"]
    min_drive_space = json_global_settings["min_drive_space"]
    webhooks = json_global_settings["webhooks"]
    max_threads = json_global_settings["max_threads"]
    proxies = json_global_settings["proxies"]
    cert = json_global_settings["cert"]


def rename_duplicates(seen, filename):
    filename_lower = filename.lower()
    if filename_lower in seen:
        count = 1
        while filename_lower in seen:
            filename = f"{filename} ({str(count)})"
            filename_lower = filename.lower()
            count += 1
    seen.add(filename_lower)
    return [seen, filename]


def parse_links(site_name, input_link):
    if site_name in {"onlyfans", "starsavn"}:
        return input_link.rsplit("/", 1)[-1]
    if site_name in {"patreon", "fourchan", "bbwchan"}:
        if "catalog" in input_link:
            input_link = input_link.split("/")[1]
            print(input_link)
            return input_link
        if input_link[-1:] == "/":
            input_link = input_link.split("/")[3]
            return input_link
        if "4chan.org" not in input_link:
            return input_link


def clean_text(string, remove_spaces=False):
    matches = ["\n", "<br>"]
    for m in matches:
        string = string.replace(m, " ").strip()
    string = " ".join(string.split())
    string = BeautifulSoup(string, "lxml").get_text()
    SAFE_PTN = r"[|\^&+\-%*/=!:\"?><]"
    string = re.sub(SAFE_PTN, " ", string.strip()).strip()
    if remove_spaces:
        string = string.replace(" ", "_")
    return string


def format_media_set(media_set):
    merged = merge({}, *media_set, strategy=Strategy.ADDITIVE)
    if "directories" in merged:
        for directory in merged["directories"]:
            os.makedirs(directory, exist_ok=True)
        merged.pop("directories")
    return merged


async def format_image(filepath: str, timestamp: float):
    if json_global_settings["helpers"]["reformat_media"]:
        while True:
            try:
                if os_name == "Windows":
                    from win32_setctime import setctime

                    setctime(filepath, timestamp)
                    # print(f"Updated Creation Time {filepath}")
                os.utime(filepath, (timestamp, timestamp))
                # print(f"Updated Modification Time {filepath}")
            except Exception as e:
                continue
            break


async def async_downloads(
    download_list: list[template_media_table], subscription: create_user
):
    async def run(download_list: list[template_media_table]):
        session_m = subscription.session_manager
        proxies = session_m.proxies
        proxy = (
            session_m.proxies[random.randint(0, len(proxies) - 1)] if proxies else ""
        )
        connector = ProxyConnector.from_url(proxy) if proxy else None
        async with ClientSession(
            connector=connector,
            cookies=session_m.auth.auth_details.cookie.format(),
            read_timeout=None,
        ) as session:
            tasks = []
            # Get content_lengths
            for download_item in download_list:
                link = download_item.link
                if link:
                    task = asyncio.ensure_future(
                        session_m.json_request(
                            download_item.link,
                            session,
                            method="HEAD",
                            json_format=False,
                        )
                    )
                    tasks.append(task)
            responses = await asyncio.gather(*tasks)
            tasks.clear()

            async def check(
                download_item: template_media_table, response: ClientResponse
            ):
                filepath = os.path.join(download_item.directory, download_item.filename)
                response_status = False
                if response.status == 200:
                    response_status = True
                    if response.content_length:
                        download_item.size = response.content_length

                if os.path.exists(filepath):
                    if os.path.getsize(filepath) == response.content_length:
                        download_item.downloaded = True
                    else:
                        return download_item
                else:
                    if response_status:
                        return download_item

            for download_item in download_list:
                temp_response = [
                    response
                    for response in responses
                    if response and str(response.url) == download_item.link
                ]
                if temp_response:
                    temp_response = temp_response[0]
                    task = check(download_item, temp_response)
                    tasks.append(task)
            result = await asyncio.gather(*tasks)
            download_list = [x for x in result if x]
            tasks.clear()
            progress_bar = None
            if download_list:
                progress_bar = download_session()
                progress_bar.start(unit="B", unit_scale=True, miniters=1)
                [progress_bar.update_total_size(x.size) for x in download_list]

            async def process_download(download_item: template_media_table):
                while True:
                    result = await session_m.download_content(
                        download_item, session, progress_bar, subscription
                    )
                    if result:
                        response, download_item = result.values()
                        if response:
                            download_path = os.path.join(
                                download_item.directory, download_item.filename
                            )
                            status_code = await write_data(
                                response, download_path, progress_bar
                            )
                            if not status_code:
                                pass
                            elif status_code == 1:
                                continue
                            elif status_code == 2:
                                break
                            timestamp = download_item.created_at.timestamp()
                            await format_image(download_path, timestamp)
                            download_item.size = response.content_length
                            download_item.downloaded = True
                    break

            max_threads = api_helper.calculate_max_threads(session_m.max_threads)
            download_groups = grouper(max_threads, download_list)
            for download_group in download_groups:
                tasks = []
                for download_item in download_group:
                    task = process_download(download_item)
                    if task:
                        tasks.append(task)
                await asyncio.gather(*tasks)
            if isinstance(progress_bar, download_session):
                progress_bar.close()
            return True

    results = await asyncio.ensure_future(run(download_list))
    return results


def filter_metadata(datas):
    for key, item in datas.items():
        for items in item["valid"]:
            for item2 in items:
                item2.pop("session")
    return datas


def import_archive(archive_path) -> Any:
    metadata = {}
    if os.path.exists(archive_path) and os.path.getsize(archive_path):
        with open(archive_path, "r", encoding="utf-8") as outfile:
            while not metadata:
                try:
                    metadata = ujson.load(outfile)
                except OSError as e:
                    print(traceback.format_exc())
    return metadata


def legacy_database_fixer(database_path, database, database_name, database_exists):
    database_directory = os.path.dirname(database_path)
    old_database_path = database_path
    old_filename = os.path.basename(old_database_path)
    new_filename = f"Pre_Alembic_{old_filename}"
    pre_alembic_path = os.path.join(database_directory, new_filename)
    pre_alembic_database_exists = False
    if os.path.exists(pre_alembic_path):
        database_path = pre_alembic_path
        pre_alembic_database_exists = True
    if database_exists:
        Session, engine = db_helper.create_database_session(database_path)
        database_session = Session()
        result = inspect(engine).has_table("alembic_version")
        if not result and not pre_alembic_database_exists:
            os.rename(old_database_path, pre_alembic_path)
            pre_alembic_database_exists = True
    if pre_alembic_database_exists:
        Session, engine = db_helper.create_database_session(pre_alembic_path)
        database_session = Session()
        api_table = database.api_table()
        media_table = database.media_table()
        legacy_api_table = api_table.legacy(database_name)
        legacy_media_table = media_table.legacy()
        result = database_session.query(legacy_api_table)
        post_db = result.all()
        datas = []
        for post in post_db:
            post_id = post.id
            created_at = post.created_at
            new_item = {
                "post_id": post_id,
                "text": post.text,
                "price": post.price,
                "paid": post.paid,
                "postedAt": created_at,
                "medias": [],
            }
            result2 = database_session.query(legacy_media_table)
            media_db = result2.filter_by(post_id=post_id).all()
            for media in media_db:
                new_item2 = {
                    "media_id": media.id,
                    "post_id": media.post_id,
                    "links": [media.link],
                    "directory": media.directory,
                    "filename": media.filename,
                    "size": media.size,
                    "media_type": media.media_type,
                    "downloaded": media.downloaded,
                    "created_at": created_at,
                }
                new_item["medias"].append(new_item2)
            datas.append(new_item)
        print
        database_session.close()
        export_sqlite2(old_database_path, datas, database_name, legacy_fixer=True)


async def fix_sqlite(
    profile_directory,
    download_directory,
    metadata_directory,
    format_directories,
    authed: create_auth,
    site_name,
    username,
    metadata_directory_format,
):
    items = content_types().__dict__.items()
    final_metadatas = []
    for api_type, value in items:
        mandatory_directories = {
            "profile_directory": profile_directory,
            "download_directory": download_directory,
            "metadata_directory": metadata_directory,
        }
        formatted_directories = await format_directories(
            mandatory_directories,
            authed,
            site_name,
            username,
            metadata_directory_format,
            "",
            api_type,
        )
        final_metadata_directory = formatted_directories["metadata_directory"]
        if all(final_metadata_directory != x for x in final_metadatas):
            final_metadatas.append(final_metadata_directory)
        print
    print
    for final_metadata in final_metadatas:
        archived_database_path = os.path.join(final_metadata, "Archived.db")
        if os.path.exists(archived_database_path):
            Session2, engine = db_helper.create_database_session(archived_database_path)
            database_session: Session = Session2()
            cwd = os.getcwd()
            for api_type, value in items:
                database_path = os.path.join(final_metadata, f"{api_type}.db")
                database_name = api_type.lower()
                alembic_location = os.path.join(
                    cwd, "database", "archived_databases", database_name
                )
                if result := inspect(engine).has_table(database_name):
                    db_helper.run_migrations(alembic_location, archived_database_path)
                    db_helper.run_migrations(alembic_location, database_path)
                    Session3, engine2 = db_helper.create_database_session(database_path)
                    db_collection = db_helper.database_collection()
                    database_session2: Session = Session3()
                    database = db_collection.database_picker("user_data")
                    if not database:
                        return
                    table_name = database.table_picker(api_type, True)
                    if not table_name:
                        return
                    archived_result = database_session.query(table_name).all()
                    for item in archived_result:
                        result2 = (
                            database_session2.query(table_name)
                            .filter(table_name.post_id == item.post_id)
                            .first()
                        )
                        if not result2:
                            item2 = item.__dict__
                            item2.pop("id")
                            item2.pop("_sa_instance_state")
                            item = table_name(**item2)
                            item.archived = True
                            database_session2.add(item)
                    database_session2.commit()
                    database_session2.close()
            database_session.commit()
            database_session.close()
            os.remove(archived_database_path)


def export_sqlite2(archive_path, datas, parent_type, legacy_fixer=False):
    metadata_directory = os.path.dirname(archive_path)
    os.makedirs(metadata_directory, exist_ok=True)
    cwd = os.getcwd()
    api_type: str = os.path.basename(archive_path).removesuffix(".db")
    database_path = archive_path
    database_name = parent_type if parent_type else api_type
    database_name = database_name.lower()
    db_collection = db_helper.database_collection()
    database = db_collection.database_picker(database_name)
    if not database:
        return
    alembic_location = os.path.join(cwd, "database", "databases", database_name)
    database_exists = os.path.exists(database_path)
    if database_exists and os.path.getsize(database_path) == 0:
        os.remove(database_path)
        database_exists = False
    if not legacy_fixer:
        legacy_database_fixer(database_path, database, database_name, database_exists)
    db_helper.run_migrations(alembic_location, database_path)
    print
    Session, engine = db_helper.create_database_session(database_path)
    database_session = Session()
    api_table = database.api_table
    media_table = database.media_table

    for post in datas:
        post_id = post["post_id"]
        date_object = None
        if postedAt := post["postedAt"]:
            if not isinstance(postedAt, datetime):
                date_object = datetime.strptime(postedAt, "%d-%m-%Y %H:%M:%S")
            else:
                date_object = postedAt
        result = database_session.query(api_table)
        post_db = result.filter_by(post_id=post_id).first()
        if not post_db:
            post_db = api_table()
        post_db.post_id = post_id
        post_db.text = post["text"]
        if post["price"] is None:
            post["price"] = 0
        post_db.price = post["price"]
        post_db.paid = post["paid"]
        post_db.archived = post["archived"]
        if date_object:
            post_db.created_at = date_object
        database_session.add(post_db)
        for media in post["medias"]:
            if media["media_type"] == "Texts":
                continue
            media_id = media.get("media_id", None)
            result = database_session.query(media_table)
            media_db = result.filter_by(media_id=media_id).first()
            if not media_db:
                media_db = result.filter_by(
                    filename=media["filename"], created_at=date_object
                ).first()
            if not media_db:
                media_db = media_table()
            if legacy_fixer:
                media_db.size = media["size"]
                media_db.downloaded = media["downloaded"]
            media_db.media_id = media_id
            media_db.post_id = post_id
            media_db.link = media["links"][0]
            media_db.preview = media.get("preview", False)
            media_db.directory = media["directory"]
            media_db.filename = media["filename"]
            media_db.api_type = api_type
            media_db.media_type = media["media_type"]
            media_db.linked = media.get("linked", None)
            if date_object:
                media_db.created_at = date_object
            database_session.add(media_db)
            print
        print
    print

    database_session.commit()
    database_session.close()
    return Session, api_type, database


def legacy_sqlite_updater(
    legacy_metadata_path: str,
    api_type: str,
    subscription: create_user,
    delete_metadatas: list,
):
    final_result = []
    if os.path.exists(legacy_metadata_path):
        cwd = os.getcwd()
        alembic_location = os.path.join(
            cwd, "database", "archived_databases", api_type.lower()
        )
        db_helper.run_migrations(alembic_location, legacy_metadata_path)
        database_name = "user_data"
        session, engine = db_helper.create_database_session(legacy_metadata_path)
        database_session: Session = session()
        db_collection = db_helper.database_collection()
        if database := db_collection.database_picker(database_name):
            if api_type == "Messages":
                api_table_table = database.table_picker(api_type, True)
            else:
                api_table_table = database.table_picker(api_type)
            media_table_table = database.media_table.media_legacy_table
            if api_table_table:
                result = database_session.query(api_table_table).all()
                result2 = database_session.query(media_table_table).all()
                for item in result:
                    item = item.__dict__
                    item["medias"] = []
                    for item2 in result2:
                        if item["post_id"] != item2.post_id:
                            continue
                        item2 = item2.__dict__
                        item2["links"] = [item2["link"]]
                        item["medias"].append(item2)
                        print
                    item["user_id"] = subscription.id
                    item["postedAt"] = item["created_at"]
                    final_result.append(item)
                delete_metadatas.append(legacy_metadata_path)
        database_session.close()
    return final_result, delete_metadatas


def export_sqlite(database_path: str, api_type, datas):
    metadata_directory = os.path.dirname(database_path)
    os.makedirs(metadata_directory, exist_ok=True)
    database_name = os.path.basename(database_path).replace(".db", "")
    cwd = os.getcwd()
    alembic_location = os.path.join(cwd, "database", "databases", database_name.lower())
    db_helper.run_migrations(alembic_location, database_path)
    Session, engine = db_helper.create_database_session(database_path)
    db_collection = db_helper.database_collection()
    database = db_collection.database_picker(database_name)
    if not database:
        return
    database_session = Session()
    api_table = database.table_picker(api_type)
    if not api_table:
        return
    for post in datas:
        post_id = post["post_id"]
        postedAt = post["postedAt"]
        date_object = None
        if postedAt:
            if not isinstance(postedAt, datetime):
                date_object = datetime.strptime(postedAt, "%d-%m-%Y %H:%M:%S")
            else:
                date_object = postedAt
        result = database_session.query(api_table)
        post_db = result.filter_by(post_id=post_id).first()
        if not post_db:
            post_db = api_table()
        if api_type == "Messages":
            post_db.user_id = post["user_id"]
        post_db.post_id = post_id
        post_db.text = post["text"]
        if post["price"] is None:
            post["price"] = 0
        post_db.price = post["price"]
        post_db.paid = post["paid"]
        post_db.archived = post["archived"]
        if date_object:
            post_db.created_at = date_object
        database_session.add(post_db)
        for media in post["medias"]:
            if media["media_type"] == "Texts":
                continue
            created_at = media["created_at"]
            if not isinstance(created_at, datetime):
                date_object = datetime.strptime(created_at, "%d-%m-%Y %H:%M:%S")
            else:
                date_object = postedAt
            media_id = media.get("media_id", None)
            result = database_session.query(database.media_table)
            media_db = result.filter_by(media_id=media_id).first()
            if not media_db:
                media_db = result.filter_by(
                    filename=media["filename"], created_at=date_object
                ).first()
            if not media_db:
                media_db = database.media_table()
            media_db.media_id = media_id
            media_db.post_id = post_id
            if "_sa_instance_state" in post:
                media_db.size = media["size"]
                media_db.downloaded = media["downloaded"]
            media_db.link = media["links"][0]
            media_db.preview = media.get("preview", False)
            media_db.directory = media["directory"]
            media_db.filename = media["filename"]
            media_db.api_type = api_type
            media_db.media_type = media["media_type"]
            media_db.linked = media.get("linked", None)
            if date_object:
                media_db.created_at = date_object
            database_session.add(media_db)
            print
        print
    print
    database_session.commit()
    database_session.close()
    return Session, api_type, database


def format_paths(j_directories, site_name):
    return list(j_directories)


async def reformat(prepared_format: prepare_reformat, unformatted):
    post_id = prepared_format.post_id
    media_id = prepared_format.media_id
    date = prepared_format.date
    text = prepared_format.text
    value = "Free"
    maximum_length = prepared_format.maximum_length
    text_length = prepared_format.text_length
    post_id = "" if post_id is None else str(post_id)
    media_id = "" if media_id is None else str(media_id)
    extra_count = 0
    if type(date) is str:
        format_variables2 = format_variables()
        if date not in [format_variables2.date, ""]:
            date = datetime.strptime(date, "%d-%m-%Y %H:%M:%S")
            date = date.strftime(prepared_format.date_format)
    else:
        if date != None:
            date = date.strftime(prepared_format.date_format)
    has_text = False
    if "{text}" in unformatted:
        has_text = True
        text = clean_text(text)
        extra_count = len("{text}")
    if (
        "{value}" in unformatted
        and prepared_format.price
        and not prepared_format.preview
    ):
        value = "Paid"
    directory = prepared_format.directory
    path = unformatted.replace("{site_name}", prepared_format.site_name)
    path = path.replace(
        "{first_letter}", prepared_format.model_username[0].capitalize()
    )
    path = path.replace("{post_id}", post_id)
    path = path.replace("{media_id}", media_id)
    path = path.replace("{profile_username}", prepared_format.profile_username)
    path = path.replace("{model_username}", prepared_format.model_username)
    path = path.replace("{api_type}", prepared_format.api_type)
    path = path.replace("{media_type}", prepared_format.media_type)
    path = path.replace("{filename}", prepared_format.filename)
    path = path.replace("{ext}", prepared_format.ext)
    path = path.replace("{value}", value)
    path = path.replace("{date}", date)
    directory_count = len(directory)
    path_count = len(path)
    maximum_length = maximum_length - (directory_count + path_count - extra_count)
    text_length = text_length if text_length < maximum_length else maximum_length
    if has_text:
        # https://stackoverflow.com/a/43848928
        def utf8_lead_byte(b):
            """A UTF-8 intermediate byte starts with the bits 10xxxxxx."""
            return (b & 0xC0) != 0x80

        def utf8_byte_truncate(text, max_bytes):
            """If text[max_bytes] is not a lead byte, back up until a lead byte is
            found and truncate before that character."""
            utf8 = text.encode("utf8")
            if len(utf8) <= max_bytes:
                return utf8
            i = max_bytes
            while i > 0 and not utf8_lead_byte(utf8[i]):
                i -= 1
            return utf8[:i]

        filtered_text = utf8_byte_truncate(text, text_length).decode("utf8")
        path = path.replace("{text}", filtered_text)
    else:
        path = path.replace("{text}", "")
    directory2 = os.path.join(directory, path)
    directory3 = os.path.abspath(directory2)
    return directory3


def get_directory(directories: list[str], extra_path):
    directories = format_paths(directories, extra_path)
    new_directories = []
    if not directories:
        directories = [""]
    for directory in directories:
        if not os.path.isabs(directory):
            if directory:
                fp: str = os.path.abspath(directory)
            else:
                fp: str = os.path.abspath(extra_path)
            directory = os.path.abspath(fp)
        os.makedirs(directory, exist_ok=True)
        new_directories.append(directory)
    directory = check_space(new_directories, min_size=min_drive_space)
    return directory


def check_space(
    download_paths, min_size=min_drive_space, priority="download", create_directory=True
):
    root = ""
    while not root:
        paths = []
        for download_path in download_paths:
            if create_directory:
                os.makedirs(download_path, exist_ok=True)
            obj_Disk = psutil.disk_usage(download_path)
            free = obj_Disk.free / (1024.0 ** 3)
            x = {"path": download_path, "free": free}
            paths.append(x)
        if priority == "download":
            for item in paths:
                download_path = item["path"]
                free = item["free"]
                if free > min_size:
                    root = download_path
                    break
        elif priority == "upload":
            paths.sort(key=lambda x: x["free"])
            item = paths[0]
            root = item["path"]
    return root


def find_model_directory(username, directories) -> Tuple[str, bool]:
    download_path = ""
    status = False
    for directory in directories:
        download_path = os.path.join(directory, username)
        if os.path.exists(download_path):
            status = True
            break
    return download_path, status


def are_long_paths_enabled():
    if os_name == "Windows":
        from ctypes import WinDLL, c_ubyte

        ntdll = WinDLL("ntdll")

        if hasattr(ntdll, "RtlAreLongPathsEnabled"):

            ntdll.RtlAreLongPathsEnabled.restype = c_ubyte
            ntdll.RtlAreLongPathsEnabled.argtypes = ()
            return bool(ntdll.RtlAreLongPathsEnabled())

        else:
            return False


def check_for_dupe_file(download_path, content_length):
    found = False
    if os.path.isfile(download_path):
        content_length = int(content_length)
        local_size = os.path.getsize(download_path)
        if local_size == content_length:
            found = True
    return found


class download_session(tqdm):
    def start(self, unit="B", unit_scale=True, miniters=1, tsize=0):
        self.unit = unit
        self.unit_scale = unit_scale
        self.miniters = miniters
        self.total = 0
        self.colour = "Green"
        if tsize:
            tsize = int(tsize)
            self.total += tsize

    def update_total_size(self, tsize):
        if tsize:
            tsize = int(tsize)
            self.total += tsize

    def update_to(self, b=1, bsize=1, tsize=None):
        self.update(b)


def get_config(config_path):
    if os.path.exists(config_path):
        json_config = ujson.load(open(config_path))
    else:
        json_config = {}
    json_config2 = copy.deepcopy(json_config)
    json_config = make_settings.fix(json_config)
    file_name = os.path.basename(config_path)
    json_config = ujson.loads(
        json.dumps(make_settings.config(**json_config), default=lambda o: o.__dict__)
    )
    updated = False
    if json_config != json_config2:
        updated = True
        filepath = os.path.join(".settings", "config.json")
        export_data(json_config, filepath)
    if not json_config:
        input(
            f"The .settings\\{file_name} file has been created. Fill in whatever you need to fill in and then press enter when done.\n"
        )
        json_config = ujson.load(open(config_path))
    return json_config, updated


def choose_auth(array):
    names = []
    array = [{"auth_count": -1, "username": "All"}] + array
    string = ""
    name_count = len(array)
    if name_count > 1:

        seperator = " | "
        for count, x in enumerate(array):
            name = x["username"]
            string += f"{str(count)} = {name}"
            names.append(x)
            if count + 1 != name_count:
                string += seperator

    print(f"Auth Usernames: {string}")
    if value := int(input().strip()):
        names = [names[value]]
    else:
        names.pop(0)
    return names


def choose_option(
    subscription_list, auto_scrape: Union[str, bool], use_default_message=False
):
    default_message = ""
    if use_default_message:
        seperator = " | "
        default_message = f"Names: Username = username {seperator}"
    new_names = []
    if names := subscription_list[0]:
        if isinstance(auto_scrape, bool) and auto_scrape:
            values = [x[1] for x in names]
        elif isinstance(auto_scrape, bool) or not auto_scrape:
            print(f"{default_message}{subscription_list[1]}")
            values = input().strip().split(",")
        else:
            values = auto_scrape
            if isinstance(auto_scrape, str):
                values = auto_scrape.split(",")
        for value in values:
            if value.isdigit():
                if value == "0":
                    new_names = names[1:]
                    break
                else:
                    new_name = names[int(value)]
                    new_names.append(new_name)
            else:
                new_name = [name for name in names if value == name[1]]
                new_names.extend(new_name)
    return [x for x in new_names if not isinstance(x[0], SimpleNamespace)]


def process_profiles(json_settings, proxies, site_name, api: Union[OnlyFans.start]):
    profile_directories = json_settings["profile_directories"]
    for profile_directory in profile_directories:
        x = os.path.join(profile_directory, site_name)
        x = os.path.abspath(x)
        os.makedirs(x, exist_ok=True)
        temp_users = os.listdir(x)
        temp_users = remove_mandatory_files(temp_users)
        if not temp_users:
            default_profile_directory = os.path.join(x, "default")
            os.makedirs(default_profile_directory)
            temp_users.append("default")
        for user in temp_users:
            user_profile = os.path.join(x, user)
            user_auth_filepath = os.path.join(user_profile, "auth.json")
            datas = {}
            if os.path.exists(user_auth_filepath):
                temp_json_auth = ujson.load(open(user_auth_filepath))
                json_auth = temp_json_auth["auth"]
                if not json_auth.get("active", None):
                    continue
                json_auth["username"] = user
                auth = api.add_auth(json_auth)
                auth.session_manager.proxies = proxies
                auth.profile_directory = user_profile
                datas["auth"] = auth.auth_details.export()
            if datas:
                export_data(datas, user_auth_filepath)
            print
        print
    return api


async def process_names(
    module, subscription_list, auto_scrape, api, json_config, site_name_lower, site_name
) -> list:
    names = choose_option(subscription_list, auto_scrape, True)
    if not names:
        print("There's nothing to scrape.")
    for name in names:
        # Extra Auth Support
        auth_count = name[0]
        authed = api.auths[auth_count]
        name = name[-1]
        assign_vars(json_config)
        username = parse_links(site_name_lower, name)
        result = await module.start_datascraper(authed, username, site_name)
    return names


async def process_downloads(api, module):
    if json_global_settings["helpers"]["downloader"]:
        for auth in api.auths:
            subscriptions = await auth.get_subscriptions(refresh=False)
            for subscription in subscriptions:
                await module.prepare_downloads(subscription)
                if json_global_settings["helpers"]["delete_empty_directories"]:
                    delete_empty_directories(
                        subscription.download_info.get("base_directory", "")
                    )


async def process_webhooks(api: Union[OnlyFans.start], category, category2):
    global_status = webhooks["global_status"]
    webhook = webhooks[category]
    webhook_state = webhook[category2]
    webhook_links = []
    if webhook_state["status"] is None:
        webhook_status = global_status
    else:
        webhook_status = webhook_state["status"]
    if global_webhooks := webhooks["global_webhooks"]:
        webhook_links = global_webhooks
    if webhook_state["webhooks"]:
        webhook_links = webhook_state["webhooks"]
    if webhook_status:
        webhook_hide_sensitive_info = True
        for auth in api.auths:
            await send_webhook(
                auth, webhook_hide_sensitive_info, webhook_links, category, category2
            )
        print
    print


def is_me(user_api):
    return "email" in user_api


async def write_data(response: ClientResponse, download_path: str, progress_bar):
    status_code = 0
    if response.status == 200:
        total_length = 0
        os.makedirs(os.path.dirname(download_path), exist_ok=True)
        with open(download_path, "wb") as f:
            try:
                async for data in response.content.iter_chunked(4096):
                    length = len(data)
                    total_length += length
                    progress_bar.update(length)
                    f.write(data)
            except (
                ClientPayloadError,
                ContentTypeError,
                ClientOSError,
                ServerDisconnectedError,
            ) as e:
                status_code = 1
    else:
        if response.content_length:
            progress_bar.update_total_size(-response.content_length)
        status_code = 2
    return status_code


def export_data(
    metadata: Union[list, dict], path: str, encoding: Optional[str] = "utf-8"
):
    if directory := os.path.dirname(path):
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding=encoding) as outfile:
        ujson.dump(metadata, outfile, indent=2, escape_forward_slashes=False)


def grouper(n, iterable, fillvalue: Optional[Union[str, int]] = None):
    args = [iter(iterable)] * n
    final_grouped = list(zip_longest(fillvalue=fillvalue, *args))
    if not fillvalue:
        grouped = []
        for group in final_grouped:
            group = [x for x in group if x]
            grouped.append(group)
        final_grouped = grouped
    return final_grouped


def remove_mandatory_files(files, keep=[]):
    matches = ["desktop.ini", ".DS_Store", ".DS_store", "@eaDir"]
    folders = [x for x in files if x not in matches]
    if keep:
        folders = [x for x in files if x in keep]
    return folders


def legacy_metadata(directory):
    if not os.path.exists(directory):
        return
    items = os.listdir(directory)
    matches = ["desktop.ini"]
    if items := [x for x in items if x not in matches]:
        metadatas = []
        for item in items:
            path = os.path.join(directory, item)
            metadata = ujson.load(open(path))
            metadatas.append(metadata)
            print
    print


def metadata_fixer(directory):
    archive_file = os.path.join(directory, "archive.json")
    metadata_file = os.path.join(directory, "Metadata")
    if os.path.exists(archive_file):
        os.makedirs(metadata_file, exist_ok=True)
        new = os.path.join(metadata_file, "Archive.json")
        shutil.move(archive_file, new)


def ordinal(n):
    return "%d%s" % (n, "tsnrhtdd"[(n / 10 % 10 != 1) * (n % 10 < 4) * n % 10 :: 4])


def id_generator(size=6, chars=string.ascii_uppercase + string.digits):
    return "".join(random.choice(chars) for _ in range(size))


def humansize(nbytes):
    i = 0
    suffixes = ["B", "KB", "MB", "GB", "TB", "PB"]
    while nbytes >= 1024 and i < len(suffixes) - 1:
        nbytes /= 1024.0
        i += 1
    f = ("%.2f" % nbytes).rstrip("0").rstrip(".")
    return f"{f} {suffixes[i]}"


def byteToGigaByte(n):
    return n / math.pow(10, 9)


async def send_webhook(
    item, webhook_hide_sensitive_info, webhook_links, category, category2: str
):
    if category == "auth_webhook":
        for webhook_link in webhook_links:
            auth = item
            username = auth.username
            if webhook_hide_sensitive_info:
                username = "REDACTED"
            message = prepare_webhooks.discord()
            embed = message.embed()
            embed.title = f"Auth {category2.capitalize()}"
            embed.add_field("username", username)
            message.embeds.append(embed)
            message = ujson.loads(json.dumps(message, default=lambda o: o.__dict__))
            requests.post(webhook_link, json=message)
    if category == "download_webhook":
        subscriptions: list[create_user] = await item.get_subscriptions(refresh=False)
        for subscription in subscriptions:
            download_info = subscription.download_info
            if download_info:
                for webhook_link in webhook_links:
                    message = prepare_webhooks.discord()
                    embed = message.embed()
                    embed.title = f"Downloaded: {subscription.username}"
                    embed.add_field("username", subscription.username)
                    embed.add_field("post_count", subscription.postsCount)
                    embed.add_field("link", subscription.get_link())
                    embed.image.url = subscription.avatar
                    message.embeds.append(embed)
                    message = ujson.loads(
                        json.dumps(message, default=lambda o: o.__dict__)
                    )
                    requests.post(webhook_link, json=message)


def find_between(s, start, end):
    format = f"{start}(.+?){end}"
    x = re.search(format, s)
    x = x[1] if x else s
    return x


def delete_empty_directories(directory):
    def start(directory):
        for root, dirnames, files in os.walk(directory, topdown=False):
            for dirname in dirnames:
                full_path = os.path.realpath(os.path.join(root, dirname))
                contents = os.listdir(full_path)
                if not contents:
                    shutil.rmtree(full_path, ignore_errors=True)
                else:
                    content_count = len(contents)
                    if content_count == 1 and "desktop.ini" in contents:
                        shutil.rmtree(full_path, ignore_errors=True)

    start(directory)
    if os.path.exists(directory) and not os.listdir(directory):
        os.rmdir(directory)


def multiprocessing():
    return ThreadPool() if max_threads < 1 else ThreadPool(max_threads)


def module_chooser(domain, json_sites):
    string = "Site: "
    seperator = " | "
    site_names = []
    wl = ["onlyfans"]
    bl = ["patreon"]
    site_count = len(json_sites)
    count = 0
    for x in json_sites:
        if not wl and x in bl or wl and x not in wl:
            continue
        string += f"{str(count)} = {x}"
        site_names.append(x)
        if count + 1 != site_count:
            string += seperator

        count += 1
    if domain and domain not in site_names:
        string = f"{domain} not supported"
        site_names = []
    return string, site_names


async def move_to_old(
    folder_directory: str,
    base_download_directories: list,
    first_letter: str,
    model_username: str,
    source: str,
):
    # MOVE TO OLD
    local_destinations = [
        os.path.join(x, folder_directory) for x in base_download_directories
    ]
    local_destination = check_space(local_destinations, min_size=100)
    local_destination = os.path.join(local_destination, first_letter, model_username)
    print(f"Moving {source} -> {local_destination}")
    shutil.copytree(source, local_destination, dirs_exist_ok=True)
    shutil.rmtree(source)
