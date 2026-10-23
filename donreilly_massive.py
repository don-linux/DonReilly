# /// script
# dependencies = [
#   "aiohttp",
#   "lxml",
# ]
# ///

import asyncio
import json
import sys
import zipfile
from pathlib import Path

import aiohttp
from lxml import etree
from lxml import html as lhtml

BASE_URL = 'https://learning.oreilly.com'
CONFIG_PATH = Path('books.json')

# HTTP statuses worth retrying: rate-limiting and transient server errors.
RETRY_STATUSES = {403, 429, 500, 502, 503, 504}

CONTAINER = b"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
    <rootfiles>
        <rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>
"""  # noqa


class ConfigError(Exception):
    pass


def load_config(path=CONFIG_PATH):
    """Read jwt, book ids, and optional pause settings from books.json."""
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        raise ConfigError(
            'books.json not found. Copy books.example.json to books.json.'
        ) from None
    except OSError as e:
        raise ConfigError(f'Could not read {path}: {e}') from None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f'{path} is not valid JSON: {e}') from None

    if not isinstance(data, dict):
        raise ConfigError(f'{path} must contain a JSON object.')

    jwt = data.get('jwt')
    if not isinstance(jwt, str) or not jwt.strip():
        raise ConfigError(f'{path} needs a jwt.')
    jwt = jwt.strip()

    ids = data.get('ids')
    if not isinstance(ids, list) or not ids:
        raise ConfigError(f'{path} needs a list of ids.')

    book_ids = []
    for book_id in ids:
        if isinstance(book_id, bool) or not isinstance(book_id, (str, int)):
            raise ConfigError(f'{path} ids must be strings.')
        book_id = str(book_id).strip()
        if (
            not book_id
            or book_id in ('.', '..')
            or '/' in book_id
            or '\\' in book_id
        ):
            raise ConfigError(f'Invalid book id: {book_id!r}')
        book_ids.append(book_id)

    return {
        'jwt': jwt,
        'ids': book_ids,
        'pause_every_pages': _int_setting(data, 'pause_every_pages', 10),
        'pause_seconds': _int_setting(data, 'pause_seconds', 30),
        'concurrency': _int_setting(data, 'concurrency', 2, minimum=1),
        'retries': _int_setting(data, 'retries', 5),
        'output_dir': _output_dir(data),
    }


def _int_setting(data, key, default, *, minimum=0):
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f'{key} must be an integer.')
    if value < minimum:
        raise ConfigError(f'{key} must be {minimum} or greater.')
    return value


def _output_dir(data):
    value = data.get('output_dir', 'downloads')
    if not isinstance(value, str) or not value.strip():
        raise ConfigError('output_dir must be a folder path.')
    return Path(value)


def to_xhtml(s, root_path):
    tree = lhtml.fromstring(s, parser=lhtml.HTMLParser(encoding='utf-8'))

    for el in list(tree.iter()):
        for attr in ['href', 'src']:
            if (el.get(attr) or '').startswith(root_path):
                el.set(attr, el.get(attr).removeprefix(root_path))

    if tree.tag != 'html':
        wrapper = etree.Element('html', nsmap={
            None: 'http://www.w3.org/1999/xhtml',
            'epub': 'http://www.idpf.org/2007/ops',
        })

        h1 = tree.find('.//h1')
        if h1 is not None:
            head = etree.SubElement(wrapper, 'head')
            title = etree.SubElement(head, 'title')
            title.text = ''.join(h1.itertext()).strip()

        body = etree.SubElement(wrapper, 'body')
        body.append(tree)
        tree = wrapper

    return etree.tostring(
        tree,
        xml_declaration=True,
        doctype='<!DOCTYPE html>',
        pretty_print=True,
        encoding='utf-8',
    )


async def check_auth(session):
    url = BASE_URL + '/api/v1/user-preferences/'
    async with session.get(url, raise_for_status=False) as r:
        return r.ok


async def fetch(session, url, *, retries=5, backoff=1.0):
    """GET `url` and return the body, retrying transient failures with
    exponential backoff (backoff, 2*backoff, 4*backoff, ...)."""
    for attempt in range(retries + 1):
        try:
            async with session.get(url) as r:
                return await r.read()
        except aiohttp.ClientResponseError as e:
            if e.status not in RETRY_STATUSES or attempt == retries:
                raise
            reason = f'HTTP {e.status}'
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == retries:
                raise
            reason = type(e).__name__

        wait = backoff * 2 ** attempt
        print(f'  {reason} on {url}; retry {attempt + 1}/{retries} in {wait:g}s')
        await asyncio.sleep(wait)


async def wait_out_rate_limit(seconds):
    print(
        f'To avoid rate limit please wait {seconds} seconds. '
        'The download will continue by itself.'
    )
    await asyncio.sleep(seconds)


class PagePause:
    """Pause every N fetched file-list pages, across the whole run."""

    def __init__(self, every, seconds):
        self.every = every
        self.seconds = seconds
        self.pages = 0
        self.paused_at = None

    def note_page(self):
        self.pages += 1

    async def wait_if_needed(self, more_work):
        if not more_work or not self.every or not self.seconds:
            return
        if (
            self.pages == 0
            or self.pages % self.every != 0
            or self.paused_at == self.pages
        ):
            return
        self.paused_at = self.pages
        await wait_out_rate_limit(self.seconds)


async def fetch_book(book_id, zfh, session, *, concurrency, retries, pause):
    root_path = f'/api/v2/epubs/urn:orm:book:{book_id}/files/'
    semaphore = asyncio.Semaphore(concurrency)

    async def download(url, path):
        async with semaphore:
            content = await fetch(session, url, retries=retries)
        if path.endswith(('.html', '.xhtml')):
            content = to_xhtml(content, root_path)
        zfh.writestr(path, content)

    zfh.writestr('mimetype', b'application/epub+zip', compress_type=zipfile.ZIP_STORED)
    zfh.writestr('META-INF/container.xml', CONTAINER)

    url = BASE_URL + root_path
    while url:
        print(f'fetching {url}')
        data = json.loads(await fetch(session, url, retries=retries))

        await asyncio.gather(*[
            download(result['url'], f'EPUB/{result["full_path"]}')
            for result in data.get('results', [])
        ])

        url = data.get('next')
        pause.note_page()
        await pause.wait_if_needed(bool(url))


async def download_all(config):
    output_dir = config['output_dir']
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    failed = []
    pause = PagePause(config['pause_every_pages'], config['pause_seconds'])
    ids = config['ids']

    async with aiohttp.ClientSession(
        raise_for_status=True,
        cookies={'orm-jwt': config['jwt']},
    ) as session:
        if await check_auth(session):
            print('Authentication successful.')
        else:
            print('Authentication failed. Continuing without…')

        for index, book_id in enumerate(ids):
            filename = output_dir / f'{book_id}.epub'
            finished = False
            try:
                with zipfile.ZipFile(filename, 'w') as zfh:
                    await fetch_book(
                        book_id, zfh, session,
                        concurrency=config['concurrency'],
                        retries=config['retries'],
                        pause=pause,
                    )
                finished = True
            except Exception as e:
                print(f'{book_id} failed: {e}')
                failed.append(book_id)
            else:
                print(f'created {filename}')
                downloaded.append(book_id)
            finally:
                if not finished:
                    filename.unlink(missing_ok=True)
            await pause.wait_if_needed(index < len(ids) - 1)

    print(f'{len(downloaded)} downloaded, {len(failed)} failed')
    for book_id in failed:
        print(f'  {book_id}')
    return failed


async def amain():
    try:
        config = load_config()
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 1

    failed = await download_all(config)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(amain()))
