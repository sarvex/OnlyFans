import asyncio
import copy
import hashlib
import json
import os
import re
import threading
import time
from itertools import chain
from multiprocessing import cpu_count
from multiprocessing.dummy import Pool as ThreadPool
from multiprocessing.pool import Pool
from os.path import dirname as up
from random import randint
from typing import Any, Optional
from urllib.parse import urlparse

import python_socks
import requests
from aiohttp import ClientSession
from aiohttp.client_exceptions import (
    ClientConnectorError,
    ClientOSError,
    ClientPayloadError,
    ContentTypeError,
    ServerDisconnectedError,
)
from aiohttp.client_reqrep import ClientResponse
from aiohttp_socks import ProxyConnectionError, ProxyConnector, ProxyError
from database.databases.user_data.models.media_table import template_media_table

from apis.onlyfans.classes import create_auth, create_user
from apis.onlyfans.classes.extras import error_details

path = up(up(os.path.realpath(__file__)))
os.chdir(path)


global_settings: dict[str, Any] = {
    "dynamic_rules_link": "https://raw.githubusercontent.com/DATAHOARDERS/dynamic-rules/main/onlyfans.json"
}


class set_settings:
    def __init__(self, option={}):
        global global_settings
        self.proxies = option.get("proxies")
        self.cert = option.get("cert")
        self.json_global_settings = option
        global_settings = self.json_global_settings


async def remove_errors(results: list):
    wrapped = False
    if not isinstance(results, list):
        wrapped = True
        results = [results]
    results = [x for x in results if not isinstance(x, error_details)]
    if wrapped and results:
        results = results[0]
    return results


def chunks(l, n):
    return [l[i * n : (i + 1) * n] for i in range((len(l) + n - 1) // n)]


def calculate_max_threads(max_threads=None):
    if not max_threads:
        max_threads = -1
    max_threads2 = cpu_count()
    if max_threads < 1 or max_threads >= max_threads2:
        max_threads = max_threads2
    return max_threads


def multiprocessing(max_threads: Optional[int] = None):
    max_threads = calculate_max_threads(max_threads)
    return ThreadPool(max_threads)


class session_manager:
    def __init__(
        self,
        auth: create_auth,
        headers: dict[str, Any] = {},
        proxies: list[str] = [],
        max_threads: int = -1,
    ) -> None:
        self.pool: Pool = auth.pool if auth.pool else multiprocessing()
        self.max_threads = max_threads
        self.kill = False
        self.headers = headers
        self.proxies: list[str] = proxies
        dr_link = global_settings["dynamic_rules_link"]
        dynamic_rules = requests.get(dr_link).json()  # type: ignore
        self.dynamic_rules = dynamic_rules
        self.auth = auth

    def create_client_session(self):
        proxy = self.get_proxy()
        connector = ProxyConnector.from_url(proxy) if proxy else None

        final_cookies = self.auth.auth_details.cookie.format()
        return ClientSession(
            connector=connector, cookies=final_cookies, read_timeout=None
        )

    def get_proxy(self) -> str:
        proxies = self.proxies
        return self.proxies[randint(0, len(proxies) - 1)] if proxies else ""

    def stimulate_sessions(self):
        # Some proxies switch IP addresses if no request have been made for x amount of secondss
        def do(session_manager):
            while not session_manager.kill:
                for session in session_manager.sessions:

                    def process_links(link, session):
                        response = session.get(link)
                        text = response.text.strip("\n")
                        if text == session.ip:
                            print
                        else:
                            found_dupe = [
                                x for x in session_manager.sessions if x.ip == text
                            ]
                            if found_dupe:
                                return
                            cloned_session = copy.deepcopy(session)
                            cloned_session.ip = text
                            cloned_session.links = []
                            session_manager.sessions.append(cloned_session)
                            print(text)
                            print
                        return text

                    time.sleep(62)
                    link = "https://checkip.amazonaws.com"
                    ip = process_links(link, session)
                    print

        t1 = threading.Thread(target=do, args=[self])
        t1.start()

    async def json_request(
        self,
        link: str,
        session: Optional[ClientSession] = None,
        method: str = "GET",
        stream: bool = False,
        json_format: bool = True,
        payload: dict[str, str] = {},
    ) -> Any:
        headers = {}
        custom_session = False
        if not session:
            custom_session = True
            session = self.create_client_session()
        headers = self.session_rules(link)
        headers["accept"] = "application/json, text/plain, */*"
        headers["Connection"] = "keep-alive"
        temp_payload = payload.copy()

        request_method = None
        result = None
        if method == "DELETE":
            request_method = session.delete
        elif method == "GET":
            request_method = session.get
        elif method == "HEAD":
            request_method = session.head
        elif method == "POST":
            request_method = session.post
            headers["content-type"] = "application/json"
            temp_payload = json.dumps(payload)
        else:
            return None
        while True:
            try:
                response = await request_method(link, headers=headers, data=temp_payload)
                if method != "HEAD" and json_format and not stream:
                    result = await response.json()
                    if "error" in result:
                        result = error_details(result)
                elif (
                    method != "HEAD"
                    and stream
                    and not json_format
                    or method == "HEAD"
                ):
                    result = response
                else:
                    result = await response.read()
                break
            except (ClientConnectorError, ProxyError):
                break
            except (
                ClientPayloadError,
                ContentTypeError,
                ClientOSError,
                ServerDisconnectedError,
                ProxyConnectionError,
                ConnectionResetError,
            ):
                continue
        if custom_session:
            await session.close()
        return result

    async def async_requests(self, items: list[str]) -> list:
        tasks = []

        async def run(links) -> list:
            proxies = self.proxies
            proxy = self.proxies[randint(0, len(proxies) - 1)] if proxies else ""
            connector = ProxyConnector.from_url(proxy) if proxy else None
            async with ClientSession(
                connector=connector,
                cookies=self.auth.auth_details.cookie.format(),
                read_timeout=None,
            ) as session:
                for link in links:
                    task = asyncio.ensure_future(self.json_request(link, session))
                    tasks.append(task)
                responses = list(await asyncio.gather(*tasks))
                return responses

        results = await asyncio.ensure_future(run(items))
        return results

    async def download_content(
        self,
        download_item: template_media_table,
        session: ClientSession,
        progress_bar,
        subscription: create_user,
    ):
        attempt_count = 1
        new_task = {}
        while attempt_count <= 3:
            attempt_count += 1
            if not download_item.link:
                continue
            response: ClientResponse
            response = await asyncio.ensure_future(
                self.json_request(
                    download_item.link,
                    session,
                    json_format=False,
                    stream=True,
                )
            )
            if response and response.status != 200:
                if response.content_length:
                    progress_bar.update_total_size(-response.content_length)
                api_type = download_item.__module__.split(".")[-1]
                post_id = download_item.post_id
                new_result = None
                if api_type == "messages":
                    new_result = await subscription.get_message_by_id(
                        message_id=post_id
                    )
                elif api_type == "posts":
                    new_result = await subscription.get_post(post_id)
                if isinstance(new_result, error_details):
                    continue
                if new_result and new_result.media:
                    if media_list := [
                        x
                        for x in new_result.media
                        if x["id"] == download_item.media_id
                    ]:
                        media = media_list[0]
                        quality = subscription.subscriber.extras["settings"][
                            "supported"
                        ]["onlyfans"]["settings"]["video_quality"]
                        link = await new_result.link_picker(media, quality)
                        download_item.link = link
                    continue
            new_task["response"] = response
            new_task["download_item"] = download_item
            break
        return new_task

    def session_rules(self, link: str) -> dict[str, Any]:
        headers = self.headers
        if "https://onlyfans.com/api2/v2/" in link:
            dynamic_rules = self.dynamic_rules
            headers["app-token"] = dynamic_rules["app_token"]
            # auth_id = headers["user-id"]
            a = [link, 0, dynamic_rules]
            headers2 = self.create_signed_headers(*a)
            headers |= headers2
        return headers

    def create_signed_headers(self, link: str, auth_id: int, dynamic_rules: dict):
        # Users: 300000 | Creators: 301000
        final_time = str(int(round(time.time())))
        path = urlparse(link).path
        query = urlparse(link).query
        path = path if not query else f"{path}?{query}"
        a = [dynamic_rules["static_param"], final_time, path, str(auth_id)]
        msg = "\n".join(a)
        message = msg.encode("utf-8")
        hash_object = hashlib.sha1(message)
        sha_1_sign = hash_object.hexdigest()
        sha_1_b = sha_1_sign.encode("ascii")
        checksum = (
            sum(sha_1_b[number] for number in dynamic_rules["checksum_indexes"])
            + dynamic_rules["checksum_constant"]
        )
        return {
            "sign": dynamic_rules["format"].format(sha_1_sign, abs(checksum)),
            "time": final_time,
        }


async def test_proxies(proxies: list[str]):
    final_proxies = []
    for proxy in proxies:
        connector = ProxyConnector.from_url(proxy) if proxy else None
        async with ClientSession(connector=connector) as session:
            link = "https://checkip.amazonaws.com"
            try:
                response = await session.get(link)
                ip = await response.text()
                ip = ip.strip()
                print(f"Session IP: {ip}" + "\n")
                final_proxies.append(proxy)
            except python_socks._errors.ProxyConnectionError as e:
                print(f"Proxy Not Set: {proxy}\n")
                continue
    return final_proxies


def restore_missing_data(master_set2, media_set, split_by):
    count = 0
    new_set = []
    for item in media_set:
        if not item:
            link = master_set2[count]
            offset = int(link.split("?")[-1].split("&")[1].split("=")[1])
            limit = int(link.split("?")[-1].split("&")[0].split("=")[1])
            if limit == split_by + 1:
                break
            offset2 = offset
            limit2 = int(limit / split_by)
            for _ in range(1, split_by + 1):
                link2 = link.replace(f"limit={limit}", f"limit={limit2}")
                link2 = link2.replace(f"offset={offset}", f"offset={str(offset2)}")
                offset2 += limit2
                new_set.append(link2)
        count += 1
    return new_set if new_set else master_set2


async def scrape_endpoint_links(links, session_manager: session_manager, api_type):
    media_set = []
    max_attempts = 100
    api_type = api_type.capitalize()
    for attempt in list(range(max_attempts)):
        if not links:
            continue
        print(f"Scrape Attempt: {str(attempt + 1)}/{max_attempts}")
        results = await session_manager.async_requests(links)
        results = await remove_errors(results)
        not_faulty = [x for x in results if x]
        faulty = [
            {"key": k, "value": v, "link": links[k]}
            for k, v in enumerate(results)
            if not v
        ]
        last_number = len(results) - 1
        if faulty:
            positives = [x for x in faulty if x["key"] != last_number]
            false_positive = [x for x in faulty if x["key"] == last_number]
            if positives:
                attempt = attempt if attempt > 1 else attempt + 1
                num = int(len(faulty) * (100 / attempt))
                split_by = 2
                print(f"Missing {num} Posts... Retrying...")
                links = restore_missing_data(links, results, split_by)
                media_set.extend(not_faulty)
            if not positives and false_positive:
                media_set.extend(not_faulty)
                break
            print
        else:
            media_set.extend(not_faulty)
            break
    return list(chain(*media_set))


def calculate_the_unpredictable(link, limit, multiplier=1):
    final_links = []
    a = list(range(1, multiplier + 1))
    for b in a:
        parsed_link = urlparse(link)
        q = parsed_link.query.split("&")
        offset = q[1]
        old_offset_num = int(re.findall("\\d+", offset)[0])
        new_offset_num = old_offset_num + (limit * b)
        new_link = link.replace(offset, f"offset={new_offset_num}")
        final_links.append(new_link)
    return final_links
